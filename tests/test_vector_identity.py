"""Tests for feature identity and coordinate reference system validation on vector writes."""

from __future__ import annotations

import geopandas
import pytest
import shapely

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import FeatureIdentityError
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.vector.collection import VectorCollectionStore, build_frame_from_geojson

COLLECTION = "districts"


def test_missing_identifier_property_lists_the_columns(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = VectorCollectionStore(storage_backend, catalog, settings)

    with pytest.raises(FeatureIdentityError) as failure:
        store.write(COLLECTION, sample_features, identifier_property="code")

    assert "'code'" in failure.value.message
    assert "level" in failure.value.message
    assert "path" in failure.value.message


def test_null_identifiers_name_the_rows(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    broken = sample_features.copy()
    broken.loc[broken.index[2], "id"] = None
    broken.loc[broken.index[5], "id"] = None
    store = VectorCollectionStore(storage_backend, catalog, settings)

    with pytest.raises(FeatureIdentityError) as failure:
        store.write(COLLECTION, broken, identifier_property="id")

    assert "[2, 5]" in failure.value.message


def test_duplicate_identifiers_name_the_values(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    broken = sample_features.copy()
    broken.loc[broken.index[4], "id"] = "square-0"
    store = VectorCollectionStore(storage_backend, catalog, settings)

    with pytest.raises(FeatureIdentityError) as failure:
        store.write(COLLECTION, broken, identifier_property="id")

    assert "square-0" in failure.value.message


def test_a_frame_without_a_crs_is_refused(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = VectorCollectionStore(storage_backend, catalog, settings)
    without_crs = geopandas.GeoDataFrame(
        sample_features.drop(columns=["geometry"]),
        geometry=geopandas.GeoSeries(sample_features.geometry.to_numpy()),
    )

    with pytest.raises(FeatureIdentityError) as failure:
        store.write(COLLECTION, without_crs, identifier_property="id")

    assert "coordinate reference system" in failure.value.message


def test_a_missing_geometry_is_refused(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings
) -> None:
    frame = geopandas.GeoDataFrame(
        {"id": ["a", "b"]},
        geometry=[shapely.Point(0.0, 0.0), shapely.Point(1.0, 1.0)],
        crs="EPSG:4326",
    )
    frame.loc[frame.index[1], "geometry"] = None
    store = VectorCollectionStore(storage_backend, catalog, settings)

    with pytest.raises(FeatureIdentityError):
        store.write(COLLECTION, frame, identifier_property="id")


def test_a_frame_without_a_geometry_column_is_refused(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings
) -> None:
    frame = geopandas.GeoDataFrame({"id": ["a", "b"]})
    store = VectorCollectionStore(storage_backend, catalog, settings)

    with pytest.raises(FeatureIdentityError):
        store.write(COLLECTION, frame, identifier_property="id")


def test_geojson_without_features_is_refused() -> None:
    with pytest.raises(FeatureIdentityError):
        build_frame_from_geojson({"type": "FeatureCollection", "features": []}, identifier_property="id")


def test_geojson_prefers_the_property_over_the_feature_id() -> None:
    feature_collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "ignored",
                "properties": {"code": "kept"},
                "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
            },
        ],
    }

    frame = build_frame_from_geojson(feature_collection, identifier_property="code")

    assert frame["code"].tolist() == ["kept"]
