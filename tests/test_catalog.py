"""Tests for the object store dataset catalog."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import DatasetNotFoundError, PublicationConflictError
from ocs_storage_exploration.storage.models import (
    BoundingBox,
    CoverageDataset,
    FeatureDataset,
    FeatureDetail,
    GridSpecification,
    ItemType,
    Publication,
    TemporalExtent,
)
from ocs_storage_exploration.storage.protocols import Catalog, StorageBackend

BBOX = BoundingBox(minimum_x=-10.0, minimum_y=-5.0, maximum_x=10.0, maximum_y=5.0)


def build_coverage(identifier: str = "temperature", title: str = "Daily temperature") -> CoverageDataset:
    return CoverageDataset(
        dataset_identifier=identifier,
        title=title,
        address=f"memory://memory/ocs/raster/{identifier}",
        bbox=BBOX,
        grid=GridSpecification(shape=(4, 8), bbox=BBOX, crs="EPSG:4326", nodata_value=-9999.0),
        variables=("temperature",),
        temporal=TemporalExtent(start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 3, tzinfo=UTC)),
        timestep_count=3,
    )


def build_feature(identifier: str = "districts") -> FeatureDataset:
    return FeatureDataset(
        dataset_identifier=identifier,
        title="Districts",
        address=f"memory://memory/ocs/vector/{identifier}",
        bbox=BBOX,
        crs="EPSG:4326",
        features=FeatureDetail(identifier_property="id", feature_count=12, geometry_types=("Polygon",)),
        publication=Publication(published=True, version=1),
    )


def test_catalog_satisfies_the_protocol(catalog: ObjectCatalog) -> None:
    assert isinstance(catalog, Catalog)


def test_records_round_trip_through_json(catalog: ObjectCatalog) -> None:
    coverage = build_coverage()
    feature = build_feature()

    catalog.put(coverage)
    catalog.put(feature)

    assert catalog.get("temperature") == coverage
    assert catalog.get("districts") == feature
    assert isinstance(catalog.require("temperature"), CoverageDataset)
    assert isinstance(catalog.require("districts"), FeatureDataset)


def test_missing_records_are_reported(catalog: ObjectCatalog) -> None:
    assert catalog.get("absent") is None

    with pytest.raises(DatasetNotFoundError):
        catalog.require("absent")

    with pytest.raises(DatasetNotFoundError):
        catalog.delete("absent")


def test_records_are_listed_and_filtered_by_item_type(catalog: ObjectCatalog) -> None:
    catalog.put(build_coverage())
    catalog.put(build_feature())

    assert [dataset.dataset_identifier for dataset in catalog.list_datasets()] == ["districts", "temperature"]
    assert [dataset.dataset_identifier for dataset in catalog.list_datasets(ItemType.COVERAGE)] == ["temperature"]
    assert [dataset.dataset_identifier for dataset in catalog.list_datasets(ItemType.FEATURE)] == ["districts"]
    assert list(catalog.iter_identifiers()) == ["districts", "temperature"]


def test_records_can_be_replaced(catalog: ObjectCatalog) -> None:
    catalog.put(build_coverage())

    catalog.put(build_coverage(title="Revised title"))

    assert catalog.require("temperature").title == "Revised title"


def test_records_can_be_deleted(catalog: ObjectCatalog) -> None:
    catalog.put(build_coverage())

    catalog.delete("temperature")

    assert catalog.get("temperature") is None
    assert list(catalog.iter_identifiers()) == []


def test_a_second_catalog_over_the_same_backend_sees_the_record(storage_backend: StorageBackend) -> None:
    writer = ObjectCatalog(storage_backend)
    reader = ObjectCatalog(storage_backend)

    writer.put(build_coverage())

    assert reader.require("temperature") == writer.require("temperature")


def test_a_stale_record_loses_the_compare_and_swap(storage_backend: StorageBackend) -> None:
    first = ObjectCatalog(storage_backend)
    second = ObjectCatalog(storage_backend)
    first.put(build_coverage())
    assert second.get("temperature") is not None

    second.put(build_coverage(title="Written by the second catalog"))

    with pytest.raises(PublicationConflictError):
        first.put(build_coverage(title="Written by the first catalog"))
    assert storage_backend.exists(first.record_address("temperature")) is True
    assert first.require("temperature").title == "Written by the second catalog"


def test_a_deleted_record_loses_the_compare_and_swap(storage_backend: StorageBackend) -> None:
    first = ObjectCatalog(storage_backend)
    second = ObjectCatalog(storage_backend)
    first.put(build_coverage())
    second.get("temperature")
    second.delete("temperature")

    with pytest.raises(PublicationConflictError):
        first.put(build_coverage(title="Written after the delete"))


def test_forgetting_etags_turns_the_next_write_into_an_overwrite(storage_backend: StorageBackend) -> None:
    first = ObjectCatalog(storage_backend)
    second = ObjectCatalog(storage_backend)
    first.put(build_coverage())
    second.get("temperature")
    second.put(build_coverage(title="Written by the second catalog"))

    first.forget_etags()
    first.put(build_coverage(title="Forced overwrite"))

    assert second.require("temperature").title == "Forced overwrite"


def test_the_record_address_is_below_the_base_prefix(catalog: ObjectCatalog) -> None:
    address = catalog.record_address("temperature")

    assert address.key == "ocs/catalog/datasets/temperature.json"
    assert catalog.backend is not None
