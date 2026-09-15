"""Helpers for the ingest tests: the committed sample files and small GeoTIFFs written to tmp_path."""

from __future__ import annotations

from pathlib import Path

import numpy
import rioxarray
import xarray

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


def ramp(rows: int, columns: int) -> numpy.typing.NDArray[numpy.float32]:
    """Return a small deterministic float32 block of the requested shape."""
    return numpy.arange(rows * columns, dtype="float32").reshape(rows, columns)
