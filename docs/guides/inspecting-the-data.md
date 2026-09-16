# Inspecting the data

Everything the service writes is an ordinary object under the data directory.
No format is proprietary to this repo, and no catalogue lives inside a
database: a dataset is a JSON record plus either an Icechunk repository or a
directory of GeoParquet versions. This page shows where those objects land and
how to open them without going through the HTTP API.

The examples below were produced by running the service against a scratch data
directory and then walking the [API walkthrough](api-walkthrough.md):

```bash
OCS_STORAGE_DATA_DIRECTORY=/tmp/ocs-demo uv run uvicorn ocs_storage_exploration.main:create_app \
  --factory --port 8765
```

## Key layout

Every key starts at `{data_directory}/{base_prefix}`, where `base_prefix`
defaults to `ocs`. The same three prefixes are used by every backend: on S3
they are keys under the bucket rather than directories on disk, as
[backends and key layout](../concepts/backends-and-layout.md) sets out.

```text
{data_directory}/
  ocs/
    catalog/datasets/{dataset_identifier}.json   one record per dataset, either item type
    raster/{dataset_identifier}/                 one Icechunk repository per coverage
    vector/{dataset_identifier}/current.json     pointer naming the published version
    vector/{dataset_identifier}/versions/v00001/reservation.json
    vector/{dataset_identifier}/versions/v00001/data.parquet
    vector/{dataset_identifier}/versions/v00001/metadata.json
    vector/{dataset_identifier}/versions/v00002/...
```

Each version directory holds three objects: the `reservation.json` that claimed
the version number, the Parquet file, and the `metadata.json` sidecar that both
completes the version and records the coordinate reference system, feature
count, identifier property and selectable columns a read of that version uses.

`find` over the directory the walkthrough wrote shows both halves:

```console
$ find /tmp/ocs-demo -maxdepth 5
/tmp/ocs-demo
/tmp/ocs-demo/ocs
/tmp/ocs-demo/ocs/catalog
/tmp/ocs-demo/ocs/raster
/tmp/ocs-demo/ocs/vector
/tmp/ocs-demo/ocs/catalog/datasets
/tmp/ocs-demo/ocs/raster/temperature-demo
/tmp/ocs-demo/ocs/vector/districts-demo
/tmp/ocs-demo/ocs/catalog/datasets/temperature-demo.json
/tmp/ocs-demo/ocs/catalog/datasets/districts-demo.json
/tmp/ocs-demo/ocs/raster/temperature-demo/snapshots
/tmp/ocs-demo/ocs/raster/temperature-demo/chunks
/tmp/ocs-demo/ocs/raster/temperature-demo/transactions
/tmp/ocs-demo/ocs/raster/temperature-demo/manifests
/tmp/ocs-demo/ocs/raster/temperature-demo/repo
/tmp/ocs-demo/ocs/raster/temperature-demo/overwritten
/tmp/ocs-demo/ocs/vector/districts-demo/versions
/tmp/ocs-demo/ocs/vector/districts-demo/current.json
/tmp/ocs-demo/ocs/vector/districts-demo/versions/v00001
/tmp/ocs-demo/ocs/vector/districts-demo/versions/v00002
```

(the Icechunk object names under `snapshots`, `chunks`, `manifests`,
`transactions` and `overwritten` are elided; a deeper `-maxdepth` lists them)

The Icechunk repository is a directory of immutable objects: `snapshots/`,
`manifests/`, `chunks/`, `transactions/` and `overwritten/` are named by
snapshot or manifest identifier, and the branch pointers live inside the single
`repo` file rather than as loose reference files. That is why publication never
renames a directory: it rewrites one pointer with a compare-and-swap.

## The catalog record

The record is what makes a dataset exist, so read it first. It is plain JSON
and it never contains a credential:

```console
$ jq '{item_type, title, publication, grid: .grid.shape, timestep_count}' \
    /tmp/ocs-demo/ocs/catalog/datasets/temperature-demo.json
{
  "item_type": "coverage",
  "title": "Synthetic temperature",
  "publication": {
    "published": true,
    "published_at": "2026-09-15T17:15:18.242242Z",
    "snapshot_identifier": "RK4P9ETMDPFEA8ZGVR30",
    "version": null,
    "previous_snapshot_identifier": "8FMG55R5397CKBMH99D0",
    "previous_version": null
  },
  "grid": [32, 64],
  "timestep_count": 9
}
```

