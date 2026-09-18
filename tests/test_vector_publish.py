"""Tests for publishing, rolling back and deleting versioned vector collections."""

from __future__ import annotations

import geopandas
import pytest

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.addresses import StorageAddress
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import (
    DatasetAlreadyExistsError,
    DatasetNotFoundError,
    NothingToPublishError,
    SnapshotNotFoundError,
)
from ocs_storage_exploration.storage.keys import vector_data_key, vector_pointer_key, vector_version_prefix
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.schemas import DatasetLifecycle, FeatureDataset, ItemType
from ocs_storage_exploration.storage.vector.collection import VectorCollectionStore

COLLECTION = "districts"


def collection_prefix(catalog: ObjectCatalog) -> str:
    """Return the storage prefix of the generation the catalog record of the collection names."""
    return catalog.require(COLLECTION).storage_key


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


def test_publish_flips_the_pointer(
    two_versions: VectorCollectionStore, storage_backend: StorageBackend, catalog: ObjectCatalog
) -> None:
    result = two_versions.publish(COLLECTION)

    assert result.item_type is ItemType.FEATURE
    assert result.published is True
    assert result.version == 2
    assert result.previous_version is None
    assert two_versions.current_version(COLLECTION) == 2
    assert storage_backend.exists(storage_backend.address(vector_pointer_key(collection_prefix(catalog))))


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
    two_versions: VectorCollectionStore, storage_backend: StorageBackend, catalog: ObjectCatalog
) -> None:
    two_versions.publish(COLLECTION, version=2)

    rollback = two_versions.publish(COLLECTION, version=1)

    assert rollback.version == 1
    assert rollback.previous_version == 2
    assert two_versions.current_version(COLLECTION) == 1
    assert two_versions.read(COLLECTION).version == 1
    # Rolling back moves a pointer; it never removes the version that was published before.
    assert storage_backend.exists(storage_backend.address(vector_data_key(collection_prefix(catalog), 2)))


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
    storage_backend.delete_prefix(storage_backend.address(vector_version_prefix(collection_prefix(catalog), 1)))

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


def test_delete_marks_the_record_before_it_sweeps_the_objects(
    two_versions: VectorCollectionStore,
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, FeatureDataset | None] = {}
    sweep = storage_backend.delete_prefix

    def watching_sweep(address: StorageAddress) -> int:
        record = catalog.get(COLLECTION)
        observed["record"] = record if isinstance(record, FeatureDataset) else None
        return sweep(address)

    monkeypatch.setattr(storage_backend, "delete_prefix", watching_sweep)

    two_versions.delete(COLLECTION)

    # The record outlives the sweep, so a writer that arrives in between is told a deletion is running
    # instead of finding no record at all and writing into a prefix that is about to be emptied.
    marked = observed["record"]
    assert marked is not None
    assert marked.lifecycle is DatasetLifecycle.DELETING
    assert marked.is_deleting is True
    assert catalog.get(COLLECTION) is None


def mark_deleting(catalog: ObjectCatalog) -> None:
    """Leave the collection in the state a deletion that stopped before its sweep leaves behind."""
    entry = catalog.require_entry(COLLECTION)
    catalog.put(entry.record.model_copy(update={"lifecycle": DatasetLifecycle.DELETING}), revision=entry.revision)


def test_a_write_takes_over_a_deletion_that_stopped_before_its_sweep(
    two_versions: VectorCollectionStore,
    catalog: ObjectCatalog,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    mark_deleting(catalog)

    result = two_versions.write(COLLECTION, sample_features.iloc[:3], identifier_property="id")

    # The prefix was swept first, so the new collection starts at version 1 and serves none of the
    # versions the dead one left behind.
    assert result.version == 1
    assert two_versions.versions(COLLECTION) == [1]
    assert len(two_versions.read(COLLECTION).frame) == 3
    record = catalog.get(COLLECTION)
    assert record is not None
    assert record.is_deleting is False


def test_a_second_delete_finishes_a_deletion_that_stopped_before_its_sweep(
    two_versions: VectorCollectionStore,
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
) -> None:
    mark_deleting(catalog)

    removed = two_versions.delete(COLLECTION)

    assert removed >= 3
    assert catalog.get(COLLECTION) is None
    assert storage_backend.list_keys(storage_backend.address("vector", COLLECTION)) == []


def test_delete_leaves_a_record_that_was_reclaimed_while_it_swept_alone(
    two_versions: VectorCollectionStore,
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    sample_features: geopandas.GeoDataFrame,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reclaiming = VectorCollectionStore(storage_backend, catalog, settings)
    sweep = storage_backend.delete_prefix

    def reclaimed_sweep(address: StorageAddress) -> int:
        monkeypatch.setattr(storage_backend, "delete_prefix", sweep)
        removed = sweep(address)
        # Another writer takes the deletion over and writes the collection again under the same name.
        reclaiming.write(COLLECTION, sample_features.iloc[:2], identifier_property="id")
        return removed

    monkeypatch.setattr(storage_backend, "delete_prefix", reclaimed_sweep)

    two_versions.delete(COLLECTION)

    record = catalog.get(COLLECTION)
    assert record is not None
    assert record.is_deleting is False
    assert reclaiming.versions(COLLECTION) == [1]


def test_two_writes_taking_over_the_same_deletion_meet_at_the_conditional_create(
    two_versions: VectorCollectionStore,
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    sample_features: geopandas.GeoDataFrame,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mark_deleting(catalog)
    competitor = VectorCollectionStore(storage_backend, catalog, settings)
    sweep = storage_backend.delete_prefix

    def overtaken_sweep(address: StorageAddress) -> int:
        monkeypatch.setattr(storage_backend, "delete_prefix", sweep)
        removed = sweep(address)
        # The other writer finishes the whole take-over while this one is still sweeping.
        competitor.write(COLLECTION, sample_features.iloc[:2], identifier_property="id")
        return removed

    monkeypatch.setattr(storage_backend, "delete_prefix", overtaken_sweep)

    with pytest.raises(DatasetAlreadyExistsError):
        two_versions.write(COLLECTION, sample_features.iloc[:3], identifier_property="id")

    # The record is the winner's, and the loser swept the generation it minted rather than leaving
    # its objects under the identifier forever: nothing stands outside the prefix the record names.
    record = catalog.get(COLLECTION)
    assert isinstance(record, FeatureDataset)
    assert record.features.feature_count == 2
    assert len(competitor.read(COLLECTION, version=1).frame) == 2
    stored = storage_backend.list_keys(storage_backend.address("vector", COLLECTION))
    assert stored
    assert all(key.startswith(storage_backend.address(record.storage_key).key) for key in stored)


def test_a_collection_being_deleted_is_absent_for_reads_publications_and_listings(
    two_versions: VectorCollectionStore,
    catalog: ObjectCatalog,
) -> None:
    two_versions.publish(COLLECTION)
    mark_deleting(catalog)

    with pytest.raises(DatasetNotFoundError):
        two_versions.read(COLLECTION)
    with pytest.raises(DatasetNotFoundError):
        two_versions.publish(COLLECTION)

    assert catalog.list_datasets() == []
    # The raw record is still there, which is what lets a later delete or write finish the deletion.
    assert catalog.get(COLLECTION) is not None
