"""Tests for the awaitable storage facade: the native catalog, the bounded engines and the timeout."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import geopandas
import pytest

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.addresses import StorageScheme
from ocs_storage_exploration.storage.errors import (
    DatasetNotFoundError,
    ItemTypeMismatchError,
    StorageTimeoutError,
)
from ocs_storage_exploration.storage.keys import raster_prefix, vector_prefix
from ocs_storage_exploration.storage.protocols import AsyncCatalog
from ocs_storage_exploration.storage.raster import TimeStep, VersionSelector, build_synthetic_cube, build_timestamps
from ocs_storage_exploration.storage.raster.repository import RasterRepository
from ocs_storage_exploration.storage.schemas import (
    BoundingBox,
    CoverageDataset,
    FeatureDataset,
    GridSpecification,
    ItemType,
)
from ocs_storage_exploration.storage.service import StorageService
from ocs_storage_exploration.storage.service_async import AsyncStorageService, StorageOperationRunner

COVERAGE = "coverage-one"
COLLECTION = "collection-one"
VARIABLE = "temperature"


def build_grid() -> GridSpecification:
    return GridSpecification(
        shape=(4, 6),
        bbox=BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=12.0, maximum_y=8.0),
        crs="EPSG:4326",
    )


def build_cube() -> Any:
    return build_synthetic_cube(
        build_grid(),
        variable=VARIABLE,
        timestamps=build_timestamps(datetime(2020, 1, 1), 2, TimeStep.DAY),
    )


@pytest.fixture
async def populated(
    async_storage_service: AsyncStorageService, sample_features: geopandas.GeoDataFrame
) -> AsyncStorageService:
    await async_storage_service.raster.create(COVERAGE, build_grid(), build_cube(), title="Coverage one")
    await async_storage_service.vector.write(
        COLLECTION,
        sample_features,
        identifier_property="id",
        title="Collection one",
    )
    return async_storage_service


def test_from_settings_builds_the_sync_service_it_wraps(settings: Settings) -> None:
    service = AsyncStorageService.from_settings(settings)

    assert isinstance(service.service, StorageService)
    assert service.settings is settings
    assert service.backend.scheme is settings.backend
    assert isinstance(service.catalog, AsyncCatalog)
    assert service.raster.repository is service.service.raster
    assert service.vector.store is service.service.vector


def test_the_runner_is_sized_from_the_settings(settings: Settings) -> None:
    configured = settings.model_copy(
        update={"max_concurrent_storage_operations": 4, "storage_operation_timeout_seconds": 7.5},
    )

    runner = AsyncStorageService.from_settings(configured).runner

    assert runner.limiter.total_tokens == 4
    assert runner.timeout_seconds == 7.5


async def test_the_runner_reports_a_timeout_as_a_storage_error() -> None:
    runner = StorageOperationRunner(max_concurrent_operations=1, timeout_seconds=0.05)

    started = time.perf_counter()
    with pytest.raises(StorageTimeoutError) as failure:
        await runner.run(lambda: time.sleep(2.0), description="sleeping")
    elapsed = time.perf_counter() - started

    assert "sleeping" in failure.value.message
    assert failure.value.status_code == 504
    assert elapsed < 1.0


async def test_the_runner_lets_an_operation_raise_its_own_timeout_error() -> None:
    runner = StorageOperationRunner(max_concurrent_operations=1, timeout_seconds=30.0)

    def raise_timeout() -> None:
        raise TimeoutError("the library timed out by itself")

    with pytest.raises(TimeoutError) as failure:
        await runner.run(raise_timeout, description="failing")

    assert not isinstance(failure.value, StorageTimeoutError)


async def test_describe_backends_lists_the_active_backend_first(async_storage_service: AsyncStorageService) -> None:
    descriptions = await async_storage_service.describe_backends()

    assert descriptions[0].scheme is async_storage_service.backend.scheme
    assert descriptions[0].available is True


async def test_list_datasets_filters_by_item_type(populated: AsyncStorageService) -> None:
    assert {record.dataset_identifier for record in await populated.list_datasets()} == {COVERAGE, COLLECTION}
    assert [record.dataset_identifier for record in await populated.list_datasets(ItemType.COVERAGE)] == [COVERAGE]
    assert [record.dataset_identifier for record in await populated.list_datasets(ItemType.FEATURE)] == [COLLECTION]


async def test_get_dataset_reads_one_record(populated: AsyncStorageService) -> None:
    assert (await populated.get_dataset(COVERAGE)).title == "Coverage one"

    with pytest.raises(DatasetNotFoundError):
        await populated.get_dataset("absent")


async def test_require_coverage_and_require_collection_refuse_the_other_item_type(
    populated: AsyncStorageService,
) -> None:
    assert isinstance(await populated.require_coverage(COVERAGE), CoverageDataset)
    assert isinstance(await populated.require_collection(COLLECTION), FeatureDataset)

    with pytest.raises(ItemTypeMismatchError):
        await populated.require_coverage(COLLECTION)
    with pytest.raises(ItemTypeMismatchError):
        await populated.require_collection(COVERAGE)


async def test_the_raster_engine_round_trips_through_the_facade(populated: AsyncStorageService) -> None:
    published = await populated.raster.publish(COVERAGE)
    appended = await populated.raster.append(COVERAGE, build_cube())
    versions = await populated.raster.versions(COVERAGE)
    summary = await populated.raster.query(COVERAGE, version=VersionSelector.DRAFT)
    description = await populated.raster.describe(COVERAGE, version=VersionSelector.DRAFT)
    attributes = await populated.raster.root_attributes(COVERAGE, version=VersionSelector.DRAFT)
    reconciled = await populated.raster.reconcile_publication(COVERAGE)

    assert published.published is True
    assert appended.timestep_count == 4
    assert len(versions) == 3
    assert summary.timestep_count == 4
    assert description.variables == (VARIABLE,)
    assert attributes
    assert reconciled.publication.published is True


async def test_the_vector_engine_round_trips_through_the_facade(populated: AsyncStorageService) -> None:
    published = await populated.vector.publish(COLLECTION)
    handle = await populated.vector.read(COLLECTION)
    versions = await populated.vector.versions(COLLECTION)
    claimed = await populated.vector.claimed_versions(COLLECTION)
    metadata = await populated.vector.version_metadata(COLLECTION, 1)
    current = await populated.vector.published_metadata(COLLECTION)
    schema = await populated.vector.table_schema(COLLECTION)
    pointer = await populated.vector.pointer(COLLECTION)

    assert published.version == 1
    assert len(handle.frame) == 12
    assert versions == [1]
    assert claimed == [1]
    assert metadata.feature_count == 12
    assert current is not None
    assert schema.row_count == 12
    assert pointer is not None
    assert await populated.vector.current_version(COLLECTION) == 1


async def test_writing_geojson_through_the_facade_reserves_the_next_version(
    populated: AsyncStorageService,
) -> None:
    reserved = await populated.vector.reserve_version(COLLECTION)
    written = await populated.vector.write_geojson(
        "collection-two",
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"id": "oslo"},
                    "geometry": {"type": "Point", "coordinates": [10.7, 59.9]},
                },
            ],
        },
        identifier_property="id",
        publish=True,
    )

    assert reserved == 2
    assert written.version == 1
    assert written.published is True


async def test_delete_dataset_routes_a_coverage_to_the_raster_engine(populated: AsyncStorageService) -> None:
    deleted = await populated.delete_dataset(COVERAGE)

    assert isinstance(deleted, CoverageDataset)
    assert populated.backend.list_keys(populated.backend.address(raster_prefix(COVERAGE))) == []
    assert [record.dataset_identifier for record in await populated.list_datasets()] == [COLLECTION]


async def test_delete_dataset_routes_a_collection_to_the_vector_engine(populated: AsyncStorageService) -> None:
    deleted = await populated.delete_dataset(COLLECTION)

    assert isinstance(deleted, FeatureDataset)
    assert populated.backend.list_keys(populated.backend.address(vector_prefix(COLLECTION))) == []
    assert [record.dataset_identifier for record in await populated.list_datasets()] == [COVERAGE]


async def test_delete_dataset_reports_an_unknown_identifier(populated: AsyncStorageService) -> None:
    with pytest.raises(DatasetNotFoundError):
        await populated.delete_dataset("absent")


async def test_the_engines_can_delete_their_own_datasets(populated: AsyncStorageService) -> None:
    await populated.raster.delete(COVERAGE)
    removed = await populated.vector.delete(COLLECTION)

    assert removed > 0
    assert await populated.list_datasets() == []


async def test_a_slow_engine_call_is_reported_as_a_timeout(
    settings: Settings, sample_features: geopandas.GeoDataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow_query(*arguments: Any, **keywords: Any) -> Any:
        time.sleep(2.0)
        raise AssertionError("the timeout should have answered long before this call returned")

    monkeypatch.setattr(RasterRepository, "query", slow_query)
    service = AsyncStorageService.from_settings(settings.model_copy(update={"storage_operation_timeout_seconds": 0.1}))

    with pytest.raises(StorageTimeoutError):
        await service.raster.query(COVERAGE)


def test_the_memory_backend_is_the_default_of_the_helper_settings() -> None:
    # The facade reads its bounds from the settings it was built with, not from a process wide default.
    service = AsyncStorageService.from_settings(Settings(backend=StorageScheme.MEMORY))

    assert service.runner.limiter.total_tokens == 16
    assert service.runner.timeout_seconds == 180.0
