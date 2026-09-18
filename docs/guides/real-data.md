# Real data

The rest of this repository is exercised by synthetic cubes and hand-written
feature collections, which prove the model but not that it survives a file
somebody else produced. This page is the other half: five real datasets, two
ingest endpoints and what to look at once they are stored.

If you only want the commands, [the offline quickstart](offline-quickstart.md)
is the copy-paste version. This page explains what the files are and how the
ingest treats them.

## The samples

Three files are committed under `samples/`, and two more are downloaded by
`make samples` into `samples/downloaded/`, which is gitignored.

| Dataset | File | What it is | Licence |
| --- | --- | --- | --- |
| `chirps3-sle-daily` | `downloaded/chirps3/chirps3-2024-01-*.tif` | 14 days of CHIRPS v3.0 final daily rainfall clipped to Sierra Leone, 63 by 69 cells in EPSG:4326, nodata -9999 | Public domain, CC0-1.0 in catalogues. Cite the Climate Hazards Center, UC Santa Barbara |
| `worldpop-sle-2026` | `sle_pop_2026_CN_1km_R2025A_UA_v1.tif` | WorldPop constrained population for 2026, 370 by 364 cells at 1 km in EPSG:4326, nodata -99999 | CC BY 4.0, WorldPop, University of Southampton |
| `sle-districts` | `sierra_leone_districts.geojson` | 13 DHIS2 organisation units with `id`, `name`, `level` and `parentName` | BSD-3-Clause, from the Open Climate Service test data |
| `sle-adm2-geoboundaries` | `downloaded/geoboundaries-sle-adm2.geojson` | 14 Sierra Leone ADM2 areas with `shapeID`, `shapeName`, `shapeGroup` and `shapeType` | CC BY 4.0, geoBoundaries |
| `ne-lakes` | `ne_110m_lakes.geojson` | 25 lakes at 1:110m with `id`, `name` and `featureclass` | Public domain, Natural Earth |

`samples/README.md` has the same table with the exact provenance of each file.

### Why the CHIRPS download is small

One global CHIRPS day is about 30 MB. `scripts/fetch_samples.py` opens the file
over HTTPS as a cloud optimised GeoTIFF and calls `rio.clip_box` on it, so GDAL
issues HTTP range requests for the tiles covering Sierra Leone and nothing else
travels. Each file written is a few kilobytes, and the whole fetch is under a
megabyte. The script is idempotent: a day already on disk is skipped, so
`make samples` is safe to run again.

## `make demo`

```bash
make demo
```

`ocs_storage_exploration/demo.py` ingests all five in process, against whatever
backend `OCS_STORAGE_BACKEND` names, and needs no running server. It publishes
four of them and leaves `ne-lakes` a draft, which is why `GET /stac/collections`
lists four: STAC advertises only what is published.

A dataset the storage layer refuses makes the run exit non-zero, and the summary
line says which one and why. A sample that was never downloaded is not a
refusal: `make samples` is optional, so the two datasets under
`samples/downloaded/` are reported as skipped and the three committed ones are
ingested anyway.

## The seeded Docker stacks

The same code is what fills the two compose profiles, as a one-shot `seed`
container the API waits for:

```bash
make docker-run-file    # seeds ./data, API on http://127.0.0.1:8000
make docker-run-s3      # seeds the bucket, API on http://127.0.0.1:8001
```

