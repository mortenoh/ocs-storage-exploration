from __future__ import annotations

from datetime import UTC, datetime

import numpy
import pytest
from pyproj import CRS

from ocs_storage_exploration.storage.errors import RasterContractError
from ocs_storage_exploration.storage.raster import (
    NODATA_ATTRIBUTE,
    PROJECTION_CODE_ATTRIBUTE,
    SPATIAL_BBOX_ATTRIBUTE,
    SPATIAL_REFERENCE_NAME,
    TimeStep,
    apply_geozarr_attributes,
    assert_finite_attributes,
    build_coordinates,
    build_synthetic_cube,
    build_timestamps,
)
from ocs_storage_exploration.storage.schemas import BoundingBox, GridSpecification

VARIABLE = "temperature"


def build_grid() -> GridSpecification:
    return GridSpecification(
        shape=(4, 6),
        bbox=BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=12.0, maximum_y=8.0),
        crs="EPSG:4326",
    )


def test_build_coordinates_returns_cell_centres_with_descending_y():
    coordinates = build_coordinates(build_grid())

    assert list(coordinates["y"]) == [7.0, 5.0, 3.0, 1.0]
    assert list(coordinates["x"]) == [1.0, 3.0, 5.0, 7.0, 9.0, 11.0]
    assert numpy.all(numpy.diff(coordinates["y"]) < 0)


def test_build_coordinates_wraps_longitudes_across_the_antimeridian():
    grid = GridSpecification(
        shape=(2, 4),
        bbox=BoundingBox(minimum_x=170.0, minimum_y=-10.0, maximum_x=190.0, maximum_y=10.0),
        crs="EPSG:4326",
    )

    coordinates = build_coordinates(grid)

    assert list(coordinates["x"]) == [172.5, 177.5, -177.5, -172.5]
    assert numpy.all(numpy.abs(coordinates["x"]) <= 180.0)


def test_build_coordinates_uses_the_dimension_names_of_the_grid():
    grid = build_grid().model_copy(update={"y_dimension": "latitude", "x_dimension": "longitude"})

    assert sorted(build_coordinates(grid)) == ["latitude", "longitude"]


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (TimeStep.DAY, datetime(2020, 2, 2)),
        (TimeStep.MONTH, datetime(2020, 3, 31)),
        (TimeStep.YEAR, datetime(2022, 1, 31)),
    ],
)
def test_build_timestamps_advances_by_the_requested_step(step: TimeStep, expected: datetime):
    timestamps = build_timestamps(datetime(2020, 1, 31), 3, step)

    assert len(timestamps) == 3
    assert timestamps[0] == datetime(2020, 1, 31)
    assert timestamps[-1] == expected


def test_build_timestamps_refuses_an_empty_series():
    with pytest.raises(RasterContractError):
        build_timestamps(datetime(2020, 1, 1), 0, TimeStep.DAY)


def test_build_synthetic_cube_has_time_y_x_dimensions_and_the_grid_dtype():
    grid = build_grid()
    timestamps = build_timestamps(datetime(2020, 1, 1, tzinfo=UTC), 3, TimeStep.DAY)

    cube = build_synthetic_cube(grid, variable=VARIABLE, timestamps=timestamps, seed=0)

    assert cube[VARIABLE].dims == ("t", "y", "x")
    assert dict(cube.sizes) == {"t": 3, "y": 4, "x": 6}
    assert cube[VARIABLE].dtype == numpy.dtype(grid.data_type)
    assert str(cube["t"].dtype).startswith("datetime64")
    assert numpy.isfinite(cube[VARIABLE].values).all()


def test_build_synthetic_cube_is_deterministic_for_one_seed():
    grid = build_grid()
    timestamps = build_timestamps(datetime(2020, 1, 1), 2, TimeStep.DAY)

    first = build_synthetic_cube(grid, variable=VARIABLE, timestamps=timestamps, seed=7)
    second = build_synthetic_cube(grid, variable=VARIABLE, timestamps=timestamps, seed=7)
    other = build_synthetic_cube(grid, variable=VARIABLE, timestamps=timestamps, seed=8)

    assert numpy.array_equal(first[VARIABLE].values, second[VARIABLE].values)
    assert not numpy.array_equal(first[VARIABLE].values, other[VARIABLE].values)


