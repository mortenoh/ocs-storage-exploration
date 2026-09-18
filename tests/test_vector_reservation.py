"""Tests that a vector write reserves its version number instead of overwriting a published one."""

from __future__ import annotations

import geopandas
import obstore
import pytest

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import PublicationConflictError
from ocs_storage_exploration.storage.keys import (
    vector_data_key,
    vector_reservation_key,
    vector_version_metadata_key,
)
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.vector.collection import VectorCollectionStore

COLLECTION = "districts"


def object_bytes(storage_backend: StorageBackend, key: str) -> bytes:
    """Read one object of the backend as raw bytes."""
    return bytes(obstore.get(storage_backend.object_store(), storage_backend.address(key).key).bytes())


def build_store(storage_backend: StorageBackend, settings: Settings) -> VectorCollectionStore:
    """Build a collection store with a catalog of its own, as a second process would have."""
    return VectorCollectionStore(storage_backend, ObjectCatalog(storage_backend), settings)


def collection_prefix(storage_backend: StorageBackend) -> str:
    """Return the storage prefix of the generation the catalog record of the collection names."""
    return ObjectCatalog(storage_backend).require(COLLECTION).storage_key


def freeze_version_listing(store: VectorCollectionStore) -> None:
    """Freeze a store on the empty listing it saw before another writer finished version 1."""
    store._list_versions = lambda storage_prefix, *, completed_only: []  # type: ignore[method-assign]


def test_a_write_leaves_a_reservation_and_a_metadata_sidecar(
    storage_backend: StorageBackend, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = build_store(storage_backend, settings)

    store.write(COLLECTION, sample_features, identifier_property="id")

    prefix = collection_prefix(storage_backend)
    # The record is the only thing that says where the objects are, and everything sits below it.
    assert prefix.startswith(f"vector/{COLLECTION}/")
    for key in (
        vector_reservation_key(prefix, 1),
        vector_data_key(prefix, 1),
        vector_version_metadata_key(prefix, 1),
    ):
        assert storage_backend.exists(storage_backend.address(key)) is True


def test_a_racing_writer_lands_on_the_next_version_instead_of_overwriting(
    storage_backend: StorageBackend, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    writer = build_store(storage_backend, settings)
    racing = build_store(storage_backend, settings)
    writer.write(COLLECTION, sample_features, identifier_property="id", publish=True)
    prefix = collection_prefix(storage_backend)
    published = object_bytes(storage_backend, vector_data_key(prefix, 1))
    # Freeze the racing writer on the empty listing it saw before the first writer created version 1.
    freeze_version_listing(racing)

    result = racing.write(COLLECTION, sample_features.iloc[:5], identifier_property="id")

    assert result.version == 2
    assert object_bytes(storage_backend, vector_data_key(prefix, 1)) == published


def test_both_writers_keep_their_own_version(
    storage_backend: StorageBackend, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    writer = build_store(storage_backend, settings)
    racing = build_store(storage_backend, settings)
    writer.write(COLLECTION, sample_features, identifier_property="id")
    freeze_version_listing(racing)

    racing.write(COLLECTION, sample_features.iloc[:5], identifier_property="id")
    del racing._list_versions

    assert racing.versions(COLLECTION) == [1, 2]
    assert len(writer.read(COLLECTION, version=1).frame) == 12
    assert len(writer.read(COLLECTION, version=2).frame) == 5


def test_a_reserved_version_is_never_handed_out_twice(
    storage_backend: StorageBackend, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = build_store(storage_backend, settings)
    store.write(COLLECTION, sample_features, identifier_property="id")

    reserved = [store.reserve_version(COLLECTION) for _ in range(3)]

    assert reserved == [2, 3, 4]


def test_a_data_object_left_by_a_crashed_write_is_never_overwritten(
    storage_backend: StorageBackend, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = build_store(storage_backend, settings)
    store.write(COLLECTION, sample_features, identifier_property="id")
    prefix = collection_prefix(storage_backend)
    orphan_key = storage_backend.address(vector_data_key(prefix, 2)).key
    obstore.put(storage_backend.object_store(), orphan_key, b"not really parquet")

    result = store.write(COLLECTION, sample_features.iloc[:5], identifier_property="id")

    assert result.version == 3
    assert object_bytes(storage_backend, vector_data_key(prefix, 2)) == b"not really parquet"


def test_an_exhausted_reservation_budget_is_reported_as_a_conflict(
    storage_backend: StorageBackend, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = build_store(storage_backend, settings)
    store.write(COLLECTION, sample_features, identifier_property="id")
    freeze_version_listing(store)

    with pytest.raises(PublicationConflictError):
        store.reserve_version(COLLECTION, attempts=1)


def test_an_unfinished_version_is_claimed_but_not_listed_as_written(
    storage_backend: StorageBackend, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = build_store(storage_backend, settings)
    store.write(COLLECTION, sample_features, identifier_property="id")

    reserved = store.reserve_version(COLLECTION)

    assert reserved == 2
    assert store.versions(COLLECTION) == [1]
    assert store.claimed_versions(COLLECTION) == [1, 2]
    # A reservation nobody finished is never published and never read.
    assert store.publish(COLLECTION).version == 1
    assert store.read(COLLECTION).version == 1
