"""Grid coordinates, synthetic cubes and the GeoZarr attributes a coverage is written with."""

from __future__ import annotations

import calendar
import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import numpy
import xarray
from numpy.typing import NDArray
from pyproj import CRS

from ocs_storage_exploration.storage.errors import RasterContractError
from ocs_storage_exploration.storage.schemas import GridSpecification

SPATIAL_REFERENCE_NAME: Final[str] = "spatial_ref"
GRID_MAPPING_ATTRIBUTE: Final[str] = "grid_mapping"
CRS_WELL_KNOWN_TEXT_ATTRIBUTE: Final[str] = "crs_wkt"
PROJECTION_CODE_ATTRIBUTE: Final[str] = "proj:code"
SPATIAL_BBOX_ATTRIBUTE: Final[str] = "spatial:bbox"
NODATA_ATTRIBUTE: Final[str] = "nodata"
MINIMUM_LONGITUDE: Final[float] = -180.0
MAXIMUM_LONGITUDE: Final[float] = 180.0
LONGITUDE_SPAN: Final[float] = 360.0
MONTHS_PER_YEAR: Final[int] = 12
NOISE_SCALE: Final[float] = 0.01
# The span a nanosecond datetime64 covers, rounded inwards onto the microsecond resolution a Python
# datetime holds. It bounds every stored time coordinate rather than only the ones written as
# nanoseconds: xarray decodes a time axis onto nanoseconds whatever unit it was written with, so a
# coarser coordinate outside this span cannot be read back either.
MINIMUM_TIMESTAMP: Final[datetime] = datetime(1677, 9, 21, 0, 12, 43, 145225)
MAXIMUM_TIMESTAMP: Final[datetime] = datetime(2262, 4, 11, 23, 47, 16, 854775)

FloatArray = NDArray[numpy.float64]


class TimeStep(StrEnum):
    """Calendar step separating two timestamps of a synthetic cube."""

    DAY = "day"
    MONTH = "month"
    YEAR = "year"


def build_cell_sizes(grid: GridSpecification) -> tuple[float, float]:
    """Return the y and x cell sizes of a grid in the units of its coordinate reference system."""
    rows, columns = grid.shape
    return (
        (grid.bbox.maximum_y - grid.bbox.minimum_y) / rows,
        (grid.bbox.maximum_x - grid.bbox.minimum_x) / columns,
    )


def build_coordinates(grid: GridSpecification) -> dict[str, FloatArray]:
    """Return the cell centre coordinates of a grid, with y descending and longitudes wrapped."""
    rows, columns = grid.shape
    y_size, x_size = build_cell_sizes(grid)
    y_values = grid.bbox.maximum_y - (numpy.arange(rows, dtype="float64") + 0.5) * y_size
    x_values = grid.bbox.minimum_x + (numpy.arange(columns, dtype="float64") + 0.5) * x_size
    if CRS.from_user_input(grid.crs).is_geographic:
        x_values = wrap_longitudes(x_values)
    return {grid.y_dimension: y_values, grid.x_dimension: x_values}


def wrap_longitudes(values: FloatArray) -> FloatArray:
    """Wrap longitudes that fall outside minus 180 to 180 degrees back into that range."""
    inside = (values >= MINIMUM_LONGITUDE) & (values <= MAXIMUM_LONGITUDE)
    wrapped = ((values - MINIMUM_LONGITUDE) % LONGITUDE_SPAN) + MINIMUM_LONGITUDE
    return numpy.asarray(numpy.where(inside, values, wrapped), dtype="float64")


def build_timestamps(start: datetime, count: int, step: TimeStep = TimeStep.DAY) -> list[datetime]:
    """Build count timestamps starting at start and advancing by whole days, months or years."""
    if count < 1:
        raise RasterContractError(f"timestep count must be at least one: {count}")
    resolved = TimeStep(step)
    return [advance_timestamp(start, index, resolved) for index in range(count)]


def advance_timestamp(start: datetime, steps: int, step: TimeStep) -> datetime:
    """Advance a timestamp by a whole number of days, months or years, clamping the day of month."""
    if step is TimeStep.DAY:
        return start + timedelta(days=steps)
    months = steps * MONTHS_PER_YEAR if step is TimeStep.YEAR else steps
    absolute_month = start.month - 1 + months
    year = start.year + absolute_month // MONTHS_PER_YEAR
    month = absolute_month % MONTHS_PER_YEAR + 1
    return start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))


