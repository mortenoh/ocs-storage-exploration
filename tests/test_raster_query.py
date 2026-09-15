from __future__ import annotations

from datetime import datetime

import numpy
import pytest
import xarray

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import QuerySizeGuardError, RasterContractError
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.raster import (
    RasterRepository,
    TimeStep,
    VersionSelector,
    build_synthetic_cube,
    build_timestamps,
)
from ocs_storage_exploration.storage.schemas import BoundingBox, GridSpecification

IDENTIFIER = "query"
VARIABLE = "temperature"
START = datetime(2020, 1, 1)
NORTHERN_HALF = BoundingBox(minimum_x=0.0, minimum_y=4.0, maximum_x=12.0, maximum_y=8.0)


def build_grid() -> GridSpecification:
    return GridSpecification(
        shape=(4, 6),
        bbox=BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=12.0, maximum_y=8.0),
        crs="EPSG:4326",
    )


def build_cube(grid: GridSpecification, *, count: int = 3, seed: int = 0) -> xarray.Dataset:
    return build_synthetic_cube(
        grid,
        variable=VARIABLE,
        timestamps=build_timestamps(START, count, TimeStep.DAY),
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
    repository = RasterRepository(storage_backend, catalog, settings)
    repository.create(IDENTIFIER, build_grid(), build_cube(build_grid()))
    repository.publish(IDENTIFIER)
    return repository


def test_query_without_a_window_reads_every_cell(raster_repository: RasterRepository):
    summary = raster_repository.query(IDENTIFIER)

    assert summary.variable == VARIABLE
    assert summary.crs == "EPSG:4326"
    assert summary.timestep_count == 3
    assert summary.cell_count == 3 * 4 * 6
    assert summary.bbox.as_tuple() == (0.0, 0.0, 12.0, 8.0)
    assert summary.minimum is not None
    assert summary.maximum is not None
    assert summary.mean is not None
    assert summary.minimum <= summary.mean <= summary.maximum


def test_bbox_subset_reads_fewer_cells_and_reports_a_different_mean(raster_repository: RasterRepository):
    full = raster_repository.query(IDENTIFIER)

    subset = raster_repository.query(IDENTIFIER, bbox=NORTHERN_HALF)

    assert subset.cell_count < full.cell_count
    assert subset.cell_count == 3 * 2 * 6
    assert subset.mean is not None
    assert full.mean is not None
    assert subset.mean != pytest.approx(full.mean, abs=1e-6)


def test_bbox_on_a_descending_y_coordinate_returns_rows(raster_repository: RasterRepository):
    summary = raster_repository.query(IDENTIFIER, bbox=NORTHERN_HALF)

    assert summary.cell_count > 0
    assert summary.bbox.minimum_y == pytest.approx(4.0)
    assert summary.bbox.maximum_y == pytest.approx(8.0)


def test_bbox_of_one_cell_still_has_a_positive_envelope(raster_repository: RasterRepository):
    single = BoundingBox(minimum_x=0.5, minimum_y=6.5, maximum_x=1.5, maximum_y=7.5)

    summary = raster_repository.query(IDENTIFIER, bbox=single)

    assert summary.cell_count == 3
    assert summary.bbox.maximum_x > summary.bbox.minimum_x
    assert summary.bbox.maximum_y > summary.bbox.minimum_y


def test_time_range_filters_the_timesteps(raster_repository: RasterRepository):
    summary = raster_repository.query(IDENTIFIER, start=datetime(2020, 1, 2), end=datetime(2020, 1, 3))

    assert summary.timestep_count == 2
    assert summary.cell_count == 2 * 4 * 6


def test_open_ended_time_range_filters_from_the_start(raster_repository: RasterRepository):
    summary = raster_repository.query(IDENTIFIER, start=datetime(2020, 1, 3))

    assert summary.timestep_count == 1


def test_empty_window_is_refused(raster_repository: RasterRepository):
    outside = BoundingBox(minimum_x=100.0, minimum_y=60.0, maximum_x=120.0, maximum_y=70.0)

    with pytest.raises(QuerySizeGuardError, match="no cells"):
        raster_repository.query(IDENTIFIER, bbox=outside)


def test_empty_time_range_is_refused(raster_repository: RasterRepository):
    with pytest.raises(QuerySizeGuardError, match="no cells"):
        raster_repository.query(IDENTIFIER, start=datetime(2021, 1, 1), end=datetime(2021, 2, 1))


def test_oversized_window_is_refused(
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
    raster_repository: RasterRepository,
):
    guarded = RasterRepository(storage_backend, catalog, settings.model_copy(update={"max_query_cell_count": 4}))

    with pytest.raises(QuerySizeGuardError, match="more than the 4 allowed"):
        guarded.query(IDENTIFIER)

    assert guarded.query(IDENTIFIER, bbox=BoundingBox(minimum_x=0.5, minimum_y=6.5, maximum_x=1.5, maximum_y=7.5))


def test_query_refuses_an_unknown_variable(raster_repository: RasterRepository):
    with pytest.raises(RasterContractError, match="humidity"):
        raster_repository.query(IDENTIFIER, variable="humidity")


def test_query_ignores_the_nodata_value_in_the_statistics(
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
):
    grid = build_grid().model_copy(update={"nodata_value": -9999.0})
    repository = RasterRepository(storage_backend, catalog, settings)
    cube = build_cube(grid, count=2)
    cube[VARIABLE].values[0, 0, 0] = -9999.0
    repository.create("nodata", grid, cube)
    repository.publish("nodata")

    summary = repository.query("nodata")

    assert summary.minimum is not None
    assert summary.minimum > -9999.0


def test_query_reports_no_statistics_when_every_cell_is_not_a_number(
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
):
    grid = build_grid()
    repository = RasterRepository(storage_backend, catalog, settings)
    cube = build_cube(grid, count=1)
    cube[VARIABLE].values[:] = numpy.nan
    repository.create("blank", grid, cube)
    repository.publish("blank")

    summary = repository.query("blank")

    assert summary.cell_count == 24
    assert summary.minimum is None
    assert summary.maximum is None
    assert summary.mean is None


def build_antimeridian_grid() -> GridSpecification:
    return GridSpecification(
        shape=(2, 4),
        bbox=BoundingBox(minimum_x=170.0, minimum_y=-10.0, maximum_x=190.0, maximum_y=10.0),
        crs="EPSG:4326",
    )


def test_a_published_query_reports_the_published_grid_and_not_the_newest_draft(
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
):
    repository = RasterRepository(storage_backend, catalog, settings)
    published_grid = build_grid()
    repository.create("regrid", published_grid, build_cube(published_grid, count=2))
    repository.publish("regrid")
    draft_grid = GridSpecification(
        shape=(4, 6),
        bbox=BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=1_200_000.0, maximum_y=800_000.0),
        crs="EPSG:3857",
    )
    repository.create("regrid", draft_grid, build_cube(draft_grid, count=2), overwrite=True)

    published = repository.query("regrid")
    draft = repository.query("regrid", version=VersionSelector.DRAFT)
    description = repository.describe("regrid")

    assert published.crs == "EPSG:4326"
    assert published.bbox.as_tuple() == (0.0, 0.0, 12.0, 8.0)
    assert draft.crs == "EPSG:3857"
    assert draft.bbox.as_tuple() == (0.0, 0.0, 1_200_000.0, 800_000.0)
    assert description.snapshot_identifier == published.snapshot_identifier
    assert description.crs == "EPSG:4326"
    assert description.bbox.as_tuple() == (0.0, 0.0, 12.0, 8.0)
    assert description.shape == (4, 6)
    assert description.variables == (VARIABLE,)
    assert description.timestep_count == 2
    assert description.temporal_start == START
    assert (description.time_dimension, description.y_dimension, description.x_dimension) == ("t", "y", "x")


def test_a_grid_crossing_the_antimeridian_can_be_queried(
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
):
    grid = build_antimeridian_grid()
    repository = RasterRepository(storage_backend, catalog, settings)
    repository.create("antimeridian", grid, build_cube(grid, count=1))
    repository.publish("antimeridian")

    whole = repository.query("antimeridian")
    eastern = repository.query(
        "antimeridian",
        bbox=BoundingBox(minimum_x=170.0, minimum_y=-10.0, maximum_x=180.0, maximum_y=10.0),
    )
    crossing = repository.query(
        "antimeridian",
        bbox=BoundingBox(minimum_x=175.0, minimum_y=-10.0, maximum_x=185.0, maximum_y=10.0),
    )

    assert whole.cell_count == 2 * 4
    assert eastern.cell_count == 2 * 2
    assert eastern.bbox.minimum_x == pytest.approx(170.0)
    assert eastern.bbox.maximum_x == pytest.approx(180.0)
    # The window wraps the same way the grid does, so it covers the cells on both sides of the line.
    assert crossing.cell_count == 2 * 2
    assert crossing.mean != pytest.approx(eastern.mean)
