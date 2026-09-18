"""Reads real raster files and normalises them onto the coverage contract the Icechunk engine writes."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, cast

import numpy
import rioxarray  # noqa: F401  # imported for the .rio accessor the GeoTIFF path and clip_box use
import xarray
import xarray.backends
from numpy.typing import NDArray
from pyproj import CRS

from ocs_storage_exploration.storage.errors import (
    BackendNotSupportedError,
    QuerySizeGuardError,
    RasterContractError,
)
from ocs_storage_exploration.storage.paths import relative_to_working_directory, resolve_ingest_paths
from ocs_storage_exploration.storage.raster.grid import (
    MAXIMUM_LONGITUDE,
    NODATA_ATTRIBUTE,
    PROJECTION_CODE_ATTRIBUTE,
    SPATIAL_REFERENCE_NAME,
    apply_geozarr_attributes,
    assert_variable_names_available,
    projection_code,
    to_datetime64,
    to_naive_utc,
)
from ocs_storage_exploration.storage.raster.repository import RasterRepository
from ocs_storage_exploration.storage.schemas import (
    BoundingBox,
    GridSpecification,
    RasterIngestResult,
)

DEFAULT_TIME_DIMENSION: Final[str] = "t"
DEFAULT_Y_DIMENSION: Final[str] = "y"
DEFAULT_X_DIMENSION: Final[str] = "x"
BAND_DIMENSION: Final[str] = "band"
DEFAULT_CRS: Final[str] = "EPSG:4326"
DEFAULT_FILENAME_DATE_PATTERN: Final[str] = r"(\d{4}-\d{2}-\d{2})"
# What the cube guard calls a source that is normalised without a file behind it to name.
DEFAULT_SOURCE_LABEL: Final[str] = "raster"

# Source spellings of each axis, first match wins. They mirror the ones the Open Climate Service
# normalises today, so a file that works there works here.
X_AXIS_NAMES: Final[tuple[str, ...]] = ("x", "X", "lon", "longitude")
Y_AXIS_NAMES: Final[tuple[str, ...]] = ("y", "Y", "lat", "latitude")
TIME_AXIS_NAMES: Final[tuple[str, ...]] = ("t", "time", "valid_time", "date", "time_counter")

GEOTIFF_SUFFIXES: Final[frozenset[str]] = frozenset({".tif", ".tiff", ".cog", ".gtiff"})
NETCDF_SUFFIXES: Final[frozenset[str]] = frozenset({".nc", ".nc4", ".cdf", ".netcdf"})
ZARR_SUFFIXES: Final[frozenset[str]] = frozenset({".zarr"})

# The one NetCDF reader this project installs. xarray ships none of its own, so a deployment without
# it is reported as an unsupported backend rather than as a malformed file. The literal type is what
# lets a caller hand the name straight to the xarray readers and writers, which take a Literal.
NETCDF_ENGINE: Final = "h5netcdf"

# Attributes that describe how a file was encoded rather than what it holds. Carrying them into Zarr
# would make a reader decode the values a second time, so they are dropped with the encoding.
CF_ENCODING_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {"_FillValue", "missing_value", "scale_factor", "add_offset", "grid_mapping", "coordinates"},
)
# Above this a geographic x axis is on the 0 to 360 frame rather than on minus 180 to 180.
LONGITUDE_ROLL_THRESHOLD: Final[float] = MAXIMUM_LONGITUDE
# A cell size is measured from the coordinate steps, which a real grid holds to within rounding only.
CELL_SIZE_TOLERANCE: Final[float] = 1e-6

FloatArray = NDArray[numpy.float64]


@dataclass(frozen=True, slots=True)
class RasterIngestPlan:
    """One resolved raster ingest: the files in timestamp order, plus the metadata the coverage records."""

    files: tuple[Path, ...]
    timestamps: tuple[datetime, ...]
    variable: str
    bbox: BoundingBox | None = None
    title: str | None = None
    license: str | None = None
    attribution: str | None = None
    overwrite: bool = False
    publish: bool = False
    working_directory: Path | None = None
    # The cell guard of the deployment that resolved the plan. It travels on the plan because the
    # engine keeps its settings to itself, and reading a file is what has to be stopped in time.
    max_cube_cells: int | None = None

    def __post_init__(self) -> None:
        """Refuse a plan whose files and timestamps do not line up one to one."""
        if not self.files:
            raise RasterContractError("a raster ingest needs at least one file")
        if len(self.files) != len(self.timestamps):
            raise RasterContractError(
                f"a raster ingest needs one timestamp per file: {len(self.files)} files, "
                f"{len(self.timestamps)} timestamps",
            )


def build_raster_ingest_plan(
    *,
    files: Sequence[str],
    variable: str,
    roots: Sequence[Path],
    timestamps: Sequence[datetime] | None = None,
    timestamp: datetime | None = None,
    filename_date_pattern: str = DEFAULT_FILENAME_DATE_PATTERN,
    bbox: BoundingBox | None = None,
    title: str | None = None,
    license: str | None = None,
    attribution: str | None = None,
    overwrite: bool = False,
    publish: bool = False,
    working_directory: Path | None = None,
    max_cube_cells: int | None = None,
) -> RasterIngestPlan:
    """Expand the requested globs, resolve one timestamp per file and order the pair by timestamp."""
    base = (working_directory if working_directory is not None else Path.cwd()).resolve()
    resolved = resolve_ingest_paths(files, roots=roots, working_directory=base)
    resolved_timestamps = _resolve_timestamps(
        resolved,
        timestamps=timestamps,
        timestamp=timestamp,
        filename_date_pattern=filename_date_pattern,
    )
    ordered = sorted(zip(resolved, resolved_timestamps, strict=True), key=lambda pair: pair[1])
    return RasterIngestPlan(
        files=tuple(path for path, _ in ordered),
        timestamps=tuple(moment for _, moment in ordered),
        variable=variable,
        bbox=bbox,
        title=title,
        license=license,
        attribution=attribution,
        overwrite=overwrite,
        publish=publish,
        working_directory=base,
        max_cube_cells=max_cube_cells,
    )


def ingest_raster_files(
    repository: RasterRepository,
    dataset_identifier: str,
    plan: RasterIngestPlan,
) -> RasterIngestResult:
    """Write the first file of a plan as a new coverage and append the rest in timestamp order."""
    first = open_raster_file(
        plan.files[0],
        variable=plan.variable,
        timestamp=plan.timestamps[0],
        bbox=plan.bbox,
        max_cube_cells=plan.max_cube_cells,
    )
    grid = grid_from_dataset(first, variable=plan.variable)
    result = repository.create(
        dataset_identifier,
        grid,
        first,
        title=plan.title,
        license=plan.license,
        attribution=plan.attribution,
        overwrite=plan.overwrite,
        message=f"ingest {plan.files[0].name}",
    )
    for path, moment in zip(plan.files[1:], plan.timestamps[1:], strict=True):
        appended = open_raster_file(
            path,
            variable=plan.variable,
            timestamp=moment,
            bbox=plan.bbox,
            max_cube_cells=plan.max_cube_cells,
        )
        result = repository.append(dataset_identifier, appended, message=f"ingest {path.name}")
    published = result.published
    if plan.publish:
        repository.publish(dataset_identifier, snapshot_identifier=result.snapshot_identifier)
        published = True
    return RasterIngestResult(
        dataset_identifier=result.dataset_identifier,
        snapshot_identifier=result.snapshot_identifier,
        timestep_count=result.timestep_count,
        variables=result.variables,
        published=published,
        files=tuple(
            relative_to_working_directory(path, working_directory=plan.working_directory) for path in plan.files
        ),
        timestamps=plan.timestamps,
    )


def open_raster_file(
    path: Path,
    *,
    variable: str,
    timestamp: datetime | None = None,
    bbox: BoundingBox | None = None,
    nodata_value: float | None = None,
    max_cube_cells: int | None = None,
    time_dimension: str = DEFAULT_TIME_DIMENSION,
    y_dimension: str = DEFAULT_Y_DIMENSION,
    x_dimension: str = DEFAULT_X_DIMENSION,
) -> xarray.Dataset:
    """Open one GeoTIFF, COG, NetCDF or Zarr file and normalise it onto the coverage contract."""
    opened = _open_source(path)
    try:
        normalised = normalise_for_contract(
            opened,
            variable=variable,
            timestamp=timestamp,
            bbox=bbox,
            nodata_value=nodata_value,
            max_cube_cells=max_cube_cells,
            source_label=path.name,
            time_dimension=time_dimension,
            y_dimension=y_dimension,
            x_dimension=x_dimension,
        )
        # The NetCDF and Zarr readers stay lazy, and the source is closed below, so the values are
        # read here rather than left as a handle onto a store that is gone by the time it is written.
        return normalised.load()
    finally:
        opened.close()


def normalise_for_contract(
    dataset: xarray.Dataset | xarray.DataArray,
    *,
    variable: str,
    timestamp: datetime | None = None,
    bbox: BoundingBox | None = None,
    nodata_value: float | None = None,
    max_cube_cells: int | None = None,
    source_label: str = DEFAULT_SOURCE_LABEL,
    time_dimension: str = DEFAULT_TIME_DIMENSION,
    y_dimension: str = DEFAULT_Y_DIMENSION,
    x_dimension: str = DEFAULT_X_DIMENSION,
) -> xarray.Dataset:
    """Bring an opened raster up to the contract: t, y and x axes, y descending, longitudes wrapped, nodata masked.

    The order is the one the Open Climate Service settled on and is not arbitrary: names first because
    everything after addresses axes by name, then the projection because whether the x axis is a longitude
    decides whether wrapping it is correct or destructive, then the reorderings that need that answer.
    """
    if not variable:
        raise RasterContractError("an ingested raster needs a variable name")
    # The name is applied by renaming the variable the source carries, so a reserved one is refused here
    # rather than after the rename has already put the data where a coordinate is about to go.
    assert_variable_names_available(
        [variable],
        time_dimension=time_dimension,
        y_dimension=y_dimension,
        x_dimension=x_dimension,
    )
    renamed = _rename_axes(
        _drop_curvilinear_coordinates(dataset, x_dimension=x_dimension, y_dimension=y_dimension),
        time_dimension=time_dimension,
        y_dimension=y_dimension,
        x_dimension=x_dimension,
    )
    clipped = _clip_to_bbox(renamed, bbox)
    squeezed = _squeeze_band(clipped)
    selected = _select_variable(squeezed, variable)
    # The last point at which every reader is still lazy: the axes are named and the bounding box has
    # already shrunk them, while masking the fill value below reads every cell of the source. The
    # engine guards the cube again before it writes, and by then the cells are in memory.
    _assert_cube_size(
        selected,
        source_label=source_label,
        max_cube_cells=max_cube_cells,
        time_dimension=time_dimension,
        y_dimension=y_dimension,
        x_dimension=x_dimension,
    )
    crs = _resolve_crs(selected, y_dimension=y_dimension, x_dimension=x_dimension)
    geographic = bool(CRS.from_user_input(crs).is_geographic)
    wrapped = _wrap_longitudes(selected, x_dimension=x_dimension, geographic=geographic)
    descending = _order_y_descending(wrapped, y_dimension=y_dimension)
    masked = _mask_nodata(descending, variable=variable, nodata_value=nodata_value)
    stamped = _stamp_timestamp(masked, timestamp, time_dimension=time_dimension)
    ordered = _order_dimensions(
        stamped,
        variable=variable,
        time_dimension=time_dimension,
        y_dimension=y_dimension,
        x_dimension=x_dimension,
    )
    cleaned = _drop_encoding_and_unusable_attributes(ordered)
    grid = grid_from_dataset(
        cleaned,
        variable=variable,
        crs=crs,
        time_dimension=time_dimension,
        y_dimension=y_dimension,
        x_dimension=x_dimension,
    )
    return apply_geozarr_attributes(cleaned, grid)


def grid_from_dataset(
    dataset: xarray.Dataset,
    *,
    variable: str,
    crs: str | None = None,
    time_dimension: str = DEFAULT_TIME_DIMENSION,
    y_dimension: str = DEFAULT_Y_DIMENSION,
    x_dimension: str = DEFAULT_X_DIMENSION,
) -> GridSpecification:
    """Derive the grid of a normalised dataset from its own shape, coordinates and projection."""
    if variable not in dataset.data_vars:
        raise RasterContractError(f"variable {variable!r} is not one of {sorted(str(name) for name in dataset)}")
    array = dataset[variable]
    y_values = _axis_values(dataset, y_dimension)
    x_values = _axis_values(dataset, x_dimension)
    y_cell_size = _cell_size(y_values, axis=y_dimension)
    x_cell_size = _cell_size(x_values, axis=x_dimension)
    resolved_crs = crs if crs is not None else _resolve_crs(dataset, y_dimension=y_dimension, x_dimension=x_dimension)
    return GridSpecification(
        shape=(int(y_values.size), int(x_values.size)),
        bbox=BoundingBox(
            minimum_x=float(x_values.min()) - x_cell_size / 2,
            minimum_y=float(y_values.min()) - y_cell_size / 2,
            maximum_x=float(x_values.max()) + x_cell_size / 2,
            maximum_y=float(y_values.max()) + y_cell_size / 2,
        ),
        crs=projection_code(resolved_crs),
        data_type=str(numpy.dtype(array.dtype)),
        nodata_value=_finite_number(array.attrs.get(NODATA_ATTRIBUTE)),
        time_dimension=time_dimension,
        y_dimension=y_dimension,
        x_dimension=x_dimension,
    )


def timestamp_from_filename(path: Path, *, pattern: str = DEFAULT_FILENAME_DATE_PATTERN) -> datetime:
    """Read the timestamp a file name carries, using the first match of the configured pattern."""
    try:
        expression = re.compile(pattern)
    except re.error as error:
        raise RasterContractError(f"filename date pattern {pattern!r} is not a regular expression: {error}") from error
    match = expression.search(path.name)
    if match is None:
        raise RasterContractError(f"file name {path.name!r} carries no timestamp matching {pattern!r}")
    parts = [group for group in match.groups() if group is not None] or [match.group(0)]
    text = "-".join(parts) if len(parts) > 1 else parts[0]
    try:
        return datetime.fromisoformat(text)
    except ValueError as error:
        raise RasterContractError(f"timestamp {text!r} read from {path.name!r} is not an ISO timestamp") from error


def _resolve_timestamps(
    files: Sequence[Path],
    *,
    timestamps: Sequence[datetime] | None,
    timestamp: datetime | None,
    filename_date_pattern: str,
) -> list[datetime]:
    """Return one timestamp per file, from the explicit list, the single timestamp or the file names."""
    if timestamps is not None and timestamp is not None:
        raise RasterContractError("an ingest takes either timestamps or a single timestamp, not both")
    if timestamp is not None:
        if len(files) != 1:
            raise RasterContractError(
                f"a single timestamp describes a single file, but {len(files)} files were resolved",
            )
        return [_representable_naive_utc(timestamp)]
    if timestamps is not None:
        if len(timestamps) != len(files):
            raise RasterContractError(
                f"{len(timestamps)} timestamps were given for {len(files)} files, which must match one to one",
            )
        return [_representable_naive_utc(value) for value in timestamps]
    return [_representable_naive_utc(timestamp_from_filename(path, pattern=filename_date_pattern)) for path in files]


def _representable_naive_utc(value: datetime) -> datetime:
    """Return a timestamp as naive UTC, refusing at plan time one no time coordinate can hold."""
    # The conversion is the range check, so a plan carrying an unholdable timestamp is refused before a
    # single file is read rather than half way through the ingest.
    to_datetime64(value)
    return to_naive_utc(value)


def _open_source(path: Path) -> xarray.Dataset | xarray.DataArray:
    """Open one raster file with the reader its suffix names."""
    suffix = path.suffix.lower()
    if suffix in GEOTIFF_SUFFIXES:
        # mask_and_scale applies the band scale factor and offset, so a scaled integer source is read
        # in the units it declares rather than in raw counts. It also masks the fill value to NaN.
        opened = rioxarray.open_rasterio(path, mask_and_scale=True)
        if isinstance(opened, list):
            raise RasterContractError(f"{path.name!r} holds several subdatasets, which ingest does not split")
        return opened
    if suffix in NETCDF_SUFFIXES:
        return _open_netcdf(path)
    if suffix in ZARR_SUFFIXES:
        opened_zarr: xarray.Dataset = xarray.open_zarr(path, decode_coords="all")
        return opened_zarr
    raise RasterContractError(
        f"{path.name!r} has no readable raster suffix: expected one of "
        f"{sorted(GEOTIFF_SUFFIXES | NETCDF_SUFFIXES | ZARR_SUFFIXES)}",
    )


def _open_netcdf(path: Path) -> xarray.Dataset:
    """Open one NetCDF file with the pinned engine, reporting a deployment without it as unsupported."""
    if NETCDF_ENGINE not in xarray.backends.list_engines():
        raise BackendNotSupportedError(
            f"reading {path.name!r} needs the {NETCDF_ENGINE!r} xarray engine, which this deployment does not install",
        )
    try:
        return xarray.open_dataset(path, engine=NETCDF_ENGINE, decode_coords="all")
    except (ValueError, OSError) as error:
        raise RasterContractError(f"{path.name!r} could not be read as NetCDF: {error}") from error


def _drop_curvilinear_coordinates(
    dataset: xarray.Dataset | xarray.DataArray,
    *,
    x_dimension: str,
    y_dimension: str,
) -> xarray.Dataset | xarray.DataArray:
    """Drop two dimensional longitude and latitude helper coordinates, which are not the spatial axes."""
    spellings = set(X_AXIS_NAMES) | set(Y_AXIS_NAMES)
    curvilinear = [
        str(name)
        for name in dataset.coords
        if str(name) in spellings and str(name) not in (x_dimension, y_dimension) and dataset[name].ndim > 1
    ]
    if not curvilinear:
        return dataset
    return dataset.drop_vars(curvilinear)


def _rename_axes(
    dataset: xarray.Dataset | xarray.DataArray,
    *,
    time_dimension: str,
    y_dimension: str,
    x_dimension: str,
) -> xarray.Dataset | xarray.DataArray:
    """Rename the source spellings of the x, y and time axes onto the contract names."""
    present = {str(name) for name in dataset.dims} | {str(name) for name in dataset.coords}
    renames: dict[str, str] = {}
    for target, spellings in (
        (x_dimension, X_AXIS_NAMES),
        (y_dimension, Y_AXIS_NAMES),
        (time_dimension, TIME_AXIS_NAMES),
    ):
        if target in present:
            continue
        for spelling in spellings:
            if spelling in present:
                renames[spelling] = target
                break
    if not renames:
        return dataset
    return dataset.rename(renames)


def _clip_to_bbox(
    dataset: xarray.Dataset | xarray.DataArray,
    bbox: BoundingBox | None,
) -> xarray.Dataset | xarray.DataArray:
    """Clip an opened raster to a bounding box given in WGS84, reprojecting the box onto the source grid."""
    if bbox is None:
        return dataset
    try:
        clipped = dataset.rio.clip_box(
            minx=bbox.minimum_x,
            miny=bbox.minimum_y,
            maxx=bbox.maximum_x,
            maxy=bbox.maximum_y,
            crs=DEFAULT_CRS,
        )
    except Exception as error:  # noqa: BLE001 - rioxarray raises several unrelated types for one failure
        raise RasterContractError(f"clipping to {list(bbox.as_tuple())} failed: {error}") from error
    return cast(xarray.Dataset | xarray.DataArray, clipped)


def _squeeze_band(dataset: xarray.Dataset | xarray.DataArray) -> xarray.Dataset | xarray.DataArray:
    """Drop a band axis of one band, which is how a single band GeoTIFF arrives."""
    if BAND_DIMENSION not in dataset.dims:
        return dataset
    if int(dataset.sizes[BAND_DIMENSION]) != 1:
        raise RasterContractError(
            f"raster holds {int(dataset.sizes[BAND_DIMENSION])} bands, and ingest writes one variable",
        )
    return dataset.squeeze(BAND_DIMENSION, drop=True)


def _select_variable(dataset: xarray.Dataset | xarray.DataArray, variable: str) -> xarray.Dataset:
    """Return a dataset holding exactly the requested variable, renaming the only one a source carries."""
    if isinstance(dataset, xarray.DataArray):
        return dataset.rename(variable).to_dataset()
    if variable in dataset.data_vars:
        return dataset[[variable]]
    names = [str(name) for name in dataset.data_vars]
    if len(names) != 1:
        raise RasterContractError(f"variable {variable!r} is not one of {sorted(names)}")
    return dataset.rename({names[0]: variable})[[variable]]


def _assert_cube_size(
    dataset: xarray.Dataset,
    *,
    source_label: str,
    max_cube_cells: int | None,
    time_dimension: str,
    y_dimension: str,
    x_dimension: str,
) -> None:
    """Refuse a raster larger than the configured guard, counting its cells the way the engine does."""
    if max_cube_cells is None:
        return
    rows = int(dataset.sizes.get(y_dimension, 0))
    columns = int(dataset.sizes.get(x_dimension, 0))
    cell_count = rows * columns * max(int(dataset.sizes.get(time_dimension, 1)), 1)
    if cell_count > max_cube_cells:
        raise QuerySizeGuardError(
            f"cube of {source_label!r} holds {cell_count} cells, more than the {max_cube_cells} allowed",
        )


def _resolve_crs(dataset: xarray.Dataset, *, y_dimension: str, x_dimension: str) -> str:
    """Resolve the projection of an opened raster from the data itself, defaulting to WGS84."""
    declared = dataset.attrs.get(PROJECTION_CODE_ATTRIBUTE)
    if isinstance(declared, str) and declared:
        return declared
    written = dataset.rio.crs
    if written is not None:
        return str(written.to_wkt())
    if SPATIAL_REFERENCE_NAME in dataset.coords:
        well_known_text = dataset[SPATIAL_REFERENCE_NAME].attrs.get("crs_wkt")
        if isinstance(well_known_text, str) and well_known_text:
            return well_known_text
    # A source that declares nothing is taken as WGS84, which is what the coordinates of every
    # undeclared raster seen here have been. Anything else has to be declared by the file.
    _assert_degrees(dataset, y_dimension=y_dimension, x_dimension=x_dimension)
    return DEFAULT_CRS


def _assert_degrees(dataset: xarray.Dataset, *, y_dimension: str, x_dimension: str) -> None:
    """Refuse a raster with no projection whose coordinates cannot be degrees."""
    y_values = _axis_values(dataset, y_dimension)
    x_values = _axis_values(dataset, x_dimension)
    if float(numpy.abs(y_values).max()) <= 90.0 and float(x_values.min()) >= -180.0 and float(x_values.max()) <= 360.0:
        return
    raise RasterContractError(
        "raster declares no coordinate reference system and its coordinates are not degrees, "
        "so the frame it is on cannot be guessed",
    )


def _wrap_longitudes(dataset: xarray.Dataset, *, x_dimension: str, geographic: bool) -> xarray.Dataset:
    """Roll a geographic x axis from the 0 to 360 frame onto minus 180 to 180 and sort it ascending."""
    if not geographic or x_dimension not in dataset.coords:
        return dataset
    values = _axis_values(dataset, x_dimension)
    if not bool((values > LONGITUDE_ROLL_THRESHOLD).any()):
        return dataset
    rolled = dataset.assign_coords({x_dimension: ((values + 180.0) % 360.0) - 180.0})
    return rolled.sortby(x_dimension)


def _order_y_descending(dataset: xarray.Dataset, *, y_dimension: str) -> xarray.Dataset:
    """Reverse a south-up raster so row zero is the northernmost row, as every consumer assumes."""
    if y_dimension not in dataset.coords or int(dataset.sizes.get(y_dimension, 0)) < 2:
        return dataset
    values = _axis_values(dataset, y_dimension)
    if bool(values[1] < values[0]):
        return dataset
    return dataset.isel({y_dimension: slice(None, None, -1)})


def _mask_nodata(dataset: xarray.Dataset, *, variable: str, nodata_value: float | None) -> xarray.Dataset:
    """Mask the fill value of a variable to NaN and record the value it stood for as a finite attribute."""
    array = dataset[variable]
    resolved = nodata_value if nodata_value is not None else _declared_nodata(array)
    if resolved is None:
        return dataset
    # Masking needs a float array to hold NaN, and the sentinel is kept as an attribute so a reader can
    # still tell which cells were absent rather than merely unrepresentable.
    floating = array if numpy.issubdtype(numpy.dtype(array.dtype), numpy.floating) else array.astype("float32")
    masked = floating.where(floating != resolved)
    masked.attrs = dict(array.attrs)
    masked.attrs[NODATA_ATTRIBUTE] = float(resolved)
    return dataset.assign({variable: masked})


def _declared_nodata(array: xarray.DataArray) -> float | None:
    """Return the fill value a variable declares, in the decoded units the ingested values carry."""
    # A decoded source leaves its sentinel behind in raw units under `encoded_nodata`, so it is read
    # first and put through the same scaling as the values; `nodata` on such an array is only NaN.
    encoded = _finite_number(array.rio.encoded_nodata)
    if encoded is not None:
        return _decode_number(encoded, array.encoding)
    number = _finite_number(array.rio.nodata)
    if number is not None:
        return number
    for key in ("_FillValue", "missing_value", NODATA_ATTRIBUTE):
        number = _finite_number(array.attrs.get(key))
        if number is not None:
            return number
    return None


def _decode_number(raw: float, encoding: Mapping[Any, Any]) -> float:
    """Apply the scale factor and offset a source was encoded with to one raw number."""
    scale = _finite_number(encoding.get("scale_factor"))
    offset = _finite_number(encoding.get("add_offset"))
    return raw * (1.0 if scale is None else scale) + (0.0 if offset is None else offset)


def _stamp_timestamp(dataset: xarray.Dataset, timestamp: datetime | None, *, time_dimension: str) -> xarray.Dataset:
    """Give a raster without a time axis the single timestep the request names."""
    if time_dimension in dataset.dims:
        return dataset
    if timestamp is None:
        raise RasterContractError(
            f"raster has no {time_dimension!r} axis, so the request must name the timestamp it stands for",
        )
    stamped = dataset.drop_vars(time_dimension) if time_dimension in dataset.coords else dataset
    return stamped.expand_dims({time_dimension: [to_datetime64(timestamp)]})


def _order_dimensions(
    dataset: xarray.Dataset,
    *,
    variable: str,
    time_dimension: str,
    y_dimension: str,
    x_dimension: str,
) -> xarray.Dataset:
    """Transpose the variable onto the time, y and x order the cube contract fixes."""
    expected = (time_dimension, y_dimension, x_dimension)
    present = tuple(str(name) for name in dataset[variable].dims)
    if present == expected:
        return dataset
    if sorted(present) != sorted(expected):
        raise RasterContractError(f"variable {variable!r} has dimensions {present}, which are not {expected}")
    return dataset.transpose(*expected)


def _drop_encoding_and_unusable_attributes(dataset: xarray.Dataset) -> xarray.Dataset:
    """Drop the CF encoding and every attribute Icechunk would refuse, on the dataset and every variable."""
    dataset.attrs = _clean_attributes(dataset.attrs)
    dataset.encoding = {}
    for name in dataset.variables:
        dataset[name].attrs = _clean_attributes(dataset[name].attrs)
        # xarray would otherwise re-apply the source scaling, chunking and fill value when writing.
        dataset[name].encoding = {}
    return dataset


def _clean_attributes(attributes: Mapping[Any, Any]) -> dict[str, Any]:
    """Keep only the attributes that survive a JSON round trip with finite numbers in them."""
    cleaned: dict[str, Any] = {}
    for key, value in attributes.items():
        name = str(key)
        if name in CF_ENCODING_ATTRIBUTES and name != NODATA_ATTRIBUTE:
            continue
        converted = _clean_value(value)
        if converted is not None:
            cleaned[name] = converted
    return cleaned


def _clean_value(value: Any) -> Any:
    """Convert one attribute value onto a JSON type, or return None when it cannot be carried."""
    if isinstance(value, bool | str):
        return value
    if isinstance(value, numpy.bool_):
        return bool(value)
    if isinstance(value, int | float | numpy.integer | numpy.floating):
        number = float(value)
        if not math.isfinite(number):
            return None
        return int(value) if isinstance(value, int | numpy.integer) else number
    if isinstance(value, list | tuple | numpy.ndarray):
        items = [_clean_value(item) for item in value]
        if any(item is None for item in items):
            return None
        return items
    return None


def _axis_values(dataset: xarray.Dataset, dimension: str) -> FloatArray:
    """Return one spatial coordinate of a dataset as a one dimensional float array."""
    if dimension not in dataset.coords:
        raise RasterContractError(f"raster has no {dimension!r} coordinate to read")
    return numpy.asarray(dataset[dimension].values, dtype="float64").ravel()


def _cell_size(values: FloatArray, *, axis: str) -> float:
    """Return the regular cell size of one axis, refusing an axis that is too short or not regular."""
    if values.size < 2:
        raise RasterContractError(
            f"axis {axis!r} holds {values.size} cells, so its cell size cannot be measured from the file",
        )
    steps = numpy.abs(numpy.diff(values))
    size = float(numpy.median(steps))
    if not math.isfinite(size) or size <= 0.0:
        raise RasterContractError(f"axis {axis!r} has no usable cell size")
    if float(numpy.max(numpy.abs(steps - size))) > max(CELL_SIZE_TOLERANCE, size * CELL_SIZE_TOLERANCE):
        raise RasterContractError(f"axis {axis!r} is not a regular grid, which a coverage has to be")
    return size


def _finite_number(value: Any) -> float | None:
    """Return a value as a finite float, or None when it is absent or not a finite number."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, int | float | numpy.integer | numpy.floating):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
