"""Tests of the S3 backend itself against a live S3-compatible endpoint."""

from __future__ import annotations

import icechunk
import obstore
import pyarrow.fs
import pyarrow.parquet
import pytest
from obstore.store import S3Store

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.addresses import StorageScheme
from ocs_storage_exploration.storage.protocols import StorageBackend

pytestmark = pytest.mark.s3


def test_the_backend_is_available_and_serves_the_configured_bucket(
    live_s3_backend: StorageBackend, live_s3_settings: Settings
) -> None:
    assert live_s3_backend.scheme is StorageScheme.S3
    assert live_s3_settings.s3 is not None
    assert live_s3_backend.root == live_s3_settings.s3.bucket
    assert live_s3_backend.base_prefix == live_s3_settings.base_prefix
    assert live_s3_backend.describe().available is True
    assert live_s3_backend.describe().supports_parquet_filesystem is True


def test_icechunk_storage_is_a_real_storage_rooted_at_the_address(live_s3_backend: StorageBackend) -> None:
    storage = live_s3_backend.icechunk_storage(live_s3_backend.address("raster", "one"))

    assert isinstance(storage, icechunk.Storage)
    repository = icechunk.Repository.open_or_create(storage)
    assert "main" in repository.list_branches()


def test_objects_round_trip_through_the_object_store(live_s3_backend: StorageBackend) -> None:
    store = live_s3_backend.object_store()
    assert isinstance(store, S3Store)
    prefix = live_s3_backend.address("vector", "districts")
    first = prefix.joined("versions", "v00001", "data.parquet")
    second = prefix.joined("current.json")

    obstore.put(store, first.key, b"first")
    obstore.put(store, second.key, b"second")

    assert bytes(obstore.get(store, first.key).bytes()) == b"first"
    assert live_s3_backend.exists(first) is True
    assert live_s3_backend.list_keys(prefix) == sorted([first.key, second.key])
    assert live_s3_backend.delete_prefix(prefix) == 2
    assert live_s3_backend.exists(first) is False
    assert live_s3_backend.list_keys(prefix) == []


def test_a_missing_object_is_reported_rather_than_raised(live_s3_backend: StorageBackend) -> None:
    assert live_s3_backend.exists(live_s3_backend.address("catalog/datasets", "absent.json")) is False
    assert live_s3_backend.delete_prefix(live_s3_backend.address("raster", "absent")) == 0


def test_parquet_is_written_and_read_through_the_pyarrow_filesystem(live_s3_backend: StorageBackend) -> None:
    filesystem = live_s3_backend.parquet_filesystem()
    assert isinstance(filesystem, pyarrow.fs.S3FileSystem)
    address = live_s3_backend.address("vector", "districts", "versions", "v00001", "data.parquet")
    path = live_s3_backend.parquet_path(address)
    assert path == f"{live_s3_backend.root}/{address.key}"
    table = pyarrow.table({"id": ["a", "b", "c"]})

    pyarrow.parquet.write_table(table, path, filesystem=filesystem)

    assert pyarrow.parquet.read_table(path, filesystem=filesystem).column("id").to_pylist() == ["a", "b", "c"]
    assert live_s3_backend.exists(address) is True
    assert live_s3_backend.list_keys(address.parent()) == [address.key]


def test_the_description_names_the_endpoint_without_any_secret(
    live_s3_backend: StorageBackend, live_s3_settings: Settings
) -> None:
    assert live_s3_settings.s3 is not None
    secret = live_s3_settings.s3.secret_access_key
    assert secret is not None
    description = live_s3_backend.describe()

    assert description.details["endpoint_url"] == live_s3_settings.s3.endpoint_url
    assert description.details["region"] == live_s3_settings.s3.region
    assert description.details["addressing_style"] == "path"
    assert description.details["allow_http"] == "true"
    assert description.details["has_credentials"] == "true"
    assert secret.get_secret_value() not in description.model_dump_json()
