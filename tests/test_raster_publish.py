from __future__ import annotations

from datetime import datetime

import icechunk
import numpy
import pytest
import xarray

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import SnapshotNotFoundError
from ocs_storage_exploration.storage.models import BoundingBox, CoverageDataset, GridSpecification, ItemType
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.raster import (
    PUBLISHED_BRANCH,
    RasterRepository,
    TimeStep,
    VersionSelector,
    build_synthetic_cube,
    build_timestamps,
)

IDENTIFIER = "publish"
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
    count: int = 3,
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


def test_publish_moves_the_branch_and_updates_the_record(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    catalog: ObjectCatalog,
    storage_backend: StorageBackend,
):
    created = raster_repository.create(IDENTIFIER, grid, build_cube(grid))

    result = raster_repository.publish(IDENTIFIER)

    assert result.item_type is ItemType.COVERAGE
    assert result.published is True
    assert result.changed is True
    assert result.snapshot_identifier == created.snapshot_identifier
    assert result.previous_snapshot_identifier is None
    record = catalog.require(IDENTIFIER)
    assert isinstance(record, CoverageDataset)
    assert record.publication.published is True
    assert record.publication.snapshot_identifier == created.snapshot_identifier
    assert record.publication.published_at is not None
    address = raster_repository.repository_address(IDENTIFIER)
    branches = icechunk.Repository.open(storage_backend.icechunk_storage(address)).list_branches()
    assert PUBLISHED_BRANCH in branches


def test_published_reader_keeps_the_published_snapshot_while_main_moves_on(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    raster_repository.publish(IDENTIFIER)

    raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))

    with raster_repository.read(IDENTIFIER, version=VersionSelector.PUBLISHED) as handle:
        assert handle.dataset.sizes["t"] == 3
    with raster_repository.read(IDENTIFIER, version=VersionSelector.DRAFT) as handle:
        assert handle.dataset.sizes["t"] == 5


def test_publishing_the_newest_snapshot_moves_the_pointer_forward(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    first = raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    raster_repository.publish(IDENTIFIER)
    second = raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))

    result = raster_repository.publish(IDENTIFIER)

    assert result.changed is True
    assert result.snapshot_identifier == second.snapshot_identifier
    assert result.previous_snapshot_identifier == first.snapshot_identifier
    with raster_repository.read(IDENTIFIER) as handle:
        assert handle.dataset.sizes["t"] == 5


def test_rollback_publishes_an_older_snapshot_again(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    catalog: ObjectCatalog,
):
    original = build_cube(grid, count=3)
    first = raster_repository.create(IDENTIFIER, grid, original)
    second = raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))
    raster_repository.publish(IDENTIFIER)

    result = raster_repository.publish(IDENTIFIER, snapshot_identifier=first.snapshot_identifier)

    assert result.changed is True
    assert result.snapshot_identifier == first.snapshot_identifier
    assert result.previous_snapshot_identifier == second.snapshot_identifier
    with raster_repository.read(IDENTIFIER) as handle:
        assert handle.dataset.sizes["t"] == 3
        assert numpy.allclose(handle.dataset[VARIABLE].values, original[VARIABLE].values)
    record = catalog.require(IDENTIFIER)
    assert isinstance(record, CoverageDataset)
    assert record.publication.snapshot_identifier == first.snapshot_identifier
    assert record.publication.previous_snapshot_identifier == second.snapshot_identifier


def test_republishing_the_same_snapshot_reports_no_change(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    created = raster_repository.create(IDENTIFIER, grid, build_cube(grid))
    raster_repository.publish(IDENTIFIER)

    result = raster_repository.publish(IDENTIFIER)

    assert result.changed is False
    assert result.published is True
    assert result.snapshot_identifier == created.snapshot_identifier
    assert result.previous_snapshot_identifier == created.snapshot_identifier


def test_publish_refuses_a_snapshot_outside_the_ancestry(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))

    with pytest.raises(SnapshotNotFoundError):
        raster_repository.publish(IDENTIFIER, snapshot_identifier="ZZZZZZZZZZZZZZZZZZZZ")


def test_read_refuses_a_snapshot_outside_the_ancestry(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))

    with pytest.raises(SnapshotNotFoundError), raster_repository.read(IDENTIFIER, snapshot_identifier="ZZZZZZZZZZZZ"):
        pass


def test_reading_an_explicit_snapshot_ignores_the_published_pointer(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    first = raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))
    raster_repository.publish(IDENTIFIER)

    with raster_repository.read(IDENTIFIER, snapshot_identifier=first.snapshot_identifier) as handle:
        assert handle.snapshot_identifier == first.snapshot_identifier
        assert handle.dataset.sizes["t"] == 3


def test_a_read_handle_opened_before_a_publish_still_reads_its_snapshot(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    original = build_cube(grid, count=3)
    raster_repository.create(IDENTIFIER, grid, original)
    raster_repository.publish(IDENTIFIER)

    with raster_repository.read(IDENTIFIER) as handle:
        raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))
        raster_repository.publish(IDENTIFIER)

        assert handle.dataset.sizes["t"] == 3
        assert numpy.allclose(handle.dataset[VARIABLE].values, original[VARIABLE].values)
