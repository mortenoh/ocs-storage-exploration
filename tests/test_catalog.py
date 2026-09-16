"""Tests for the object store dataset catalog."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import (
    DatasetAlreadyExistsError,
    DatasetNotFoundError,
    PublicationConflictError,
)
from ocs_storage_exploration.storage.protocols import Catalog, StorageBackend
from ocs_storage_exploration.storage.schemas import (
    BoundingBox,
    CatalogEntry,
    CoverageDataset,
    DatasetLifecycle,
    FeatureDataset,
    FeatureDetail,
    GridSpecification,
    ItemType,
    Publication,
    TemporalExtent,
)

BBOX = BoundingBox(minimum_x=-10.0, minimum_y=-5.0, maximum_x=10.0, maximum_y=5.0)


def build_coverage(identifier: str = "temperature", title: str = "Daily temperature") -> CoverageDataset:
    return CoverageDataset(
        dataset_identifier=identifier,
        title=title,
        storage_key=f"raster/{identifier}",
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
        storage_key=f"vector/{identifier}",
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


def test_get_entry_carries_the_revision_the_record_was_read_at(catalog: ObjectCatalog) -> None:
    catalog.put(build_coverage(), create=True)

    entry = catalog.get_entry("temperature")

    assert entry is not None
    assert isinstance(entry, CatalogEntry)
    assert entry.record == catalog.require("temperature")
    assert entry.revision


def test_get_entry_reports_a_missing_record(catalog: ObjectCatalog) -> None:
    assert catalog.get_entry("absent") is None


def test_two_catalogs_creating_the_same_record_cannot_both_win(storage_backend: StorageBackend) -> None:
    first = ObjectCatalog(storage_backend)
    second = ObjectCatalog(storage_backend)

    first.put(build_coverage(), create=True)

    with pytest.raises(DatasetAlreadyExistsError):
        second.put(build_coverage(title="Written by the second catalog"), create=True)
    assert second.require("temperature").title == "Daily temperature"


def test_a_stale_revision_loses_the_compare_and_swap(storage_backend: StorageBackend) -> None:
    first = ObjectCatalog(storage_backend)
    second = ObjectCatalog(storage_backend)
    first.put(build_coverage(), create=True)
    stale = first.get_entry("temperature")
    assert stale is not None
    current = second.get_entry("temperature")
    assert current is not None

    second.put(build_coverage(title="Written by the second catalog"), revision=current.revision)

    with pytest.raises(PublicationConflictError):
        first.put(build_coverage(title="Written by the first catalog"), revision=stale.revision)
    assert storage_backend.exists(first.record_address("temperature")) is True
    assert first.require("temperature").title == "Written by the second catalog"


def test_a_read_in_between_does_not_launder_a_stale_revision(storage_backend: StorageBackend) -> None:
    first = ObjectCatalog(storage_backend)
    second = ObjectCatalog(storage_backend)
    first.put(build_coverage(), create=True)
    stale = first.get_entry("temperature")
    assert stale is not None
    current = second.get_entry("temperature")
    assert current is not None
    second.put(build_coverage(title="Written by the second catalog"), revision=current.revision)

    # Reading the fresh record must not turn the revision the caller still holds into a valid one.
    assert first.require("temperature").title == "Written by the second catalog"

    with pytest.raises(PublicationConflictError):
        first.put(build_coverage(title="Written by the first catalog"), revision=stale.revision)


def test_a_deleted_record_loses_the_compare_and_swap(storage_backend: StorageBackend) -> None:
    first = ObjectCatalog(storage_backend)
    second = ObjectCatalog(storage_backend)
    first.put(build_coverage(), create=True)
    entry = first.get_entry("temperature")
    assert entry is not None
    second.delete("temperature")

    with pytest.raises(PublicationConflictError):
        first.put(build_coverage(title="Written after the delete"), revision=entry.revision)


def test_a_put_without_a_revision_overwrites_whatever_is_there(storage_backend: StorageBackend) -> None:
    first = ObjectCatalog(storage_backend)
    second = ObjectCatalog(storage_backend)
    first.put(build_coverage(), create=True)
    revision = second.require_entry("temperature").revision
    second.put(build_coverage(title="Written by the second catalog"), revision=revision)

    first.put(build_coverage(title="Forced overwrite"))

    assert second.require("temperature").title == "Forced overwrite"


def test_require_entry_reports_a_missing_record(catalog: ObjectCatalog) -> None:
    with pytest.raises(DatasetNotFoundError):
        catalog.require_entry("absent")


def test_the_record_address_is_below_the_base_prefix(catalog: ObjectCatalog) -> None:
    address = catalog.record_address("temperature")

    assert address.key == f"{catalog.backend.base_prefix}/catalog/datasets/temperature.json"
    assert catalog.backend is not None


def test_list_datasets_skips_a_record_being_deleted_while_get_still_reads_it(catalog: ObjectCatalog) -> None:
    catalog.put(build_coverage(), create=True)
    catalog.put(build_feature(), create=True)
    entry = catalog.require_entry("districts")
    catalog.put(entry.record.model_copy(update={"lifecycle": DatasetLifecycle.DELETING}), revision=entry.revision)

    listed = catalog.list_datasets()

    assert [record.dataset_identifier for record in listed] == ["temperature"]
    assert catalog.list_datasets(ItemType.FEATURE) == []
    # A deletion that stopped half way has to be findable, or nothing could ever finish it.
    marked = catalog.require("districts")
    assert marked.is_deleting is True
    assert catalog.get("districts") is not None
