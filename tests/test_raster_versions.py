from __future__ import annotations

from datetime import datetime

import pytest
import xarray

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import DatasetNotFoundError
from ocs_storage_exploration.storage.models import BoundingBox, GridSpecification
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.raster import (
    RasterRepository,
    TimeStep,
    build_synthetic_cube,
    build_timestamps,
)

IDENTIFIER = "versions"
VARIABLE = "temperature"
START = datetime(2020, 1, 1)


def build_grid() -> GridSpecification:
    return GridSpecification(
        shape=(4, 6),
        bbox=BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=12.0, maximum_y=8.0),
        crs="EPSG:4326",
    )


def build_cube(
    grid: GridSpecification,
    *,
    count: int = 2,
    start: datetime = START,
    seed: int = 0,
) -> xarray.Dataset:
    return build_synthetic_cube(
        grid,
        variable=VARIABLE,
        timestamps=build_timestamps(start, count, TimeStep.DAY),
        seed=seed,
    )


@pytest.fixture
def grid() -> GridSpecification:
    return build_grid()


@pytest.fixture
def raster_repository(
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
) -> RasterRepository:
    return RasterRepository(storage_backend, catalog, settings)


def test_versions_are_listed_newest_first(raster_repository: RasterRepository, grid: GridSpecification):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid), message="initial write")
    raster_repository.append(IDENTIFIER, build_cube(grid, count=1, start=datetime(2020, 1, 3)), message="second write")

    versions = raster_repository.versions(IDENTIFIER)

    assert [version.message for version in versions[:2]] == ["second write", "initial write"]
    assert versions[0].written_at >= versions[1].written_at
    assert all(version.snapshot_identifier for version in versions)
    assert not any(version.is_published for version in versions)


def test_versions_mark_the_published_snapshot(raster_repository: RasterRepository, grid: GridSpecification):
    created = raster_repository.create(IDENTIFIER, grid, build_cube(grid))
    raster_repository.publish(IDENTIFIER)
    raster_repository.append(IDENTIFIER, build_cube(grid, count=1, start=datetime(2020, 1, 3)))

    versions = raster_repository.versions(IDENTIFIER)

    published = [version for version in versions if version.is_published]
    assert [version.snapshot_identifier for version in published] == [created.snapshot_identifier]
    assert versions[0].snapshot_identifier != created.snapshot_identifier


def test_versions_honour_the_limit(raster_repository: RasterRepository, grid: GridSpecification):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))
    raster_repository.append(IDENTIFIER, build_cube(grid, count=1, start=datetime(2020, 1, 3)))

    versions = raster_repository.versions(IDENTIFIER, limit=1)

    assert len(versions) == 1


def test_versions_require_a_catalog_record(raster_repository: RasterRepository):
    with pytest.raises(DatasetNotFoundError):
        raster_repository.versions("missing")
