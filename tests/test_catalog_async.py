"""Tests for the awaitable dataset catalog, which must behave exactly like the sync one."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.catalog_async import AsyncObjectCatalog
from ocs_storage_exploration.storage.errors import (
    DatasetAlreadyExistsError,
    DatasetNotFoundError,
    PublicationConflictError,
)
from ocs_storage_exploration.storage.protocols import AsyncCatalog, StorageBackend
from ocs_storage_exploration.storage.schemas import (
    BoundingBox,
    CatalogEntry,
    CoverageDataset,
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


def test_the_async_catalog_satisfies_the_async_protocol(async_catalog: AsyncObjectCatalog) -> None:
    assert isinstance(async_catalog, AsyncCatalog)


async def test_records_round_trip_through_json(async_catalog: AsyncObjectCatalog) -> None:
    coverage = build_coverage()
    feature = build_feature()

    await async_catalog.put(coverage)
    await async_catalog.put(feature)

    assert await async_catalog.get("temperature") == coverage
    assert await async_catalog.get("districts") == feature
    assert isinstance(await async_catalog.require("temperature"), CoverageDataset)
    assert isinstance(await async_catalog.require("districts"), FeatureDataset)


async def test_missing_records_are_reported(async_catalog: AsyncObjectCatalog) -> None:
    assert await async_catalog.get("absent") is None
    assert await async_catalog.get_entry("absent") is None

    with pytest.raises(DatasetNotFoundError):
        await async_catalog.require("absent")

    with pytest.raises(DatasetNotFoundError):
        await async_catalog.require_entry("absent")

    with pytest.raises(DatasetNotFoundError):
        await async_catalog.delete("absent")


async def test_records_are_listed_and_filtered_by_item_type(async_catalog: AsyncObjectCatalog) -> None:
    await async_catalog.put(build_coverage())
    await async_catalog.put(build_feature())

    listed = await async_catalog.list_datasets()
    coverages = await async_catalog.list_datasets(ItemType.COVERAGE)
    features = await async_catalog.list_datasets(ItemType.FEATURE)
    identifiers = [identifier async for identifier in async_catalog.iter_identifiers()]

    assert [dataset.dataset_identifier for dataset in listed] == ["districts", "temperature"]
    assert [dataset.dataset_identifier for dataset in coverages] == ["temperature"]
    assert [dataset.dataset_identifier for dataset in features] == ["districts"]
    assert identifiers == ["districts", "temperature"]


async def test_a_record_written_by_the_sync_catalog_is_read_by_the_async_one(
    storage_backend: StorageBackend,
) -> None:
    ObjectCatalog(storage_backend).put(build_coverage(), create=True)

    assert (await AsyncObjectCatalog(storage_backend).require("temperature")).title == "Daily temperature"


async def test_a_record_written_by_the_async_catalog_is_read_by_the_sync_one(
    storage_backend: StorageBackend,
) -> None:
    await AsyncObjectCatalog(storage_backend).put(build_coverage(), create=True)

    assert ObjectCatalog(storage_backend).require("temperature").title == "Daily temperature"


async def test_get_entry_carries_the_revision_the_record_was_read_at(async_catalog: AsyncObjectCatalog) -> None:
    await async_catalog.put(build_coverage(), create=True)

    entry = await async_catalog.get_entry("temperature")

    assert isinstance(entry, CatalogEntry)
    assert entry.record == await async_catalog.require("temperature")
    assert entry.revision


async def test_two_catalogs_creating_the_same_record_cannot_both_win(storage_backend: StorageBackend) -> None:
    first = AsyncObjectCatalog(storage_backend)
    second = AsyncObjectCatalog(storage_backend)

    await first.put(build_coverage(), create=True)

    with pytest.raises(DatasetAlreadyExistsError):
        await second.put(build_coverage(title="Written by the second catalog"), create=True)
    assert (await second.require("temperature")).title == "Daily temperature"


async def test_a_stale_revision_loses_the_compare_and_swap(storage_backend: StorageBackend) -> None:
    first = AsyncObjectCatalog(storage_backend)
    second = AsyncObjectCatalog(storage_backend)
    await first.put(build_coverage(), create=True)
    stale = await first.require_entry("temperature")
    current = await second.require_entry("temperature")

    await second.put(build_coverage(title="Written by the second catalog"), revision=current.revision)

    with pytest.raises(PublicationConflictError):
        await first.put(build_coverage(title="Written by the first catalog"), revision=stale.revision)
    assert (await first.require("temperature")).title == "Written by the second catalog"


async def test_a_deleted_record_loses_the_compare_and_swap(storage_backend: StorageBackend) -> None:
    first = AsyncObjectCatalog(storage_backend)
    second = AsyncObjectCatalog(storage_backend)
    await first.put(build_coverage(), create=True)
    entry = await first.require_entry("temperature")
    await second.delete("temperature")

    with pytest.raises(PublicationConflictError):
        await first.put(build_coverage(title="Written after the delete"), revision=entry.revision)


async def test_a_write_is_either_a_create_or_a_compare_and_swap(async_catalog: AsyncObjectCatalog) -> None:
    with pytest.raises(PublicationConflictError):
        await async_catalog.put(build_coverage(), revision="some-etag", create=True)


async def test_a_put_without_a_revision_overwrites_whatever_is_there(async_catalog: AsyncObjectCatalog) -> None:
    await async_catalog.put(build_coverage(), create=True)

    await async_catalog.put(build_coverage(title="Forced overwrite"))

    assert (await async_catalog.require("temperature")).title == "Forced overwrite"


async def test_records_can_be_deleted(async_catalog: AsyncObjectCatalog) -> None:
    await async_catalog.put(build_coverage())

    await async_catalog.delete("temperature")

    assert await async_catalog.get("temperature") is None


def test_the_record_address_is_below_the_base_prefix(async_catalog: AsyncObjectCatalog) -> None:
    address = async_catalog.record_address("temperature")

    assert address.key == f"{async_catalog.backend.base_prefix}/catalog/datasets/temperature.json"
