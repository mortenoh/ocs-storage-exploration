"""Tests for envelope, clause, projection and limit handling on vector reads."""

from __future__ import annotations

import geopandas
import pytest
import shapely

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import SelectableColumnError, SnapshotNotFoundError
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.schemas import BoundingBox
from ocs_storage_exploration.storage.vector.collection import VectorCollectionStore

COLLECTION = "districts"
# A window entirely inside the notch of the horseshoe, which its envelope still covers.
NOTCH = BoundingBox(minimum_x=11.2, minimum_y=1.2, maximum_x=11.8, maximum_y=2.8)


@pytest.fixture
def store(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> VectorCollectionStore:
    collection_store = VectorCollectionStore(storage_backend, catalog, settings)
    collection_store.write(
        COLLECTION,
        sample_features,
        identifier_property="id",
        selectable_columns=("level", "path"),
    )
    return collection_store


def test_bbox_read_excludes_the_horseshoe_when_the_window_is_in_its_notch(store: VectorCollectionStore) -> None:
    handle = store.read(COLLECTION, bbox=NOTCH)

    assert handle.frame.empty


def test_bbox_read_includes_the_horseshoe_when_the_window_touches_a_prong(store: VectorCollectionStore) -> None:
    window = BoundingBox(minimum_x=10.2, minimum_y=1.2, maximum_x=10.8, maximum_y=2.8)

    handle = store.read(COLLECTION, bbox=window)

    assert handle.frame["id"].tolist() == ["horseshoe"]


def test_bbox_read_selects_one_grid_square(store: VectorCollectionStore) -> None:
    window = BoundingBox(minimum_x=0.2, minimum_y=0.2, maximum_x=0.8, maximum_y=0.8)

    handle = store.read(COLLECTION, bbox=window)

    assert handle.frame["id"].tolist() == ["square-0"]


def test_bbox_in_web_mercator_is_reprojected_and_still_matches(store: VectorCollectionStore) -> None:
    window = BoundingBox(minimum_x=0.2, minimum_y=0.2, maximum_x=0.8, maximum_y=0.8)
    projected = geopandas.GeoSeries([shapely.box(*window.as_tuple())], crs="EPSG:4326").to_crs("EPSG:3857")
    bounds = projected.total_bounds

    handle = store.read(
        COLLECTION,
        bbox=BoundingBox.from_sequence((bounds[0], bounds[1], bounds[2], bounds[3])),
        bbox_crs="EPSG:3857",
    )

    assert handle.frame["id"].tolist() == ["square-0"]


def test_reprojected_bbox_also_excludes_the_notch(store: VectorCollectionStore) -> None:
    projected = geopandas.GeoSeries([shapely.box(*NOTCH.as_tuple())], crs="EPSG:4326").to_crs("EPSG:3857")
    bounds = projected.total_bounds

    handle = store.read(
        COLLECTION,
        bbox=BoundingBox.from_sequence((bounds[0], bounds[1], bounds[2], bounds[3])),
        bbox_crs="EPSG:3857",
    )

    assert handle.frame.empty


def test_where_filters_on_a_selectable_column(store: VectorCollectionStore) -> None:
    handle = store.read(COLLECTION, where={"level": "2"})

    assert sorted(handle.frame["id"]) == ["square-3", "square-4", "square-5"]


def test_where_coerces_the_value_to_the_column_type(store: VectorCollectionStore) -> None:
    assert store.read(COLLECTION, where={"level": 4}).frame["id"].tolist() == ["horseshoe"]


def test_where_matches_a_path_prefix(store: VectorCollectionStore) -> None:
    handle = store.read(COLLECTION, where={"path": "/root/a*"})

    assert sorted(handle.frame["id"]) == [
        "point-0",
        "point-1",
        "square-0",
        "square-1",
        "square-2",
        "square-3",
        "square-4",
        "square-5",
    ]


def test_where_matches_an_exact_path(store: VectorCollectionStore) -> None:
    handle = store.read(COLLECTION, where={"path": "/root/d/f"})

    assert handle.frame["id"].tolist() == ["horseshoe"]


def test_where_refuses_an_undeclared_column(store: VectorCollectionStore) -> None:
    with pytest.raises(SelectableColumnError):
        store.read(COLLECTION, where={"id": "horseshoe"})


def test_columns_still_returns_the_identifier_and_the_geometry(store: VectorCollectionStore) -> None:
    handle = store.read(COLLECTION, columns=["level"], where={"level": "4"})

    assert sorted(handle.frame.columns) == ["geometry", "id", "level"]
    assert handle.frame["id"].tolist() == ["horseshoe"]
    assert handle.frame.geometry.iloc[0] is not None


def test_columns_refuses_a_column_that_is_not_stored(store: VectorCollectionStore) -> None:
    with pytest.raises(SelectableColumnError):
        store.read(COLLECTION, columns=["missing"])


def test_limit_truncates_the_result(store: VectorCollectionStore) -> None:
    assert len(store.read(COLLECTION, limit=3).frame) == 3


def test_limit_applies_after_the_envelope_filter(store: VectorCollectionStore) -> None:
    window = BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=3.0, maximum_y=3.0)

    handle = store.read(COLLECTION, bbox=window, limit=2)

    assert len(handle.frame) == 2


def test_read_refuses_an_unknown_version(store: VectorCollectionStore) -> None:
    with pytest.raises(SnapshotNotFoundError):
        store.read(COLLECTION, version=7)
