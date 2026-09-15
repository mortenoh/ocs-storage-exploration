"""Tests that a read resolves its metadata from the version it selected rather than from the catalog record."""

from __future__ import annotations

import geopandas
import pytest

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import (
    FeatureCountGuardError,
    SelectableColumnError,
    SnapshotNotFoundError,
)
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.schemas import BoundingBox, VectorVersionMetadata
from ocs_storage_exploration.storage.vector.collection import VectorCollectionStore

COLLECTION = "districts"
ONE_SQUARE = BoundingBox(minimum_x=0.2, minimum_y=0.2, maximum_x=0.8, maximum_y=0.8)


@pytest.fixture
def store(storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings) -> VectorCollectionStore:
    """Build a collection store on the fixture backend."""
    return VectorCollectionStore(storage_backend, catalog, settings)


def test_a_published_read_keeps_the_crs_of_the_published_version(
    store: VectorCollectionStore, sample_features: geopandas.GeoDataFrame
) -> None:
    store.write(COLLECTION, sample_features, identifier_property="id", publish=True)
    store.write(COLLECTION, sample_features.to_crs("EPSG:3857"), identifier_property="id")

    handle = store.read(COLLECTION, bbox=ONE_SQUARE)

    assert handle.version == 1
    assert handle.frame["id"].tolist() == ["square-0"]


def test_a_smaller_draft_does_not_bypass_the_feature_count_guard(
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    guarded = VectorCollectionStore(
        storage_backend, catalog, settings.model_copy(update={"max_unqualified_feature_count": 2})
    )
    guarded.write(COLLECTION, sample_features, identifier_property="id", publish=True)
    guarded.write(COLLECTION, sample_features.iloc[:2], identifier_property="id")

    with pytest.raises(FeatureCountGuardError) as failure:
        guarded.read(COLLECTION)

    assert "12" in failure.value.message


def test_a_draft_that_declares_no_selectable_column_does_not_disarm_the_published_one(
    store: VectorCollectionStore, sample_features: geopandas.GeoDataFrame
) -> None:
    store.write(COLLECTION, sample_features, identifier_property="id", selectable_columns=("level",), publish=True)
    store.write(COLLECTION, sample_features.iloc[:5], identifier_property="id")

    handle = store.read(COLLECTION, where={"level": "1"})

    assert handle.version == 1
    assert sorted(handle.frame["id"]) == ["square-0", "square-1", "square-2"]


def test_a_draft_that_renames_the_identifier_does_not_break_the_published_projection(
    store: VectorCollectionStore, sample_features: geopandas.GeoDataFrame
) -> None:
    store.write(COLLECTION, sample_features, identifier_property="id", selectable_columns=("level",), publish=True)
    store.write(COLLECTION, sample_features.rename(columns={"id": "code"}), identifier_property="code")

    handle = store.read(COLLECTION, columns=["level"], where={"level": "4"})

    assert handle.version == 1
    assert sorted(handle.frame.columns) == ["geometry", "id", "level"]


def test_version_metadata_describes_the_version_it_was_written_with(
    store: VectorCollectionStore, sample_features: geopandas.GeoDataFrame
) -> None:
    store.write(
        COLLECTION,
        sample_features,
        identifier_property="id",
        selectable_columns=("level", "path"),
        license="CC-BY-4.0",
        attribution="Statistics Norway",
    )
    store.write(COLLECTION, sample_features.iloc[:5], identifier_property="id")

    first = store.version_metadata(COLLECTION, 1)
    second = store.version_metadata(COLLECTION, 2)

    assert isinstance(first, VectorVersionMetadata)
    assert first.version == 1
    assert first.crs == "EPSG:4326"
    assert first.feature_count == 12
    assert first.identifier_property == "id"
    assert first.primary_geometry == "geometry"
    assert first.geometry_types == ("Point", "Polygon")
    assert first.selectable_columns == ("level", "path")
    assert first.bbox is not None
    assert first.license == "CC-BY-4.0"
    assert first.attribution == "Statistics Norway"
    assert second.feature_count == 5
    assert second.selectable_columns == ()


def test_published_metadata_follows_the_pointer(
    store: VectorCollectionStore, sample_features: geopandas.GeoDataFrame
) -> None:
    store.write(COLLECTION, sample_features, identifier_property="id", publish=True)
    store.write(COLLECTION, sample_features.iloc[:5], identifier_property="id")

    published = store.published_metadata(COLLECTION)

    assert published is not None
    assert published.version == 1
    assert published.feature_count == 12


def test_published_metadata_is_absent_while_nothing_is_published(
    store: VectorCollectionStore, sample_features: geopandas.GeoDataFrame
) -> None:
    store.write(COLLECTION, sample_features, identifier_property="id")

    assert store.published_metadata(COLLECTION) is None


def test_version_metadata_refuses_a_version_that_was_never_written(
    store: VectorCollectionStore, sample_features: geopandas.GeoDataFrame
) -> None:
    store.write(COLLECTION, sample_features, identifier_property="id")

    with pytest.raises(SnapshotNotFoundError):
        store.version_metadata(COLLECTION, 7)


def test_the_pointer_counts_the_features_of_the_version_it_names(
    store: VectorCollectionStore, sample_features: geopandas.GeoDataFrame
) -> None:
    store.write(COLLECTION, sample_features, identifier_property="id")
    store.write(COLLECTION, sample_features.iloc[:5], identifier_property="id")

    store.publish(COLLECTION, version=1)
    pointer = store.pointer(COLLECTION)

    assert pointer is not None
    assert pointer.version == 1
    assert pointer.feature_count == 12


def test_an_undeclared_column_is_still_refused_on_the_published_version(
    store: VectorCollectionStore, sample_features: geopandas.GeoDataFrame
) -> None:
    store.write(COLLECTION, sample_features, identifier_property="id", publish=True)
    store.write(COLLECTION, sample_features.iloc[:5], identifier_property="id", selectable_columns=("level",))

    with pytest.raises(SelectableColumnError):
        store.read(COLLECTION, where={"level": "1"})
