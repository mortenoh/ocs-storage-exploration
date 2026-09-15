# Offline quickstart

Everything in this repository runs without a network, once four things are on
the machine: the virtual environment, the sample files, the Docker images and
the DuckDB extensions. One target fetches all four.

Run step 1 while you still have a connection. Everything after it works on a
plane.

## 1. Once, online

```bash
make offline
```

It runs `make install`, `make samples`, `make docker-build`, pulls the rustfs
image and warms the DuckDB extensions, then prints what it cached:

```console
$ make offline
>>> Installing dependencies
>>> Fetching the downloadable samples
>>> Fetching samples into samples/downloaded
[present] samples/downloaded/chirps3/chirps3-2024-01-01.tif (6,430 bytes)
...
[present] samples/downloaded/geoboundaries-sle-adm2.geojson (936,499 bytes)
>>> Samples ready; `make demo` now runs entirely offline
>>> Building the service image
 Image ocs-storage-exploration-api Built
 Image ocs-storage-exploration-api-s3 Built
>>> Pulling the rustfs image
>>> Warming the DuckDB extensions

>>> Ready for offline use. Cached on this machine:
    - the virtual environment in .venv (uv sync --all-extras)
    - the sample files in samples/ and samples/downloaded/
    - the service images for the file and s3 compose profiles
    - the rustfs image compose.yml pins
    - the DuckDB spatial and httpfs extensions in ~/.duckdb
    Next, offline: make demo, make run, make test, make docs
```

One line confirms the four:

```bash
ls samples/downloaded/chirps3 | wc -l && docker image ls | grep -cE 'ocs-storage-exploration-api|rustfs' && uv run python -c "import rioxarray"
```

`14`, then a count of 3 or more, then no import error, means everything is in
place.
`make offline` is idempotent: run it again and it skips what is already there.

## 2. Offline, every time

### Ingest the samples

```bash
make demo
```

It writes five datasets into `./data` in process, without a running server:

```console
>>> Backend file, ingest roots ['samples', 'data']
[done] chirps3-sle-daily
[done] worldpop-sle-2026
[done] sle-districts
[done] sle-adm2-geoboundaries
[done] ne-lakes

>>> Ingested into the file backend
dataset                 kind        state      detail
----------------------  ----------  ---------  ----------------------------------------------------
chirps3-sle-daily       coverage    published  14 timesteps, 2024-01-01 to 2024-01-14, variable precipitation
worldpop-sle-2026       coverage    published  1 timestep, 2026-01-01 to 2026-01-01, variable population
sle-districts           collection  published  version 1, 13 features, id id
sle-adm2-geoboundaries  collection  published  version 1, 14 features, id shapeID
ne-lakes                collection  draft      version 1, 25 features, id id
```

Note: the filesystem backend makes Icechunk print a `WARN` about concurrent
commits on every write. It is Icechunk telling you that a local filesystem has
no conditional PUT, which is true and is why the S3 backend exists. It is not
an error and nothing is lost.

`make demo` overwrites what it wrote last time, so it can be run again after a
change. To start clean, `rm -rf data` first.

### Run the service and hit it

In one terminal:

```bash
make run
```

In another, each of these answers JSON:

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/v1/backends
curl -s http://127.0.0.1:8000/api/v1/datasets

# a coverage window: 14 days of rainfall over a box inside Sierra Leone
curl -s 'http://127.0.0.1:8000/api/v1/raster/chirps3-sle-daily/query?bbox=-13.3,7.9,-12.0,9.0'

# the same coverage over the first week only
curl -s 'http://127.0.0.1:8000/api/v1/raster/chirps3-sle-daily/query?start=2024-01-01T00:00:00&end=2024-01-07T00:00:00'

# the static population grid, one timestep
curl -s http://127.0.0.1:8000/api/v1/raster/worldpop-sle-2026/query

# the snapshots the 14 days left behind, newest first
curl -s http://127.0.0.1:8000/api/v1/raster/chirps3-sle-daily/versions

# features by envelope, and by clause with only two columns
curl -s 'http://127.0.0.1:8000/api/v1/vector/sle-districts/features?bbox=-13.3,7.9,-12.0,9.0'
curl -s 'http://127.0.0.1:8000/api/v1/vector/sle-districts/features?where=level:2&columns=name,level'
curl -s 'http://127.0.0.1:8000/api/v1/vector/sle-adm2-geoboundaries/features?limit=3'

# the same records as STAC
curl -s http://127.0.0.1:8000/stac/collections
curl -s http://127.0.0.1:8000/stac/collections/chirps3-sle-daily
```

What to expect:

```console
$ curl -s http://127.0.0.1:8000/health
{"status":"ok","version":"0.1.0","backend":"file"}

$ curl -s 'http://127.0.0.1:8000/api/v1/raster/chirps3-sle-daily/query?bbox=-13.3,7.9,-12.0,9.0'
{"dataset_identifier":"chirps3-sle-daily","variable":"precipitation",
 "bbox":{"minimum_x":-13.2999975,"minimum_y":7.8999992,"maximum_x":-11.9999975,"maximum_y":8.9999992},
 "crs":"EPSG:4326","snapshot_identifier":"43RVK929J5Z5SJVF4F6G","timestep_count":14,
 "cell_count":8008,"minimum":0.0,"maximum":2.034855842590332,"mean":0.007030892275411297}