def to_naive_utc(value: datetime) -> datetime:
    """Return a timestamp as naive UTC because Zarr stores datetimes without a time zone."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def to_datetime64(value: datetime) -> numpy.datetime64:
    """Convert one timestamp into the naive UTC datetime64 a Zarr time coordinate holds, refusing an unholdable one."""
    naive = to_naive_utc(value)
    # numpy wraps rather than raises here, so an unchecked conversion of the year 2500 stores the year
    # 1915 and the coverage silently describes a time axis nobody asked for.
    if naive < MINIMUM_TIMESTAMP or naive > MAXIMUM_TIMESTAMP:
        raise RasterContractError(
            f"timestamp {naive.isoformat()} is outside the range a time coordinate holds, "
            f"{MINIMUM_TIMESTAMP.isoformat()} to {MAXIMUM_TIMESTAMP.isoformat()}",
        )
    return numpy.datetime64(naive, "ns")


def clamp_to_datetime64(value: datetime) -> numpy.datetime64:
    """Convert one query bound into a datetime64, clamping it onto the range rather than refusing it."""
    # Everything up to the year 3000 is a window a reader is entitled to ask for, and it selects every
    # timestep there is. Only a wrapped bound is wrong, so the bound is moved onto the end it ran past.
    naive = min(max(to_naive_utc(value), MINIMUM_TIMESTAMP), MAXIMUM_TIMESTAMP)
    return numpy.datetime64(naive, "ns")


def assert_variable_names_available(
    names: Iterable[Any],
    *,
    time_dimension: str,
    y_dimension: str,
    x_dimension: str,
) -> None:
    """Refuse data variable names that a coordinate of the written coverage already takes."""
    reserved = {SPATIAL_REFERENCE_NAME, time_dimension, y_dimension, x_dimension}
    # Assigning the coordinate would replace a data variable of the same name, so the coverage would be
    # written, and reported as created, with the data of that variable silently gone.
    taken = sorted({str(name) for name in names} & reserved)
    if taken:
        raise RasterContractError(
            f"variable names {taken} are taken by the coordinates a coverage is written with, {sorted(reserved)}",
        )


def build_synthetic_cube(
    grid: GridSpecification,
    *,
    variable: str,
    timestamps: Sequence[datetime],
    seed: int = 0,
) -> xarray.Dataset:
    """Build a deterministic synthetic cube of one variable with dimensions time, y and x."""
    if not timestamps:
        raise RasterContractError("a synthetic cube needs at least one timestamp")
    if not variable:
        raise RasterContractError("a synthetic cube needs a variable name")
    assert_variable_names_available(
        [variable],
        time_dimension=grid.time_dimension,
        y_dimension=grid.y_dimension,
        x_dimension=grid.x_dimension,
    )
    coordinates = build_coordinates(grid)
    y_values = coordinates[grid.y_dimension]
    x_values = coordinates[grid.x_dimension]
    shape = (len(timestamps), y_values.size, x_values.size)
    pattern = numpy.sin(numpy.radians(y_values))[None, :, None] * numpy.cos(numpy.radians(x_values))[None, None, :]
    offsets = numpy.arange(len(timestamps), dtype="float64")[:, None, None]
    noise = numpy.random.default_rng(seed).normal(loc=0.0, scale=NOISE_SCALE, size=shape)
    values = numpy.asarray(pattern + offsets + noise, dtype=numpy.dtype(grid.data_type))
    dimensions = (grid.time_dimension, grid.y_dimension, grid.x_dimension)
    return xarray.Dataset(
        {variable: (dimensions, values, _variable_attributes(grid, variable))},
        coords={
            grid.time_dimension: (grid.time_dimension, _as_datetime64(timestamps), {"standard_name": "time"}),
            grid.y_dimension: (grid.y_dimension, y_values, _axis_attributes(grid, vertical=True)),
            grid.x_dimension: (grid.x_dimension, x_values, _axis_attributes(grid, vertical=False)),
        },
        attrs=dict(grid.attributes),
    )


def apply_geozarr_attributes(dataset: xarray.Dataset, grid: GridSpecification) -> xarray.Dataset:
    """Attach the CF grid mapping coordinate and the GeoZarr root attributes to a dataset."""
    assert_variable_names_available(
        dataset.data_vars,
        time_dimension=grid.time_dimension,
        y_dimension=grid.y_dimension,
        x_dimension=grid.x_dimension,
    )
    crs = CRS.from_user_input(grid.crs)
    well_known_text = crs.to_wkt()
    decorated = dataset.assign_coords({SPATIAL_REFERENCE_NAME: numpy.int32(0)})
    decorated[SPATIAL_REFERENCE_NAME].attrs = {
        CRS_WELL_KNOWN_TEXT_ATTRIBUTE: well_known_text,
        SPATIAL_REFERENCE_NAME: well_known_text,
    }
    for name in decorated.data_vars:
        decorated[name].attrs[GRID_MAPPING_ATTRIBUTE] = SPATIAL_REFERENCE_NAME
        # The fill value belongs on the variable, so a reader of one snapshot never has to consult a
        # record that describes the newest write instead.
        if grid.nodata_value is not None:
            decorated[name].attrs.setdefault(NODATA_ATTRIBUTE, float(grid.nodata_value))
    decorated.attrs.update(grid.attributes)
    decorated.attrs[PROJECTION_CODE_ATTRIBUTE] = projection_code(grid.crs)
    decorated.attrs[SPATIAL_BBOX_ATTRIBUTE] = list(grid.bbox.as_tuple())
    assert_finite_attributes(decorated)
    return decorated


def projection_code(crs: str) -> str:
    """Return the authority code of a coordinate reference system, or the requested name."""
    authority = CRS.from_user_input(crs).to_authority()
    if authority is None:
        return crs
    return f"{authority[0]}:{authority[1]}"


def assert_finite_attributes(dataset: xarray.Dataset) -> None:
    """Refuse NaN or infinite attribute values anywhere in a dataset because Icechunk rejects them."""
    _assert_finite_mapping(dataset.attrs, "dataset")
    for name, variable in dataset.variables.items():
        _assert_finite_mapping(variable.attrs, str(name))


def _assert_finite_mapping(attributes: Mapping[Any, Any], owner: str) -> None:
    """Refuse NaN or infinite values in one attribute mapping."""
    for key, value in attributes.items():
        for number in _iter_numbers(value):
            if not math.isfinite(number):
                raise RasterContractError(f"attribute {str(key)!r} of {owner} is not finite: {value!r}")


def _iter_numbers(value: Any) -> Iterator[float]:
    """Yield every number reachable from an attribute value."""
    if isinstance(value, bool):
        return
    if isinstance(value, int | float | numpy.integer | numpy.floating):
        yield float(value)
        return
    if isinstance(value, list | tuple | numpy.ndarray):
        for item in value:
            yield from _iter_numbers(item)


def _as_datetime64(timestamps: Sequence[datetime]) -> NDArray[numpy.datetime64]:
    """Convert timestamps into the naive UTC datetime64 array a Zarr coordinate holds."""
    return numpy.array([to_datetime64(value) for value in timestamps], dtype="datetime64[ns]")


def _variable_attributes(grid: GridSpecification, variable: str) -> dict[str, Any]:
    """Return the attributes of a synthetic data variable, including its fill value."""
    attributes: dict[str, Any] = {"long_name": f"synthetic {variable}", "units": "1"}
    if grid.nodata_value is not None:
        attributes[NODATA_ATTRIBUTE] = float(grid.nodata_value)
    return attributes


def _axis_attributes(grid: GridSpecification, *, vertical: bool) -> dict[str, Any]:
    """Return the CF attributes of one spatial axis of a grid."""
    if CRS.from_user_input(grid.crs).is_geographic:
        if vertical:
            return {"standard_name": "latitude", "units": "degrees_north", "axis": "Y"}
        return {"standard_name": "longitude", "units": "degrees_east", "axis": "X"}
    if vertical:
        return {"standard_name": "projection_y_coordinate", "units": "m", "axis": "Y"}
    return {"standard_name": "projection_x_coordinate", "units": "m", "axis": "X"}
