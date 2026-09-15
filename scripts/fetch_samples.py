"""Downloads the two network-dependent sample sets: CHIRPS daily rainfall and geoBoundaries admin areas."""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Final

import rioxarray

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DOWNLOAD_DIRECTORY: Final[Path] = REPOSITORY_ROOT / "samples" / "downloaded"
CHIRPS_DIRECTORY: Final[Path] = DOWNLOAD_DIRECTORY / "chirps3"

# CHIRPS v3.0 final daily rainfall, published as global cloud optimised GeoTIFFs. Each global file is
# roughly 30 MB, but a clip_box over an open COG issues HTTP range requests, so only the Sierra Leone
# window travels: the files written here are a few kilobytes each.
CHIRPS_URL_TEMPLATE: Final[str] = (
    "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/rnl/cogs/"
    "{year}/chirps-v3.0.rnl.{year}.{month:02d}.{day:02d}.cog"
)
CHIRPS_FIRST_DAY: Final[date] = date(2024, 1, 1)
CHIRPS_DAY_COUNT: Final[int] = 14
CHIRPS_NODATA: Final[float] = -9999.0

# Minimum x, minimum y, maximum x, maximum y of Sierra Leone in EPSG:4326.
SIERRA_LEONE_BBOX: Final[tuple[float, float, float, float]] = (-13.5, 6.9, -10.1, 10.0)

# Pinned to a commit rather than to a branch so the file fetched today is the file fetched tomorrow.
GEOBOUNDARIES_URL: Final[str] = (
    "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/SLE/ADM2/geoBoundaries-SLE-ADM2.geojson"
)
GEOBOUNDARIES_NAME: Final[str] = "geoboundaries-sle-adm2.geojson"
DOWNLOAD_TIMEOUT_SECONDS: Final[float] = 120.0


def chirps_days() -> list[date]:
    """Return the days of CHIRPS rainfall this script fetches."""
    return [CHIRPS_FIRST_DAY + timedelta(days=offset) for offset in range(CHIRPS_DAY_COUNT)]


def chirps_url(day: date) -> str:
    """Return the CHIRPS cloud optimised GeoTIFF URL of one day."""
    return CHIRPS_URL_TEMPLATE.format(year=day.year, month=day.month, day=day.day)


def chirps_path(day: date, *, directory: Path = CHIRPS_DIRECTORY) -> Path:
    """Return the local path one day of clipped CHIRPS rainfall is written to."""
    return directory / f"chirps3-{day.isoformat()}.tif"


def fetch_chirps_day(day: date, *, directory: Path = CHIRPS_DIRECTORY) -> tuple[Path, bool]:
    """Clip one day of global CHIRPS rainfall to Sierra Leone, returning its path and whether it was written."""
    destination = chirps_path(day, directory=directory)
    if destination.exists():
        return destination, False
    destination.parent.mkdir(parents=True, exist_ok=True)
    minimum_x, minimum_y, maximum_x, maximum_y = SIERRA_LEONE_BBOX
    array = rioxarray.open_rasterio(chirps_url(day))
    if isinstance(array, list):
        raise OSError(f"the CHIRPS file for {day.isoformat()} holds several subdatasets, which this script cannot clip")
    try:
        window = array.rio.clip_box(minx=minimum_x, miny=minimum_y, maxx=maximum_x, maxy=maximum_y)
        window.rio.write_nodata(CHIRPS_NODATA, inplace=True)
        # Write beside the destination and rename, so an interrupted run never leaves a partial file
        # that the existence check above would then skip.
        partial = destination.with_suffix(".tif.partial")
        window.rio.to_raster(partial, driver="GTiff", compress="deflate")
        partial.replace(destination)
    finally:
        array.close()
    return destination, True


def fetch_geoboundaries(*, directory: Path = DOWNLOAD_DIRECTORY) -> tuple[Path, bool]:
    """Download the geoBoundaries Sierra Leone ADM2 areas, returning the path and whether it was written."""
    destination = directory / GEOBOUNDARIES_NAME
    if destination.exists():
        return destination, False
    directory.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".geojson.partial")
    with urllib.request.urlopen(GEOBOUNDARIES_URL, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        partial.write_bytes(response.read())
    partial.replace(destination)
    return destination, True


def describe(path: Path, *, written: bool) -> str:
    """Return a one-line report of a fetched file, its size and whether this run wrote it."""
    relative = path.relative_to(REPOSITORY_ROOT) if path.is_relative_to(REPOSITORY_ROOT) else path
    return f"[{'fetched' if written else 'present'}] {relative} ({path.stat().st_size:,} bytes)"


def main() -> int:
    """Fetch every downloadable sample that is not already on disk and report what is there."""
    print(f">>> Fetching samples into {DOWNLOAD_DIRECTORY.relative_to(REPOSITORY_ROOT)}")
    try:
        for day in chirps_days():
            path, written = fetch_chirps_day(day)
            print(describe(path, written=written))
        path, written = fetch_geoboundaries()
        print(describe(path, written=written))
    except (OSError, urllib.error.URLError) as error:
        print(f"ERROR: fetching the samples needs network access: {error}", file=sys.stderr)
        return 1
    print(">>> Samples ready; `make demo` now runs entirely offline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
