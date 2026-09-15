"""S3 backend placeholder that records its configuration and refuses every operation for now."""

from __future__ import annotations

from typing import TYPE_CHECKING

import icechunk
import pyarrow.fs
from obstore.store import ObjectStore
from pydantic import SecretStr

from ocs_storage_exploration.storage.addresses import StorageAddress, StorageScheme
from ocs_storage_exploration.storage.backends.base import BaseStorageBackend
from ocs_storage_exploration.storage.errors import BackendNotSupportedError

if TYPE_CHECKING:
    from ocs_storage_exploration.settings import Settings

NOT_IMPLEMENTED_MESSAGE = "the s3 backend is not implemented yet; use the file or memory backend"


class S3StorageBackend(BaseStorageBackend):
    """Holds a complete S3 configuration while every operation still raises BackendNotSupportedError."""

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
    ) -> None:
        """Record the full S3 configuration without building any client yet."""
        super().__init__(scheme=StorageScheme.S3, root=bucket, base_prefix=base_prefix)
        self._region = region
        self._endpoint_url = endpoint_url
        self._allow_http = allow_http
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session_token = session_token
        self._force_path_style = force_path_style
        self._anonymous = anonymous

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
        )

    @property
    def available(self) -> bool:
        """Report that this backend cannot serve requests yet."""
        return False

    @property
    def supports_parquet_filesystem(self) -> bool:
        """Report that no pyarrow filesystem is wired up yet."""
        return False

    @property
    def has_credentials(self) -> bool:
        """Report whether static credentials were configured, without revealing them."""
        return self._access_key_id is not None and self._secret_access_key is not None

    def icechunk_storage(self, address: StorageAddress) -> icechunk.Storage:
        """Refuse to resolve Icechunk storage until the S3 backend is implemented."""
        raise BackendNotSupportedError(NOT_IMPLEMENTED_MESSAGE)

    def object_store(self) -> ObjectStore:
        """Refuse to build an obstore store until the S3 backend is implemented."""
        raise BackendNotSupportedError(NOT_IMPLEMENTED_MESSAGE)

    def parquet_filesystem(self) -> pyarrow.fs.FileSystem | None:
        """Refuse to build a pyarrow filesystem until the S3 backend is implemented."""
        raise BackendNotSupportedError(NOT_IMPLEMENTED_MESSAGE)

    def parquet_path(self, address: StorageAddress) -> str:
        """Refuse to render a Parquet path until the S3 backend is implemented."""
        raise BackendNotSupportedError(NOT_IMPLEMENTED_MESSAGE)

    def description_details(self) -> dict[str, str]:
        """Describe the S3 configuration without exposing any secret."""
        return {
            "region": self._region or "",
            "endpoint_url": self._endpoint_url or "",
            "allow_http": str(self._allow_http).lower(),
            "force_path_style": str(self._force_path_style).lower(),
            "anonymous": str(self._anonymous).lower(),
            "has_credentials": str(self.has_credentials).lower(),
            "status": "not implemented",
        }