```

`GET /stac/collections` lists four collections, not five: `ne-lakes` is left
unpublished on purpose, and STAC advertises only what is published. In a
browser, <http://127.0.0.1:8000/docs> is the generated OpenAPI page and every
endpoint can be tried from there.

### Read the docs locally

```bash
make docs
```

That serves this site at <http://127.0.0.1:8001>. It is fully offline: the
theme uses the system font stack rather than fetching Google Fonts.

### Open what the demo wrote

The files are ordinary objects under `./data`:

```console
$ find data -maxdepth 4 | sort
data/ocs/catalog/datasets/chirps3-sle-daily.json
data/ocs/catalog/datasets/ne-lakes.json
data/ocs/catalog/datasets/sle-adm2-geoboundaries.json
data/ocs/catalog/datasets/sle-districts.json
data/ocs/catalog/datasets/worldpop-sle-2026.json
data/ocs/raster/chirps3-sle-daily/chunks
data/ocs/raster/chirps3-sle-daily/manifests
data/ocs/raster/chirps3-sle-daily/repo
data/ocs/raster/chirps3-sle-daily/snapshots
data/ocs/raster/worldpop-sle-2026/...
data/ocs/vector/ne-lakes/versions
data/ocs/vector/sle-adm2-geoboundaries/current.json
data/ocs/vector/sle-districts/current.json
data/ocs/vector/sle-districts/versions
```

1.3 MB in total: the 14 CHIRPS days and the population grid are small, and
`ne-lakes` has no `current.json` because nothing was published for it.

[Inspecting the data](inspecting-the-data.md) opens them with Icechunk,
xarray, geopandas, pyarrow and DuckDB; every path in it becomes a real path
once `make demo` has run. The DuckDB snippet works offline because
`make offline` installed the extension:

```console
$ uvx --with duckdb python -c "
import duckdb
duckdb.sql('LOAD spatial;')
duckdb.sql(\"SELECT name, level FROM read_parquet('data/ocs/vector/sle-districts/versions/v00001/data.parquet') LIMIT 3\").show()
"
```

### Run the tests

```bash
make test
```

813 passed, 4 skipped. The tests marked `samples` use the CHIRPS files
`make samples` downloaded and skip themselves when those are absent, so the
suite is green either way.

### The S3 variant

The S3 backend needs an endpoint, which `compose.yml` provides. The image is
already pulled, so this works offline too. In one terminal:

```bash
docker compose up rustfs     # foreground; Ctrl-C stops it
docker compose down rustfs   # removes the stopped container and the network
```

The first runs rustfs in the foreground on <http://127.0.0.1:9000>, with its
console on <http://127.0.0.1:9001>. Ctrl-C stops the container but leaves it
stopped rather than removed, which is what the second line is for: run it once
you are finished and `docker ps -a` is empty again. The service has to be named
on both lines, because `compose.yml` puts rustfs behind a profile and a bare
`docker compose down` would not touch it. In another terminal, while rustfs is
up:

```bash
export OCS_STORAGE_BACKEND=s3
export OCS_STORAGE_S3__BUCKET=ocs-storage-exploration
export OCS_STORAGE_S3__REGION=us-east-1
export OCS_STORAGE_S3__ENDPOINT_URL=http://127.0.0.1:9000
export OCS_STORAGE_S3__ACCESS_KEY_ID=rustfsadmin
export OCS_STORAGE_S3__SECRET_ACCESS_KEY=rustfsadmin
export OCS_STORAGE_S3__ALLOW_HTTP=true
export OCS_STORAGE_S3__FORCE_PATH_STYLE=true

make demo
make run
```

The same demo, the same URLs, the objects in the bucket rather than on disk.
The rustfs console shows the `ocs/catalog`, `ocs/raster` and `ocs/vector`
prefixes appearing as objects.

`make test-s3` does not need any of that: it starts rustfs itself, runs the
`s3`-marked tests against it and stops it again, even when a test fails.

```bash
make test-s3
```

Alternatively `make docker-run-s3` runs the service and rustfs together in one
foreground stack on <http://127.0.0.1:8001>; Ctrl-C stops and removes both.
That one binds the API itself, so it is not the way to point a local `make
demo` at rustfs; `docker compose up rustfs` is.

## 3. What needs the network

Only these. Everything else in this repository is local.

| Step | Why it needs the network |
| --- | --- |
| `make samples` | Downloads CHIRPS days from `data.chc.ucsb.edu` and the geoBoundaries file from GitHub |
| `make install` on a cold cache | Resolves and downloads wheels; a warm `uv` cache makes it offline, and `uv sync --all-extras --offline` proves it |
| `make docker-build`, `docker compose pull` | Pulls the base image and the rustfs image |
| The DuckDB `INSTALL` statements | Downloads the `spatial` and `httpfs` extensions into `~/.duckdb` |
| An online STAC validator | Fetches the extension schemas the collections reference |

`make offline` runs the first four. Nothing else in `make demo`, `make run`,
`make test`, `make test-s3` or `make docs` opens a socket to the internet.
