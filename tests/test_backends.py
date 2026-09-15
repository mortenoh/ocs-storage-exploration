"""Tests for the storage backends and the plugin facade that builds them."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import icechunk
import obstore
import pyarrow.fs
import pytest
from obstore.store import S3Store
from pydantic import SecretStr

from ocs_storage_exploration.settings import ObjectStorageSettings, Settings
from ocs_storage_exploration.storage.addresses import StorageScheme
from ocs_storage_exploration.storage.backends import (
    FilesystemStorageBackend,
    MemoryStorageBackend,
    S3StorageBackend,
    default_plugin_manager,
)
from ocs_storage_exploration.storage.errors import BackendNotSupportedError
from ocs_storage_exploration.storage.plugins import backend_for_scheme, provided_schemes
from ocs_storage_exploration.storage.protocols import StorageBackend

SECRET_VALUE = "supersecretvalue"


def build_s3_backend() -> S3StorageBackend:
    return S3StorageBackend(
        bucket="ocs-exploration",
        region="eu-north-1",
        endpoint_url="http://localhost:9000",
        allow_http=True,
        access_key_id="rustfsadmin",
        secret_access_key=SecretStr(SECRET_VALUE),
        session_token=SecretStr(SECRET_VALUE),
    )


def test_backends_satisfy_the_protocol(storage_backend: StorageBackend) -> None:
    assert isinstance(storage_backend, StorageBackend)


def test_addresses_start_at_the_base_prefix(storage_backend: StorageBackend) -> None:
    address = storage_backend.address("catalog/datasets", "one.json")

    assert address.key == f"{storage_backend.base_prefix}/catalog/datasets/one.json"
    assert address.scheme is storage_backend.scheme
    assert address.as_uri().startswith(f"{storage_backend.scheme}://")


def test_objects_can_be_written_listed_and_deleted(storage_backend: StorageBackend) -> None:
    store = storage_backend.object_store()
    prefix = storage_backend.address("vector/districts")
    first = prefix.joined("versions", "v00001", "data.parquet")
    second = prefix.joined("current.json")
    obstore.put(store, first.key, b"first")
    obstore.put(store, second.key, b"second")

    assert storage_backend.exists(first) is True
    assert storage_backend.list_keys(prefix) == sorted([first.key, second.key])
    assert storage_backend.delete_prefix(prefix) == 2
    assert storage_backend.exists(first) is False
    assert storage_backend.list_keys(prefix) == []


def test_listing_is_bounded_by_path_segments(storage_backend: StorageBackend) -> None:
    store = storage_backend.object_store()
    wanted = storage_backend.address("vector/one", "current.json")
    other = storage_backend.address("vector/one-extra", "current.json")
    obstore.put(store, wanted.key, b"wanted")
    obstore.put(store, other.key, b"other")

    assert storage_backend.list_keys(storage_backend.address("vector/one")) == [wanted.key]


def test_missing_objects_do_not_exist(storage_backend: StorageBackend) -> None:
    assert storage_backend.exists(storage_backend.address("catalog/datasets", "absent.json")) is False


def test_icechunk_storage_is_resolved(storage_backend: StorageBackend) -> None:
    storage = storage_backend.icechunk_storage(storage_backend.address("raster/one"))

    assert isinstance(storage, icechunk.Storage)


def test_describe_reports_the_backend(storage_backend: StorageBackend) -> None:
    description = storage_backend.describe()

    assert description.scheme is storage_backend.scheme
    assert description.root == storage_backend.root
    assert description.base_prefix == storage_backend.base_prefix
    assert description.available is True


def test_memory_backend_reuses_one_icechunk_storage_per_key() -> None:
    backend = MemoryStorageBackend()
    address = backend.address("raster/one")

    assert backend.icechunk_storage(address) is backend.icechunk_storage(address)
    assert backend.icechunk_storage(address) is not backend.icechunk_storage(backend.address("raster/two"))


def test_memory_backend_delete_drops_the_icechunk_storages_it_cached() -> None:
    backend = MemoryStorageBackend()
    kept = backend.icechunk_storage(backend.address("raster/kept"))
    deleted = backend.icechunk_storage(backend.address("raster/deleted"))

    assert backend.delete_prefix(backend.address("raster/deleted")) == 1
    assert backend.icechunk_storage(backend.address("raster/deleted")) is not deleted
    assert backend.icechunk_storage(backend.address("raster/kept")) is kept


def test_memory_backend_has_no_parquet_filesystem() -> None:
    backend = MemoryStorageBackend()

    assert backend.parquet_filesystem() is None
    assert backend.supports_parquet_filesystem is False
    assert backend.parquet_path(backend.address("vector/one", "data.parquet")) == "ocs/vector/one/data.parquet"


def test_filesystem_backend_uses_the_local_parquet_filesystem(tmp_path: Path) -> None:
    backend = FilesystemStorageBackend(tmp_path / "data")

    assert isinstance(backend.parquet_filesystem(), pyarrow.fs.LocalFileSystem)
    assert backend.parquet_path(backend.address("vector/one", "data.parquet")) == str(
        Path(backend.root) / "ocs/vector/one/data.parquet"
    )
    assert Path(backend.root).is_dir()


def test_s3_backend_describes_itself_without_secrets() -> None:
    description = build_s3_backend().describe()

    assert description.scheme is StorageScheme.S3
    assert description.root == "ocs-exploration"
    assert description.available is True
    assert description.supports_parquet_filesystem is True
    assert description.details["bucket"] == "ocs-exploration"
    assert description.details["region"] == "eu-north-1"
    assert description.details["endpoint_url"] == "http://localhost:9000"
    assert description.details["addressing_style"] == "path"
    assert description.details["allow_http"] == "true"
    assert description.details["has_credentials"] == "true"
    assert SECRET_VALUE not in description.model_dump_json()


def test_s3_backend_reports_its_client_bounds_in_its_description() -> None:
    description = build_s3_backend().describe()

    assert description.details["connect_timeout_seconds"] == "5.0"
    assert description.details["request_timeout_seconds"] == "30.0"
    assert description.details["max_retries"] == "3"


def test_the_client_bounds_reach_all_three_clients() -> None:
    backend = S3StorageBackend(
        bucket="ocs-exploration",
        connect_timeout_seconds=2.0,
        request_timeout_seconds=4.0,
        max_retries=2,
        retry_backoff_seconds=0.25,
    )

    client_options = backend._client_options()
    retry_config = backend._retry_config()
    pyarrow_options = backend._pyarrow_options()
    repository_config = backend.repository_config()
    storage = repository_config.storage
    assert storage is not None
    timeouts = storage.timeouts
    retries = storage.retries
    assert timeouts is not None
    assert retries is not None

    # One budget for the whole request: four seconds per attempt, three attempts, two backoffs.
    assert backend.retry_budget_seconds == pytest.approx(12.5)
    assert client_options.get("connect_timeout") == timedelta(seconds=2.0)
    assert client_options.get("timeout") == timedelta(seconds=4.0)
    assert retry_config.get("max_retries") == 2
    assert retry_config.get("retry_timeout") == timedelta(seconds=12.5)
    assert retry_config.get("backoff") == {
        "init_backoff": timedelta(seconds=0.25),
        "max_backoff": timedelta(seconds=1.0),
        "base": 2,
    }
    assert pyarrow_options["connect_timeout"] == 2.0
    assert pyarrow_options["request_timeout"] == 4.0
    assert timeouts.connect_timeout_ms == 2000
    assert timeouts.read_timeout_ms == 4000
    assert timeouts.operation_timeout_ms == 12500
    assert timeouts.operation_attempt_timeout_ms == 4000
    # Icechunk counts tries including the first one, obstore counts retries after it.
    assert retries.max_tries == 3
    assert retries.initial_backoff_ms == 250
    assert retries.max_backoff_ms == 1000
    # Never disabled, whatever the timeouts are.
    assert storage.unsafe_use_conditional_create is not False
    assert storage.unsafe_use_conditional_update is not False


def test_only_the_s3_backend_imposes_a_repository_config(tmp_path: Path) -> None:
    assert FilesystemStorageBackend(directory=tmp_path / "data").repository_config() is None
    assert MemoryStorageBackend().repository_config() is None
    assert isinstance(build_s3_backend().repository_config(), icechunk.RepositoryConfig)


def test_s3_backend_builds_and_caches_its_handles_without_reaching_the_endpoint() -> None:
    backend = build_s3_backend()
    address = backend.address("vector/one", "data.parquet")

    assert isinstance(backend.icechunk_storage(backend.address("raster/one")), icechunk.Storage)
    assert isinstance(backend.object_store(), S3Store)
    assert backend.object_store() is backend.object_store()
    assert isinstance(backend.parquet_filesystem(), pyarrow.fs.S3FileSystem)
    assert backend.parquet_filesystem() is backend.parquet_filesystem()
    assert backend.parquet_path(address) == "ocs-exploration/ocs/vector/one/data.parquet"


def test_s3_backend_needs_an_object_storage_block() -> None:
    with pytest.raises(BackendNotSupportedError):
        S3StorageBackend.from_settings(Settings(backend=StorageScheme.S3))


def test_s3_backend_is_built_from_settings() -> None:
    settings = Settings(
        backend=StorageScheme.S3,
        s3=ObjectStorageSettings(bucket="ocs-exploration", prefix="exploration"),
    )

    backend = backend_for_scheme(default_plugin_manager(), settings, settings.backend)

    assert backend.scheme is StorageScheme.S3
    assert backend.root == "ocs-exploration"
    assert backend.base_prefix == "exploration"


def test_every_built_in_scheme_is_provided_by_a_plugin() -> None:
    assert provided_schemes(default_plugin_manager()) == (StorageScheme.FILE, StorageScheme.MEMORY, StorageScheme.S3)


def test_the_plugins_build_the_backend_named_by_the_settings(settings: Settings) -> None:
    backend = backend_for_scheme(default_plugin_manager(), settings, settings.backend)

    assert backend.scheme is settings.backend
