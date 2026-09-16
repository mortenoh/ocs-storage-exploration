"""Helpers for the ingest tests: the committed sample files and small rasters written to tmp_path."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import numpy
import rioxarray
import xarray

from ocs_storage_exploration.storage.raster.ingest import NETCDF_ENGINE

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIRECTORY = REPOSITORY_ROOT / "samples"
DOWNLOADED_DIRECTORY = SAMPLES_DIRECTORY / "downloaded"
CHIRPS_DIRECTORY = DOWNLOADED_DIRECTORY / "chirps3"
WORLDPOP_FILE = SAMPLES_DIRECTORY / "sle_pop_2026_CN_1km_R2025A_UA_v1.tif"
DISTRICTS_FILE = SAMPLES_DIRECTORY / "sierra_leone_districts.geojson"
LAKES_FILE = SAMPLES_DIRECTORY / "ne_110m_lakes.geojson"


def write_geotiff(
    path: Path,
    *,
    values: numpy.typing.NDArray[numpy.float32],
    y_values: numpy.typing.NDArray[numpy.float64],
    x_values: numpy.typing.NDArray[numpy.float64],
    crs: str = "EPSG:4326",
    nodata_value: float | None = None,
) -> Path:
    """Write one single band GeoTIFF with the given coordinates, for the normalisation tests."""
    # rioxarray has to be reachable for the .rio accessor below; naming it keeps the import honest.
    assert rioxarray.__version__
    array = xarray.DataArray(
        values[None, :, :],
        dims=("band", "y", "x"),
        coords={"band": [1], "y": y_values, "x": x_values},
    )
    array.rio.write_crs(crs, inplace=True)
    if nodata_value is not None:
        array.rio.write_nodata(nodata_value, inplace=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    array.rio.to_raster(path)
    return path


def write_scaled_geotiff(
    path: Path,
    *,
    values: numpy.typing.NDArray[numpy.int16],
    y_values: numpy.typing.NDArray[numpy.float64],
    x_values: numpy.typing.NDArray[numpy.float64],
    scale_factor: float,
    add_offset: float = 0.0,
    crs: str = "EPSG:4326",
    nodata_value: int | None = None,
) -> Path:
    """Write one integer GeoTIFF carrying the rasterio scales and offsets a reader has to apply."""
    assert rioxarray.__version__
    array = xarray.DataArray(
        values[None, :, :],
        dims=("band", "y", "x"),
        coords={"band": [1], "y": y_values, "x": x_values},
    )
    array.rio.write_crs(crs, inplace=True)
    if nodata_value is not None:
        array.rio.write_nodata(nodata_value, inplace=True)
    # rioxarray reads the band scales and offsets from these tags and writes the values unchanged,
    # which is exactly how a real scaled source is stored: raw integers plus the transform to apply.
    array.attrs["scales"] = (scale_factor,)
    array.attrs["offsets"] = (add_offset,)
    path.parent.mkdir(parents=True, exist_ok=True)
    array.rio.to_raster(path)
    return path


def build_cube(
    *,
    values: numpy.typing.NDArray[numpy.float32],
    y_values: numpy.typing.NDArray[numpy.float64],
    x_values: numpy.typing.NDArray[numpy.float64],
    timestamps: Sequence[datetime],
    variable: str = "rain",
    crs: str = "EPSG:4326",
) -> xarray.Dataset:
    """Build one small t, y and x cube with a written projection, for the NetCDF and Zarr helpers."""
    array = xarray.DataArray(
        values,
        dims=("t", "y", "x"),
        coords={
            "t": numpy.array([numpy.datetime64(moment, "ns") for moment in timestamps]),
            "y": y_values,
            "x": x_values,
        },
        name=variable,
    )
    dataset = array.to_dataset()
    dataset.rio.write_crs(crs, inplace=True)
    return dataset


def write_zarr_store(
    path: Path,
    *,
    values: numpy.typing.NDArray[numpy.float32],
    y_values: numpy.typing.NDArray[numpy.float64],
    x_values: numpy.typing.NDArray[numpy.float64],
    timestamps: Sequence[datetime],
    variable: str = "rain",
    crs: str = "EPSG:4326",
) -> Path:
    """Write one Zarr directory store holding a t, y and x cube, for the directory store tests."""
    cube = build_cube(
        values=values,
        y_values=y_values,
        x_values=x_values,
        timestamps=timestamps,
        variable=variable,
        crs=crs,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    cube.to_zarr(path, mode="w", consolidated=False)
    return path


def write_netcdf(
    path: Path,
    *,
    values: numpy.typing.NDArray[numpy.float32],
    y_values: numpy.typing.NDArray[numpy.float64],
    x_values: numpy.typing.NDArray[numpy.float64],
    timestamps: Sequence[datetime],
    variable: str = "rain",
    crs: str = "EPSG:4326",
) -> Path:
    """Write one NetCDF file holding a t, y and x cube, for the NetCDF ingest tests."""
    cube = build_cube(
        values=values,
        y_values=y_values,
        x_values=x_values,
        timestamps=timestamps,
        variable=variable,
        crs=crs,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    cube.to_netcdf(path, engine=NETCDF_ENGINE)
    return path


def ramp(rows: int, columns: int) -> numpy.typing.NDArray[numpy.float32]:
    """Return a small deterministic float32 block of the requested shape."""
    return numpy.arange(rows * columns, dtype="float32").reshape(rows, columns)
