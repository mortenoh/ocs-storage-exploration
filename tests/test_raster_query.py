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
from ocs_storage_exploration.storage.raster.repository import _coordinate_indices
from ocs_storage_exploration.storage.schemas import BoundingBox, GridSpecification

IDENTIFIER = "query"
GLOBAL_IDENTIFIER = "global"
VARIABLE = "temperature"
START = datetime(2020, 1, 1)
NORTHERN_HALF = BoundingBox(minimum_x=0.0, minimum_y=4.0, maximum_x=12.0, maximum_y=8.0)
# The cell centres of an eight column global grid, which is what build_global_grid writes.
GLOBAL_CENTRES = numpy.asarray([-157.5, -112.5, -67.5, -22.5, 22.5, 67.5, 112.5, 157.5], dtype="float64")


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


def test_a_draft_nodata_value_does_not_change_the_published_statistics(
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
):
    repository = RasterRepository(storage_backend, catalog, settings)
    grid = build_grid()
    zeros = build_cube(grid, count=1)
    zeros[VARIABLE].values[:] = 0.0
    repository.create("zeros", grid, zeros)
    repository.publish("zeros")
    # The draft declares zero as its fill value; the snapshot on the published branch never did.
    draft_grid = grid.model_copy(update={"nodata_value": 0.0})
    repository.create("zeros", draft_grid, build_cube(draft_grid, count=1), overwrite=True)

    summary = repository.query("zeros")

    assert summary.minimum == 0.0
    assert summary.maximum == 0.0
    assert summary.mean == 0.0


def write_jumbled_coverage(
    repository: RasterRepository,
    grid: GridSpecification,
    monkeypatch: pytest.MonkeyPatch,
) -> xarray.Dataset:
    """Write a coverage whose time axis runs January then December, which only an unguarded append can build."""
    repository.create("jumbled", grid, build_cube(grid, count=2))
    earlier = build_synthetic_cube(
        grid,
        variable=VARIABLE,
        timestamps=build_timestamps(datetime(2019, 12, 30), 2, TimeStep.DAY),
        seed=1,
    )
    monkeypatch.setattr(repository, "_assert_appendable", lambda *arguments, **keywords: None)
    repository.append("jumbled", earlier)
    repository.publish("jumbled")
    return earlier


def test_a_query_on_a_non_monotonic_time_axis_reads_a_window_with_no_matching_label(
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = RasterRepository(storage_backend, catalog, settings)
    earlier = write_jumbled_coverage(repository, build_grid(), monkeypatch)

    summary = repository.query("jumbled", start=datetime(2019, 12, 29), end=datetime(2019, 12, 31))

    assert summary.timestep_count == 2
    assert summary.cell_count == 2 * 4 * 6
    assert summary.mean == pytest.approx(float(earlier[VARIABLE].values.mean()), abs=1e-5)


def test_a_query_spanning_a_non_monotonic_time_axis_reads_every_timestep_inside_it(
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = RasterRepository(storage_backend, catalog, settings)
    write_jumbled_coverage(repository, build_grid(), monkeypatch)

    summary = repository.query("jumbled", start=datetime(2019, 12, 30), end=datetime(2020, 1, 2))

    assert summary.timestep_count == 4
    assert summary.cell_count == 4 * 4 * 6


def build_global_grid() -> GridSpecification:
    return GridSpecification(
        shape=(2, 8),
        bbox=BoundingBox(minimum_x=-180.0, minimum_y=-90.0, maximum_x=180.0, maximum_y=90.0),
        crs="EPSG:4326",
    )


@pytest.fixture
def global_repository(
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
) -> RasterRepository:
    repository = RasterRepository(storage_backend, catalog, settings)
    grid = build_global_grid()
    repository.create(GLOBAL_IDENTIFIER, grid, build_cube(grid, count=1))
    repository.publish(GLOBAL_IDENTIFIER)
    return repository


def test_a_full_circle_longitude_window_selects_every_cell():
    # Wrapping the two endpoints on their own folds each of these onto one meridian.
    assert _coordinate_indices(GLOBAL_CENTRES, 0.0, 360.0, wrap=True).tolist() == list(range(8))
    assert _coordinate_indices(GLOBAL_CENTRES, -180.0, 180.0, wrap=True).tolist() == list(range(8))
    assert _coordinate_indices(GLOBAL_CENTRES, 10.0, 370.0, wrap=True).tolist() == list(range(8))
    assert _coordinate_indices(GLOBAL_CENTRES, -180.0, 540.0, wrap=True).tolist() == list(range(8))


def test_a_longitude_window_of_no_width_stays_a_single_meridian():
    meridians = numpy.asarray([-180.0, -90.0, 0.0, 90.0], dtype="float64")

    assert _coordinate_indices(meridians, 0.0, 0.0, wrap=True).tolist() == [2]
    assert _coordinate_indices(GLOBAL_CENTRES, 0.0, 0.0, wrap=True).tolist() == []


def test_a_near_full_longitude_window_still_covers_both_halves():
    # 359 degrees wide, so the only gap is between minus one and zero, where no cell centre sits.
    assert _coordinate_indices(GLOBAL_CENTRES, 0.0, 359.0, wrap=True).tolist() == list(range(8))


def test_a_longitude_window_crossing_the_antimeridian_is_the_union_of_its_halves():
    assert _coordinate_indices(GLOBAL_CENTRES, 150.0, 210.0, wrap=True).tolist() == [0, 7]


def test_a_projected_axis_reads_a_full_circle_span_as_plain_numbers():
    metres = numpy.asarray([0.0, 200.0, 400.0, 600.0], dtype="float64")

    assert _coordinate_indices(metres, 0.0, 360.0, wrap=False).tolist() == [0, 1]


def test_a_global_query_selects_every_cell_in_either_longitude_convention(global_repository: RasterRepository):
    whole = global_repository.query(GLOBAL_IDENTIFIER)

    eastward = global_repository.query(
        GLOBAL_IDENTIFIER,
        bbox=BoundingBox(minimum_x=0.0, minimum_y=-90.0, maximum_x=360.0, maximum_y=90.0),
    )
    signed = global_repository.query(
        GLOBAL_IDENTIFIER,
        bbox=BoundingBox(minimum_x=-180.0, minimum_y=-90.0, maximum_x=180.0, maximum_y=90.0),
    )

    assert whole.cell_count == 2 * 8
    assert eastward.cell_count == whole.cell_count
    assert signed.cell_count == whole.cell_count
    assert whole.mean is not None
    assert eastward.mean == pytest.approx(whole.mean)
    assert signed.mean == pytest.approx(whole.mean)
    assert eastward.bbox.as_tuple() == pytest.approx((-180.0, -90.0, 180.0, 90.0))
    assert signed.bbox.as_tuple() == pytest.approx((-180.0, -90.0, 180.0, 90.0))


def test_a_global_query_across_the_antimeridian_still_reads_two_halves(global_repository: RasterRepository):
    crossing = global_repository.query(
        GLOBAL_IDENTIFIER,
        bbox=BoundingBox(minimum_x=150.0, minimum_y=-90.0, maximum_x=210.0, maximum_y=90.0),
    )

    assert crossing.cell_count == 2 * 2
    assert crossing.bbox.minimum_x == pytest.approx(-180.0)
    assert crossing.bbox.maximum_x == pytest.approx(180.0)