Both run in the foreground and Ctrl-C stops and removes everything. The seed is
idempotent in the set of datasets rather than in the number of versions: a
second run overwrites each coverage under the same identifier and writes the
next version of each collection, publishing it.
[The offline quickstart](offline-quickstart.md#both-stacks-in-one-command) has
the details and the rustfs console URL.

## What to look at afterwards

Start the service with `make run`, or `make docker-run-file` for the seeded
stack on the same port, then:

```bash
# 14 days of rainfall summarised over a box inside the country
curl -s 'http://127.0.0.1:8000/api/v1/raster/chirps3-sle-daily/query?bbox=-13.3,7.9,-12.0,9.0'

# the first week only
curl -s 'http://127.0.0.1:8000/api/v1/raster/chirps3-sle-daily/query?start=2024-01-01T00:00:00&end=2024-01-07T00:00:00'

# every snapshot the ingest left behind: one create and thirteen appends
curl -s http://127.0.0.1:8000/api/v1/raster/chirps3-sle-daily/versions

# districts by envelope, and by attribute with two columns
curl -s 'http://127.0.0.1:8000/api/v1/vector/sle-districts/features?bbox=-13.3,7.9,-12.0,9.0'
curl -s 'http://127.0.0.1:8000/api/v1/vector/sle-districts/features?where=level:2&columns=name,level'

# the same records as a STAC catalog
curl -s http://127.0.0.1:8000/stac/collections
curl -s http://127.0.0.1:8000/stac/collections/chirps3-sle-daily
```

The CHIRPS collection carries the datacube extension with the real extents the
files had, and `worldpop-sle-2026` has a temporal extent of exactly one instant
because a static raster still gets a `t` axis, from the request rather than
from the file.

On disk, `data/ocs/raster/chirps3-sle-daily/` is one Icechunk repository with
fourteen commits on `main` and a `published` branch pointing at the last of
them, and `data/ocs/vector/sle-districts/versions/v00001/data.parquet` is an
ordinary GeoParquet file. [Inspecting the data](inspecting-the-data.md) opens
both with Icechunk, xarray, geopandas, pyarrow and DuckDB; every path in it is
real once `make demo` has run.

## What normalisation does to a file

`storage/raster/ingest.py` mirrors the rules the Open Climate Service applies to
a fetched period, so a file that works there works here.

Before any of the rules below, the file is read through a decoding reader:
GeoTIFF and COG are opened with `mask_and_scale=True`, and NetCDF and Zarr
decode CF attributes by default. A source that stores raw integers next to a
scale factor and an offset is therefore read in the units it declares, so a cell
holding 100 in a file with a scale of 0.1 and an offset of 5 arrives as 15.0.
That is also the one change of dtype ingest makes: a scaled integer source
becomes a floating point array, because the decoded values do not fit the
integer type the file used.

Then, in order, because the order matters:

1. Two dimensional `lon` and `lat` helper coordinates are dropped: they are not
   the spatial axes and they confuse both the rename below and rioxarray.
2. `x`, `X`, `lon` and `longitude` become `x`; `y`, `Y`, `lat` and `latitude`
   become `y`; `time`, `valid_time`, `date` and `time_counter` become `t`.
3. The raster is clipped, if the request named a bounding box.
4. A `band` axis of one band is squeezed away; more than one band is refused,
   because ingest writes one variable.
5. The projection is resolved from the data, from `proj:code` or the CF grid
   mapping, defaulting to EPSG:4326 only when the coordinates can be degrees.
   This has to happen before the two steps below: whether the x axis is a
   longitude decides whether rolling it is correct or destructive, and an
   easting of 500000 rolled as a longitude becomes -40.
6. A geographic x axis beyond 180 is rolled onto -180 to 180 and sorted.
7. `y` is reversed if it ascends, together with its rows, so row 0 is the
   northernmost row. Consumers assume this and never check.
8. The nodata sentinel is masked to NaN and kept as a finite `nodata` attribute,
   so a reader can still tell an absent cell from an unrepresentable one. The
   sentinel is recorded in the same units as the values around it: a file that
   declares -1 with a scale of 0.1 and an offset of 5 records 4.9, not -1, so
   the attribute and the array never disagree about what an absent cell meant.
9. A raster with no time axis gets the single timestep the request names.
10. The variable is transposed onto `(t, y, x)`, the CF encoding is dropped, and
    any attribute that is not a finite JSON value is dropped, because Icechunk
    refuses them.

The `GridSpecification` is then derived from the normalised data itself: its
shape, its cell centres grown by half a cell into a bounding box, its dtype, its
nodata value and its projection. Nothing about the grid is taken from the
request, so an ingest cannot declare a grid the file does not have. An axis that
is not regular, or that holds a single cell, is refused rather than guessed at.

## Formats and the readers behind them

| Source | Suffixes | Reader |
| --- | --- | --- |
| GeoTIFF and COG | `.tif`, `.tiff`, `.cog`, `.gtiff` | rioxarray on rasterio, with `mask_and_scale=True` |
| NetCDF | `.nc`, `.nc4`, `.cdf`, `.netcdf` | xarray on `h5netcdf`, pinned by name |
| Zarr | `.zarr` | xarray on zarr |

Two of those need a word.

**NetCDF needs an engine.** xarray ships no NetCDF reader of its own, so this
project depends on `h5netcdf[h5py]` and names it explicitly rather than letting
xarray guess: a guess in a deployment that has none raises a bare `ValueError`
that reaches the client as a 500. A deployment built without the engine answers
501 and says which engine is missing, and a file that is malformed rather than
unreadable answers 422.

**A Zarr store is a directory, not a file.** `samples/cube.zarr` is a directory
of chunks and metadata, which the path resolver accepts because `.zarr` is a
directory store rather than because directories are allowed; any other directory
is still refused. A literal path and a glob both work, so
`{"files": ["samples/*.zarr"]}` resolves the stores under `samples/`.

Note: a scaled integer source ingested before the decoding reader landed was
stored in raw counts, with a sentinel to match. Re-ingest it with
`"overwrite": true` to replace it; there is no in-place fix-up, because the raw
values and the decoded ones are different data.

## Bring your own data

Two endpoints take a local path. Both refuse a path outside the configured
ingest roots, which default to `samples/` and the data directory and are set
with `OCS_STORAGE_INGEST_ROOTS`.

### `POST /api/v1/raster/{id}/ingest`

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/raster/my-rainfall/ingest \
  -H 'content-type: application/json' \
  -d '{
    "files": ["samples/downloaded/chirps3/chirps3-*.tif"],
    "variable": "precipitation",
    "timestamps": "from-filename",
    "filename_date_pattern": "(\\d{4}-\\d{2}-\\d{2})",
    "title": "CHIRPS v3.0 daily rainfall",
    "license": "CC0-1.0",
    "attribution": "Climate Hazards Center, UC Santa Barbara",
    "publish": true
  }'
