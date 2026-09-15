"""Tests writing a vector collection and reading it back over both backends."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import geopandas
import obstore
import pyarrow
import pyarrow.parquet
import pytest

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import ItemTypeMismatchError, SelectableColumnError
from ocs_storage_exploration.storage.keys import vector_data_key
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.schemas import (
    BoundingBox,
    CoverageDataset,
    FeatureDataset,
    GridSpecification,
    ItemType,
    StorageFormat,
    TemporalExtent,
)
from ocs_storage_exploration.storage.vector.collection import VectorCollectionStore

COLLECTION = "districts"


def read_parquet_footer(
    storage_backend: StorageBackend, identifier: str = COLLECTION, version: int = 1
) -> pyarrow.parquet.ParquetFile:
    address = storage_backend.address(vector_data_key(identifier, version))
    payload = bytes(obstore.get(storage_backend.object_store(), address.key).bytes())
    return pyarrow.parquet.ParquetFile(pyarrow.BufferReader(payload))


def test_write_then_read_keeps_identifiers_and_geometry(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = VectorCollectionStore(storage_backend, catalog, settings)

    result = store.write(COLLECTION, sample_features, identifier_property="id", title="Districts")

    assert result.version == 1
    assert result.feature_count == 12
    assert result.published is False

    handle = store.read(COLLECTION)

    assert handle.version == 1
    assert sorted(handle.frame["id"]) == sorted(sample_features["id"])
    assert handle.frame.crs is not None
    original = sample_features.set_index("id").geometry
    written = handle.frame.set_index("id").geometry
    for identifier in original.index:
        assert written.loc[identifier].equals(original.loc[identifier])


def test_write_records_the_catalog_entry(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = VectorCollectionStore(storage_backend, catalog, settings)

    store.write(COLLECTION, sample_features, identifier_property="id", selectable_columns=("level", "path"))

    record = catalog.require(COLLECTION)

    assert isinstance(record, FeatureDataset)
    assert record.item_type is ItemType.FEATURE
    assert record.storage_format is StorageFormat.GEOPARQUET
    assert record.crs == "EPSG:4326"
    assert record.features.identifier_property == "id"
    assert record.features.feature_count == 12
    assert record.features.primary_geometry == "geometry"
    assert record.features.geometry_types == ("Point", "Polygon")
    assert record.features.selectable_columns == ("level", "path")
    assert record.bbox is not None
    assert record.bbox.minimum_x == pytest.approx(0.0)
    assert record.bbox.maximum_x == pytest.approx(21.5)


def test_written_parquet_carries_a_covering_bbox_column(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = VectorCollectionStore(storage_backend, catalog, settings)
    store.write(COLLECTION, sample_features, identifier_property="id")

    parquet_file = read_parquet_footer(storage_backend)
    schema = parquet_file.schema_arrow

    assert "bbox" in schema.names
    assert [field.name for field in schema.field("bbox").type] == ["xmin", "ymin", "xmax", "ymax"]


def test_written_parquet_declares_geoparquet_1_1_0_with_a_covering(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = VectorCollectionStore(storage_backend, catalog, settings)
    store.write(COLLECTION, sample_features, identifier_property="id")

    metadata = json.loads(read_parquet_footer(storage_backend).schema_arrow.metadata[b"geo"])

    assert metadata["version"] == "1.1.0"
    assert metadata["primary_column"] == "geometry"
    geometry = metadata["columns"]["geometry"]
    assert geometry["encoding"] == "WKB"
    assert geometry["covering"]["bbox"] == {
        "xmin": ["bbox", "xmin"],
        "ymin": ["bbox", "ymin"],
        "xmax": ["bbox", "xmax"],
        "ymax": ["bbox", "ymax"],
    }


def test_write_hilbert_sorts_the_features(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = VectorCollectionStore(storage_backend, catalog, settings)
    store.write(COLLECTION, sample_features, identifier_property="id")

    handle = store.read(COLLECTION)

    distances = handle.frame.geometry.hilbert_distance().tolist()
    assert distances == sorted(distances)


def test_write_uses_the_configured_row_group_size(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    small = settings.model_copy(update={"parquet_row_group_size": 4})
    store = VectorCollectionStore(storage_backend, catalog, small)
    store.write(COLLECTION, sample_features, identifier_property="id")

    assert read_parquet_footer(storage_backend).num_row_groups == 3


def test_write_geojson_uses_the_feature_id_as_a_fallback(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings
) -> None:
    store = VectorCollectionStore(storage_backend, catalog, settings)
    feature_collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "from-the-id",
                "properties": {"level": 1},
                "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
            },
            {
                "type": "Feature",
                "properties": {"code": "from-the-property", "level": 2},
                "geometry": {"type": "Point", "coordinates": [3.0, 4.0]},
            },
        ],
    }

    result = store.write_geojson(COLLECTION, feature_collection, identifier_property="code")

    assert result.feature_count == 2
    handle = store.read(COLLECTION)
    assert sorted(handle.frame["code"]) == ["from-the-id", "from-the-property"]
    assert handle.frame.crs is not None


def test_write_refuses_a_selectable_column_that_does_not_exist(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = VectorCollectionStore(storage_backend, catalog, settings)

    with pytest.raises(SelectableColumnError):
        store.write(COLLECTION, sample_features, identifier_property="id", selectable_columns=("missing",))


def test_write_refuses_to_take_over_a_raster_record(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    envelope = BoundingBox(minimum_x=-1.0, minimum_y=-1.0, maximum_x=1.0, maximum_y=1.0)
    catalog.put(
        CoverageDataset(
            dataset_identifier=COLLECTION,
            title="A raster",
            address="memory://memory/ocs/raster/districts",
            grid=GridSpecification(shape=(2, 2), bbox=envelope, crs="EPSG:4326"),
            temporal=TemporalExtent(start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 2, tzinfo=UTC)),
        ),
    )
    store = VectorCollectionStore(storage_backend, catalog, settings)

    with pytest.raises(ItemTypeMismatchError):
        store.write(COLLECTION, sample_features, identifier_property="id")


def test_the_licence_and_attribution_survive_a_second_write(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = VectorCollectionStore(storage_backend, catalog, settings)
    store.write(
        COLLECTION,
        sample_features,
        identifier_property="id",
        license="CC-BY-4.0",
        attribution="Statistics Norway",
    )

    store.write(COLLECTION, sample_features.iloc[:4], identifier_property="id")
    record = catalog.require(COLLECTION)

    assert isinstance(record, FeatureDataset)
    assert record.license == "CC-BY-4.0"
    assert record.attribution == "Statistics Norway"


def test_a_later_write_can_replace_the_licence_and_attribution(
    storage_backend: StorageBackend, catalog: ObjectCatalog, settings: Settings, sample_features: geopandas.GeoDataFrame
) -> None:
    store = VectorCollectionStore(storage_backend, catalog, settings)
    store.write(COLLECTION, sample_features, identifier_property="id", license="CC-BY-4.0", attribution="First")

    store.write(
        COLLECTION,
        sample_features.iloc[:4],
        identifier_property="id",
        license="proprietary",
        attribution="Second",
    )
    record = catalog.require(COLLECTION)

    assert isinstance(record, FeatureDataset)
    assert record.license == "proprietary"
    assert record.attribution == "Second"
