"""S3 backend resolving addresses to Icechunk, obstore and pyarrow handles on one bucket."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import icechunk
import pyarrow.fs
from obstore.store import ObjectStore, S3Store
from pydantic import SecretStr

from ocs_storage_exploration.storage.addresses import StorageAddress, StorageScheme
from ocs_storage_exploration.storage.backends.base import BaseStorageBackend
from ocs_storage_exploration.storage.errors import BackendNotSupportedError

if TYPE_CHECKING:
    from obstore.store import ClientConfig, RetryConfig, S3Config

    from ocs_storage_exploration.settings import Settings

HTTP_SCHEME: Final[str] = "http"
HTTPS_SCHEME: Final[str] = "https"
VIRTUAL_HOSTED_STYLE: Final[str] = "virtual-hosted"
PATH_STYLE: Final[str] = "path"


class S3StorageBackend(BaseStorageBackend):
    """Stores every dataset under one base prefix of one S3 compatible bucket."""

    def __init__(
        self,
        bucket: str,
        base_prefix: str = "ocs",
        region: str | None = None,
        endpoint_url: str | None = None,
        allow_http: bool = False,
        access_key_id: str | None = None,
        secret_access_key: SecretStr | None = None,
        session_token: SecretStr | None = None,
        force_path_style: bool = True,
        anonymous: bool = False,
        connect_timeout_seconds: float = 5.0,
        request_timeout_seconds: float = 30.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        """Record the S3 configuration and prepare the lazily built obstore and pyarrow handles."""
        super().__init__(scheme=StorageScheme.S3, root=bucket, base_prefix=base_prefix)
        self._region = region
        self._endpoint_url = endpoint_url
        self._allow_http = allow_http
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session_token = session_token
        self._force_path_style = force_path_style
        self._anonymous = anonymous
        self._connect_timeout_seconds = connect_timeout_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._store: S3Store | None = None
        self._filesystem: pyarrow.fs.S3FileSystem | None = None
        # One application serves several requests on the threadpool, so each client is built once
        # under this lock. The clients themselves are thread-safe: obstore's S3Store is a Rust
        # object_store handle and pyarrow's S3FileSystem is documented as safe to share.
        self._client_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> S3StorageBackend:
        """Build an S3 backend from the object storage block of the service settings."""
        options = settings.s3
        if options is None:
            raise BackendNotSupportedError("the s3 backend needs an OCS_STORAGE_S3__ configuration block")
        return cls(
            bucket=options.bucket,
            base_prefix=options.prefix or settings.base_prefix,
            region=options.region,
            endpoint_url=options.endpoint_url,
            allow_http=options.allow_http,
            access_key_id=options.access_key_id,
            secret_access_key=options.secret_access_key,
            session_token=options.session_token,
            force_path_style=options.force_path_style,
            anonymous=options.anonymous,
            connect_timeout_seconds=options.connect_timeout_seconds,
            request_timeout_seconds=options.request_timeout_seconds,
            max_retries=options.max_retries,
            retry_backoff_seconds=options.retry_backoff_seconds,
        )

    @property
    def bucket(self) -> str:
        """Bucket every key of this backend lives in."""
        return self._root

    @property
    def has_credentials(self) -> bool:
        """Report whether static credentials were configured, without revealing them."""
        return self._access_key_id is not None and self._secret_access_key is not None

    @property
    def retry_budget_seconds(self) -> float:
        """Wall time a single request may spend across its attempts and the backoff between them."""
        return self._request_timeout_seconds * (self._max_retries + 1) + self._retry_backoff_seconds * self._max_retries

    def icechunk_storage(self, address: StorageAddress) -> icechunk.Storage:
        """Resolve an address into S3 Icechunk storage prefixed by the key of the repository."""
        return icechunk.s3_storage(
            bucket=self.bucket,
            prefix=address.key,
            region=self._region,
            endpoint_url=self._endpoint_url,
            allow_http=self._allow_http,
            access_key_id=self._access_key_id,
            secret_access_key=_revealed_secret(self._secret_access_key),
            session_token=_revealed_secret(self._session_token),
            anonymous=self._anonymous or None,
            force_path_style=self._force_path_style,
            # Icechunk takes whole seconds here; the millisecond bounds are in repository_config.
            network_stream_timeout_seconds=max(1, round(self._request_timeout_seconds)),
        )

    def repository_config(self) -> icechunk.RepositoryConfig:
        """Bound the Icechunk S3 client with the same timeouts and retries as the other two handles."""
        return icechunk.RepositoryConfig(
            storage=icechunk.StorageSettings(
                timeouts=icechunk.StorageTimeoutSettings(
                    connect_timeout_ms=_milliseconds(self._connect_timeout_seconds),
                    read_timeout_ms=_milliseconds(self._request_timeout_seconds),
                    operation_timeout_ms=_milliseconds(self.retry_budget_seconds),
                    operation_attempt_timeout_ms=_milliseconds(self._request_timeout_seconds),
                ),
                retries=icechunk.StorageRetriesSettings(
                    # Icechunk counts tries including the first one; obstore counts retries after it.
                    max_tries=self._max_retries + 1,
                    initial_backoff_ms=_milliseconds(self._retry_backoff_seconds),
                    max_backoff_ms=_milliseconds(self._maximum_backoff_seconds),
                ),
            ),
        )

    def object_store(self) -> ObjectStore:
        """Return the obstore store of the whole bucket, built once and keyed by the full object key."""
        with self._client_lock:
            if self._store is None:
                self._store = S3Store(
                    self.bucket,
                    config=self._obstore_config(),
                    client_options=self._client_options(),
                    retry_config=self._retry_config(),
                )
            return self._store

    def parquet_filesystem(self) -> pyarrow.fs.FileSystem | None:
        """Return the pyarrow S3 filesystem of the bucket, built once."""
        with self._client_lock:
            if self._filesystem is None:
                self._filesystem = pyarrow.fs.S3FileSystem(**self._pyarrow_options())
            return self._filesystem

    def parquet_path(self, address: StorageAddress) -> str:
        """Render an address as the bucket-qualified path the pyarrow S3 filesystem expects."""
        return f"{self.bucket}/{address.key}"

    def description_details(self) -> dict[str, str]:
        """Describe the S3 configuration without exposing any secret."""
        return {
            "bucket": self.bucket,
            "region": self._region or "",
            "endpoint_url": self._endpoint_url or "",
            "addressing_style": PATH_STYLE if self._force_path_style else VIRTUAL_HOSTED_STYLE,
            "allow_http": str(self._allow_http).lower(),
            "anonymous": str(self._anonymous).lower(),
            "has_credentials": str(self.has_credentials).lower(),
            "connect_timeout_seconds": str(self._connect_timeout_seconds),
            "request_timeout_seconds": str(self._request_timeout_seconds),
            "max_retries": str(self._max_retries),
        }

    @property
    def _maximum_backoff_seconds(self) -> float:
        """Longest pause between two attempts, reached by doubling the initial backoff once per retry."""
        return self._retry_backoff_seconds * 2.0**self._max_retries

    def _obstore_config(self) -> S3Config:
        """Translate the settings into the obstore S3 configuration."""
        config: S3Config = {"virtual_hosted_style_request": not self._force_path_style}
        if self._region is not None:
            config["region"] = self._region
        if self._endpoint_url is not None:
            config["endpoint"] = self._endpoint_url
        if self._access_key_id is not None:
            config["access_key_id"] = self._access_key_id
        if self._secret_access_key is not None:
            config["secret_access_key"] = self._secret_access_key.get_secret_value()
        if self._session_token is not None:
            config["session_token"] = self._session_token.get_secret_value()
        if self._anonymous:
            config["skip_signature"] = True
        return config

    def _client_options(self) -> ClientConfig:
        """Translate the settings into the obstore HTTP client options."""
        return {
            "allow_http": self._allow_http,
            "connect_timeout": timedelta(seconds=self._connect_timeout_seconds),
            "timeout": timedelta(seconds=self._request_timeout_seconds),
        }

    def _retry_config(self) -> RetryConfig:
        """Translate the settings into the obstore retry configuration."""
        return {
            "max_retries": self._max_retries,
            "retry_timeout": timedelta(seconds=self.retry_budget_seconds),
            "backoff": {
                "init_backoff": timedelta(seconds=self._retry_backoff_seconds),
                "max_backoff": timedelta(seconds=self._maximum_backoff_seconds),
                "base": 2,
            },
        }

    def _pyarrow_options(self) -> dict[str, object]:
        """Translate the settings into the pyarrow S3 filesystem options."""
        options: dict[str, object] = {
            "anonymous": self._anonymous,
            "connect_timeout": self._connect_timeout_seconds,
            "request_timeout": self._request_timeout_seconds,
            # pyarrow counts attempts including the first one, as Icechunk does.
            "retry_strategy": pyarrow.fs.AwsStandardS3RetryStrategy(max_attempts=self._max_retries + 1),
        }
        if self._region is not None:
            options["region"] = self._region
        if not self._anonymous:
            if self._access_key_id is not None:
                options["access_key"] = self._access_key_id
            if self._secret_access_key is not None:
                options["secret_key"] = self._secret_access_key.get_secret_value()
            if self._session_token is not None:
                options["session_token"] = self._session_token.get_secret_value()
        if self._endpoint_url is not None:
            # pyarrow wants a bare authority and takes the transport from scheme; it then uses path addressing.
            parts = urlsplit(self._endpoint_url)
            options["endpoint_override"] = parts.netloc or parts.path
            options["scheme"] = parts.scheme or (HTTP_SCHEME if self._allow_http else HTTPS_SCHEME)
        elif self._allow_http:
            options["scheme"] = HTTP_SCHEME
        return options


def _revealed_secret(secret: SecretStr | None) -> str | None:
    """Reveal a secret for the client that needs it, or return None when it was never set."""
    return None if secret is None else secret.get_secret_value()


def _milliseconds(seconds: float) -> int:
    """Render a duration in seconds as the whole milliseconds Icechunk asks for, never rounding down to zero."""
    return max(1, round(seconds * 1000))