```

| Field | Meaning |
| --- | --- |
| `files` | Paths or globs, resolved against the working directory, expanded and refused outside the ingest roots. GeoTIFF, COG, NetCDF and Zarr; a `.zarr` store is a directory and resolves as one |
| `variable` | The name the variable is written under, whatever the file called it |
| `timestamps` | `"from-filename"` (the default), or an explicit list of ISO timestamps, one per resolved file |
| `timestamp` | One ISO timestamp instead, for a single static file such as a population grid |
| `filename_date_pattern` | The regular expression read off each file name, `(\d{4}-\d{2}-\d{2})` by default. Several capture groups are joined with `-`, so `(\d{4})\.(\d{2})\.(\d{2})` reads `chirps.2024.01.07.cog` |
| `bbox` | Optional clip, in EPSG:4326, reprojected onto the source grid |
| `title`, `license`, `attribution` | Recorded on the catalog record and carried into STAC |
| `overwrite` | Replace an existing coverage rather than refusing with 409 |
| `publish` | Move the published branch onto the snapshot the ingest ends at |

The files are sorted by timestamp, the first one creates the coverage and the
rest are appended in order, through the same `create` and `append` the synthetic
endpoints use, so the cube size guard, the coordinate check and the variable
check all apply. Each source file is measured against the guard on its own as
well, after the bounding box has clipped it and before a cell is read, so a file
whose cube would hold more than `OCS_STORAGE_MAX_CUBE_CELLS` cells is refused
with 413 naming that file rather than after it is in memory. The response is the
usual `RasterWriteResult` plus the files read and the timestamps they carried:

```json
{
  "dataset_identifier": "my-rainfall",
  "snapshot_identifier": "43RVK929J5Z5SJVF4F6G",
  "timestep_count": 14,
  "variables": ["precipitation"],
  "published": true,
  "files": ["samples/downloaded/chirps3/chirps3-2024-01-01.tif", "..."],
  "timestamps": ["2024-01-01T00:00:00", "..."]
}
```

A static raster is the same call with `timestamp` instead:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/raster/my-population/ingest \
  -H 'content-type: application/json' \
  -d '{
    "files": ["samples/sle_pop_2026_CN_1km_R2025A_UA_v1.tif"],
    "variable": "population",
    "timestamp": "2026-01-01T00:00:00Z",
    "license": "CC-BY-4.0",
    "attribution": "WorldPop, University of Southampton",
    "publish": true
  }'
```