`snapshot_identifier` names what readers see; `previous_snapshot_identifier` is
what a rollback moved away from, which [versioning](../concepts/versioning.md)
explains in full. The vector pointer object is the same idea in one file:

```console
$ cat /tmp/ocs-demo/ocs/vector/districts-demo/current.json
{"version":1,"key":"ocs/vector/districts-demo/versions/v00001/data.parquet","feature_count":3,
 "published_at":"2026-09-15T17:15:18.434733Z"}
```

## Opening a coverage with Icechunk and xarray

Point Icechunk at the repository directory, open a readonly session on a
branch, and read it with xarray. `decode_coords="all"` is mandatory: without it
`spatial_ref` stays a data variable and the coordinate reference system is
silently lost.

```python
import icechunk
import xarray

repository = icechunk.Repository.open(
    icechunk.local_filesystem_storage("/tmp/ocs-demo/ocs/raster/temperature-demo")
)
print(repository.list_branches())  # {'main', 'published'}

session = repository.readonly_session("published")
dataset = xarray.open_zarr(session.store, consolidated=False, zarr_format=3, decode_coords="all")
print(dataset)
print(dataset.spatial_ref.attrs["crs_wkt"][:43])
print(dataset.attrs["proj:code"], dataset.attrs["spatial:bbox"], dataset.attrs["time_step"])
```

```text
<xarray.Dataset> Size: 50kB
Dimensions:      (t: 6, y: 32, x: 64)
Coordinates:
  * t            (t) datetime64[ns] 48B 2020-01-01 2020-02-01 ... 2020-06-01
  * y            (y) float64 256B 87.19 81.56 75.94 ... -75.94 -81.56 -87.19
  * x            (x) float64 512B -177.2 -171.6 -165.9 ... 165.9 171.6 177.2
    spatial_ref  int32 4B ...
Data variables:
    temperature  (t, y, x) float32 49kB ...
Attributes:
    time_step:     month
    proj:code:     EPSG:4326
    spatial:bbox:  [-180.0, -90.0, 180.0, 90.0]
GEOGCRS["WGS 84",ENSEMBLE["World Geodetic S
EPSG:4326 [-180.0, -90.0, 180.0, 90.0] month
```

`main` is the draft branch every write commits to, and `published` is the
pointer readers follow. After the walkthrough rolls the coverage back, the two
branches disagree, which is exactly what a rollback is:

```python
draft = repository.readonly_session("main")
print(xarray.open_zarr(draft.store, consolidated=False, zarr_format=3, decode_coords="all").sizes)
# Frozen({'t': 9, 'y': 32, 'x': 64}) against t: 6 on the published branch
```

`ancestry` is the same history the `versions` endpoint reports, newest first:

```python
for information in repository.ancestry(branch="main"):
    print(information.id, information.message, information.written_at)
```

```text
8FMG55R5397CKBMH99D0 append 2026-09-15 17:15:18.166688+00:00
RK4P9ETMDPFEA8ZGVR30 initial write 2026-09-15 17:15:18.134216+00:00
1CECHNKREP0F1RSTCMT0 Repository initialized 2026-09-15 17:15:18.120458+00:00
```

Note: the `time_step` root attribute is written by this service, not by
GeoZarr. It records the calendar step the synthetic cube was generated with so
that an append can continue the time axis without being told the step again.

## Opening a collection with geopandas

Each version directory holds one GeoParquet file. `read_parquet` takes a
bounding box and uses the covering bbox column to prune row groups:

```python
import geopandas

frame = geopandas.read_parquet(
    "/tmp/ocs-demo/ocs/vector/districts-demo/versions/v00001/data.parquet",
    bbox=(5, 59, 11, 61),
)
print(frame[["id", "level"]])
print(frame.crs.to_string())
```

```text
       id  level
0  bergen      2
1    oslo      2
EPSG:4326
```

The coordinate reference system comes back from the `geo` metadata as PROJJSON,
so `print(frame.crs)` answers the whole document; `to_string()` or
`to_authority()` is what gives the short code back.

Pruning is by envelope only, so the service re-filters with an exact
`intersects` test after reading; see [GeoParquet](../research/geoparquet.md)
for why that matters for a concave geometry.

## Reading the Parquet metadata directly

