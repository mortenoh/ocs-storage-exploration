"""Tests for the guard that refuses unqualified reads of a large vector collection."""

from __future__ import annotations

import geopandas
import pytest

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import FeatureCountGuardError
from ocs_storage_exploration.storage.models import BoundingBox
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.vector.collection import VectorCollectionStore

COLLECTION = "districts"
WHOLE_WORLD = BoundingBox(minimum_x=-180.0, minimum_y=-90.0, maximum_x=180.0, maximum_y=90.0)


@pytest.fixture
def guarded_store(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> VectorCollectionStore:
    guarded_settings = settings.model_copy(update={"max_unqualified_feature_count": 2})
    store = VectorCollectionStore(storage_backend, catalog, guarded_settings)
    store.write(
        COLLECTION,
        sample_features,
        identifier_property="id",
        selectable_columns=("level", "path"),
    )
    return store


def test_an_unqualified_read_is_refused_above_the_threshold(guarded_store: VectorCollectionStore) -> None:
    with pytest.raises(FeatureCountGuardError) as failure:
        guarded_store.read(COLLECTION)

    assert "12" in failure.value.message
    assert "2" in failure.value.message


def test_the_guard_reports_payload_too_large() -> None:
    assert FeatureCountGuardError.status_code == 413


def test_a_bbox_qualifies_the_read(guarded_store: VectorCollectionStore) -> None:
    assert len(guarded_store.read(COLLECTION, bbox=WHOLE_WORLD).frame) == 12


def test_a_where_clause_qualifies_the_read(guarded_store: VectorCollectionStore) -> None:
    assert len(guarded_store.read(COLLECTION, where={"level": "1"}).frame) == 3


def test_a_small_collection_reads_without_a_qualifier(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    guarded_settings = settings.model_copy(update={"max_unqualified_feature_count": 12})
    store = VectorCollectionStore(storage_backend, catalog, guarded_settings)
    store.write(COLLECTION, sample_features, identifier_property="id")

    assert len(store.read(COLLECTION).frame) == 12