### `POST /api/v1/vector/{id}/ingest`

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/vector/my-districts/ingest \
  -H 'content-type: application/json' \
  -d '{
    "path": "samples/sierra_leone_districts.geojson",
    "identifier_property": "id",
    "selectable_columns": ["level", "name", "parentName"],
    "title": "Sierra Leone districts",
    "license": "BSD-3-Clause",
    "attribution": "DHIS2 demo database",
    "publish": true
  }'
```

| Field | Meaning |
| --- | --- |
| `path` | One file, GeoJSON, GeoPackage, FlatGeobuf, shapefile or GeoParquet, under an ingest root |
| `identifier_property` | The column that identifies a feature. It must exist, be non-null and be unique, or the write is refused with 422 |
| `selectable_columns` | The columns a `where` clause may filter on |
| `crs` | The frame to assume for a file that declares none, EPSG:4326 by default |
| `title`, `license`, `attribution`, `publish` | As for raster |

Which property to use as the identifier depends on the file:

| File | `identifier_property` | Useful `selectable_columns` |
| --- | --- | --- |
| `sierra_leone_districts.geojson` | `id`, the DHIS2 organisation unit UID such as `O6uvpzGd5pu` | `level`, `name`, `parentName` |
| `geoboundaries-sle-adm2.geojson` | `shapeID`, for example `65957751B2614126839094` | `shapeName`, `shapeGroup`, `shapeType` |
| `ne_110m_lakes.geojson` | `id`, an integer from Natural Earth | `name`, `featureclass` |

One real-file detail the reader handles: a column that is an empty object or an
empty array on every row is dropped before the write. The DHIS2 organisation
units carry `dimensions: {}` on every feature, and Parquet refuses a struct with
no child field. A column that is empty in every row carries nothing that could
be lost.

### Path safety

`Settings.ingest_roots` defaults to `[samples, data_directory]`. A path is
resolved first and then checked, so a `..` segment, a symbolic link out of a
root and an absolute path elsewhere are all refused the same way, with 400:

```console
$ curl -s -X POST http://127.0.0.1:8000/api/v1/vector/escape/ingest \
    -H 'content-type: application/json' \
    -d '{"path": "samples/../pyproject.toml", "identifier_property": "id"}'
{"error":"IngestPathError","detail":"ingest path 'samples/../pyproject.toml' resolves outside the ingest roots '/app/samples', '/app/data'"}
```

An absolute path inside a root is allowed, which is what the demo uses. Widen
the roots with `OCS_STORAGE_INGEST_ROOTS='["samples","/srv/climate-data"]'`.

## What this does not do yet

The ingest reads files that are already on the machine. Driving an OCS dataset
plugin's `fetch_period` straight into `append`, so a period is fetched and
stored in one call, is the step after this one; see [the roadmap](../roadmap.md).
There is also no pyramid: a coarse read of a large grid still pays for the full
resolution.