def test_apply_geozarr_attributes_adds_the_spatial_reference_coordinate():
    grid = build_grid()
    cube = build_synthetic_cube(grid, variable=VARIABLE, timestamps=build_timestamps(datetime(2020, 1, 1), 1), seed=0)

    decorated = apply_geozarr_attributes(cube, grid)

    assert SPATIAL_REFERENCE_NAME in decorated.coords
    assert CRS.from_wkt(str(decorated[SPATIAL_REFERENCE_NAME].attrs["crs_wkt"])) == CRS.from_user_input(grid.crs)
    assert decorated[SPATIAL_REFERENCE_NAME].attrs[SPATIAL_REFERENCE_NAME]
    assert decorated[VARIABLE].attrs["grid_mapping"] == SPATIAL_REFERENCE_NAME
    assert decorated.attrs[PROJECTION_CODE_ATTRIBUTE] == "EPSG:4326"
    assert decorated.attrs[SPATIAL_BBOX_ATTRIBUTE] == [0.0, 0.0, 12.0, 8.0]


def test_apply_geozarr_attributes_keeps_the_grid_attributes():
    grid = build_grid().model_copy(update={"attributes": {"institution": "ocs"}})
    cube = build_synthetic_cube(grid, variable=VARIABLE, timestamps=build_timestamps(datetime(2020, 1, 1), 1), seed=0)

    decorated = apply_geozarr_attributes(cube, grid)

    assert decorated.attrs["institution"] == "ocs"


def test_assert_finite_attributes_refuses_a_non_finite_value():
    grid = build_grid()
    cube = build_synthetic_cube(grid, variable=VARIABLE, timestamps=build_timestamps(datetime(2020, 1, 1), 1), seed=0)
    cube.attrs["offset"] = float("nan")

    with pytest.raises(RasterContractError, match="offset"):
        assert_finite_attributes(cube)


def test_assert_finite_attributes_refuses_a_non_finite_value_inside_a_sequence():
    grid = build_grid()
    cube = build_synthetic_cube(grid, variable=VARIABLE, timestamps=build_timestamps(datetime(2020, 1, 1), 1), seed=0)
    cube[VARIABLE].attrs["limits"] = [1.0, float("inf")]

    with pytest.raises(RasterContractError, match="limits"):
        assert_finite_attributes(cube)


def build_single_step_cube(grid: GridSpecification):
    return build_synthetic_cube(grid, variable=VARIABLE, timestamps=build_timestamps(datetime(2020, 1, 1), 1), seed=0)


def test_apply_geozarr_attributes_stamps_the_nodata_value_the_grid_declares():
    grid = build_grid()
    cube = build_single_step_cube(grid)
    assert NODATA_ATTRIBUTE not in cube[VARIABLE].attrs

    decorated = apply_geozarr_attributes(cube, grid.model_copy(update={"nodata_value": -9999.0}))

    assert decorated[VARIABLE].attrs[NODATA_ATTRIBUTE] == -9999.0


def test_apply_geozarr_attributes_keeps_the_nodata_value_a_variable_already_carries():
    grid = build_grid().model_copy(update={"nodata_value": -9999.0})
    cube = build_single_step_cube(grid)
    cube[VARIABLE].attrs[NODATA_ATTRIBUTE] = -1.0

    decorated = apply_geozarr_attributes(cube, grid)

    assert decorated[VARIABLE].attrs[NODATA_ATTRIBUTE] == -1.0


def test_apply_geozarr_attributes_stamps_no_nodata_value_when_the_grid_declares_none():
    grid = build_grid()

    decorated = apply_geozarr_attributes(build_single_step_cube(grid), grid)

    assert NODATA_ATTRIBUTE not in decorated[VARIABLE].attrs
