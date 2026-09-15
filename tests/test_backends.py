"""Tests for the storage backends and the backend registry."""

from __future__ import annotations

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
)
from ocs_storage_exploration.storage.errors import BackendNotSupportedError, StorageError
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.registry import (
    build_backend,
    build_backend_from_dotted_path,
    registered_schemes,
)

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
    assert address.as_uri().startswith(f"{storage_backend.scheme.value}://")


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

    backend = build_backend(settings)

    assert backend.scheme is StorageScheme.S3
    assert backend.root == "ocs-exploration"
    assert backend.base_prefix == "exploration"


def test_every_scheme_has_a_registered_factory() -> None:
    assert registered_schemes() == (StorageScheme.FILE, StorageScheme.MEMORY, StorageScheme.S3)


def test_registry_builds_the_backend_named_by_the_settings(settings: Settings) -> None:
    backend = build_backend(settings)

    assert backend.scheme is settings.backend


def test_dotted_path_loading_filters_unknown_parameters() -> None:
    backend = build_backend_from_dotted_path(
        "ocs_storage_exploration.storage.backends.memory.MemoryStorageBackend",
        {"base_prefix": "exploration", "unknown_parameter": 1},
    )

    assert isinstance(backend, MemoryStorageBackend)
    assert backend.base_prefix == "exploration"


def test_dotted_path_loading_rejects_other_classes() -> None:
    with pytest.raises(StorageError):
        build_backend_from_dotted_path("ocs_storage_exploration.settings.Settings", {})


def test_dotted_path_loading_reports_bad_paths() -> None:
    with pytest.raises(StorageError):
        build_backend_from_dotted_path("memory", {})
    with pytest.raises(StorageError):
        build_backend_from_dotted_path("ocs_storage_exploration.storage.backends.memory.Missing", {})
    with pytest.raises(StorageError):
        build_backend_from_dotted_path("ocs_storage_exploration.absent.Thing", {})
