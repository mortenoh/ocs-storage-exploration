"""Tests for publishing, rolling back and deleting versioned vector collections."""

from __future__ import annotations

import geopandas
import pytest

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import (
    DatasetNotFoundError,
    NothingToPublishError,
    SnapshotNotFoundError,
)
from ocs_storage_exploration.storage.keys import vector_data_key, vector_pointer_key, vector_version_prefix
from ocs_storage_exploration.storage.models import FeatureDataset, ItemType
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.vector.collection import VectorCollectionStore

COLLECTION = "districts"


@pytest.fixture
def two_versions(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> VectorCollectionStore:
    store = VectorCollectionStore(storage_backend, catalog, settings)
    store.write(COLLECTION, sample_features, identifier_property="id")
    store.write(COLLECTION, sample_features.iloc[:5], identifier_property="id")
    return store


def test_two_writes_produce_two_versions(two_versions: VectorCollectionStore) -> None:
    assert two_versions.versions(COLLECTION) == [1, 2]


def test_read_before_publish_sees_the_latest_written_version(two_versions: VectorCollectionStore) -> None:
    handle = two_versions.read(COLLECTION)

    assert handle.version == 2
    assert len(handle.frame) == 5
    assert two_versions.current_version(COLLECTION) is None


def test_publish_flips_the_pointer(two_versions: VectorCollectionStore, storage_backend: StorageBackend) -> None:
    result = two_versions.publish(COLLECTION)

    assert result.item_type is ItemType.FEATURE
    assert result.published is True
    assert result.version == 2
    assert result.previous_version is None
    assert two_versions.current_version(COLLECTION) == 2
    assert storage_backend.exists(storage_backend.address(vector_pointer_key(COLLECTION)))


def test_publish_records_the_publication_on_the_catalog_record(
    two_versions: VectorCollectionStore, catalog: ObjectCatalog
) -> None:
    two_versions.publish(COLLECTION, version=1)

    record = catalog.require(COLLECTION)

    assert isinstance(record, FeatureDataset)
    assert record.publication.published is True
    assert record.publication.version == 1
    assert record.publication.published_at is not None


def test_read_after_publish_follows_the_pointer(two_versions: VectorCollectionStore) -> None:
    two_versions.publish(COLLECTION, version=1)

    handle = two_versions.read(COLLECTION)

    assert handle.version == 1
    assert len(handle.frame) == 12


def test_rollback_is_a_publish_of_an_older_version(
    two_versions: VectorCollectionStore, storage_backend: StorageBackend
) -> None:
    two_versions.publish(COLLECTION, version=2)

    rollback = two_versions.publish(COLLECTION, version=1)

    assert rollback.version == 1
    assert rollback.previous_version == 2
    assert two_versions.current_version(COLLECTION) == 1
    assert two_versions.read(COLLECTION).version == 1
    # Rolling back moves a pointer; it never removes the version that was published before.
    assert storage_backend.exists(storage_backend.address(vector_data_key(COLLECTION, 2)))


def test_republishing_the_same_version_changes_nothing(two_versions: VectorCollectionStore) -> None:
    two_versions.publish(COLLECTION, version=2)

    repeated = two_versions.publish(COLLECTION, version=2)

    assert repeated.version == 2
    # An unchanged publication is the one whose previous version is the version it points at.
    assert repeated.previous_version == repeated.version
    assert two_versions.current_version(COLLECTION) == 2


def test_an_explicit_version_still_wins_over_the_pointer(two_versions: VectorCollectionStore) -> None:
    two_versions.publish(COLLECTION, version=1)

    assert two_versions.read(COLLECTION, version=2).version == 2


def test_publish_refuses_an_unknown_version(two_versions: VectorCollectionStore) -> None:
    with pytest.raises(SnapshotNotFoundError):
        two_versions.publish(COLLECTION, version=9)


def test_publish_refuses_a_collection_with_no_version(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = VectorCollectionStore(storage_backend, catalog, settings)
    store.write(COLLECTION, sample_features, identifier_property="id")
    storage_backend.delete_prefix(storage_backend.address(vector_version_prefix(COLLECTION, 1)))

    with pytest.raises(NothingToPublishError):
        store.publish(COLLECTION)


def test_write_can_publish_immediately(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = VectorCollectionStore(storage_backend, catalog, settings)

    result = store.write(COLLECTION, sample_features, identifier_property="id", publish=True)

    assert result.published is True
    assert store.current_version(COLLECTION) == 1


def test_delete_removes_the_record_and_every_object(
    two_versions: VectorCollectionStore, storage_backend: StorageBackend, catalog: ObjectCatalog
) -> None:
    two_versions.publish(COLLECTION)

    removed = two_versions.delete(COLLECTION)

    assert removed >= 3
    assert catalog.get(COLLECTION) is None
    assert storage_backend.list_keys(storage_backend.address("vector", COLLECTION)) == []


def test_delete_refuses_an_unknown_collection(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings
) -> None:
    store = VectorCollectionStore(storage_backend, catalog, settings)

    with pytest.raises(DatasetNotFoundError):
        store.delete(COLLECTION)