The `geo` key in the file footer is where the GeoParquet contract lives. This
is the fastest way to confirm the schema version, the encoding and the covering
bounding box the writer declared:

```python
import json

import pyarrow.parquet

path = "/tmp/ocs-demo/ocs/vector/districts-demo/versions/v00001/data.parquet"
metadata = pyarrow.parquet.read_metadata(path)
geo = json.loads(metadata.metadata[b"geo"])

print(metadata.num_rows, metadata.num_row_groups)
print(geo["version"], geo["primary_column"])
print(geo["columns"]["geometry"]["encoding"], geo["columns"]["geometry"]["covering"])
print(pyarrow.parquet.read_schema(path).names)
```

```text
3 1
1.1.0 geometry
WKB {'bbox': {'xmin': ['bbox', 'xmin'], 'ymin': ['bbox', 'ymin'], 'xmax': ['bbox', 'xmax'], 'ymax': ['bbox', 'ymax']}}
['geometry', 'id', 'level', 'path', 'bbox']
```

`metadata.row_group(index).column(position).statistics` shows the per-row-group
minimum and maximum a bounding box query prunes on. With the default row group
size of 65536 a small fixture is a single row group, so pruning only becomes
visible on a collection large enough to fill several.

## Querying with DuckDB (optional)

DuckDB reads GeoParquet natively once the spatial extension is loaded, which
makes it a convenient check that the file is readable outside the Python stack:

```bash
uvx --with duckdb python - <<'PY'
import duckdb

duckdb.sql("INSTALL spatial; LOAD spatial;")
duckdb.sql("""
    SELECT id, level, ST_AsText(geometry) AS geometry
    FROM read_parquet('/tmp/ocs-demo/ocs/vector/districts-demo/versions/v00001/data.parquet')
    WHERE level = 2
""").show()
PY
```

```text
┌─────────┬───────┬───────────────────────────────────────────────────────────────┐
│   id    │ level │                           geometry                            │
│ varchar │ int64 │                            varchar                            │
├─────────┼───────┼───────────────────────────────────────────────────────────────┤
│ bergen  │     2 │ POLYGON ((5.2 60.3, 5.5 60.3, 5.5 60.5, 5.2 60.5, 5.2 60.3))  │
│ oslo    │     2 │ POLYGON ((10.6 59.8, 10.9 59.8, 10.9 60, 10.6 60, 10.6 59.8)) │
└─────────┴───────┴───────────────────────────────────────────────────────────────┘
```

The geometry column arrives as a DuckDB `GEOMETRY`, so `ST_GeomFromWKB` is not
needed and will be refused as a type error.

## The S3 backend

`make docker-run-s3` starts the rustfs endpoint from `compose.yml` next to the
service, with a one-shot seed container that fills the bucket before the API
comes up: the S3 API is on port 9000 and the browser console is on
<http://127.0.0.1:9001/rustfs/console/>, where the same `ocs/catalog`,
`ocs/raster` and `ocs/vector` prefixes appear as objects in the bucket. The credentials come from
`RUSTFS_ACCESS_KEY` and `RUSTFS_SECRET_KEY` and default to `rustfsadmin`;
`.env.example` has the matching `OCS_STORAGE_S3__` block. `make test-s3` starts
rustfs on its own, for the marked tests, and stops it again.

The service writes to S3 for real, so the console shows the objects this service
wrote. The key layout is identical, because it comes from one module regardless
of scheme, and
[backends and key layout](../concepts/backends-and-layout.md) explains why a
dataset is isolated by prefix rather than by bucket. The same objects can be
listed from Python:

```python
import obstore
from obstore.store import S3Store

store = S3Store(
    "ocs-storage-exploration",
    config={
        "endpoint": "http://127.0.0.1:9000",
        "region": "us-east-1",
        "access_key_id": "rustfsadmin",
        "secret_access_key": "rustfsadmin",
        "virtual_hosted_style_request": False,
    },
    client_options={"allow_http": True},
)
for item in obstore.list(store, "ocs").collect():
    print(item["path"])
```

One filesystem detail: deleting a dataset deletes every object below its prefix,
but obstore deletes objects rather than directories, so the empty
`raster/{dataset_identifier}` and `vector/{dataset_identifier}` directories are
left behind on disk. An object store has no directories, so there is nothing
equivalent to leave behind there.
