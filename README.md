# OCS storage exploration

[![CI](https://github.com/mortenoh/ocs-storage-exploration/actions/workflows/ci.yml/badge.svg)](https://github.com/mortenoh/ocs-storage-exploration/actions/workflows/ci.yml)
[![Documentation](https://github.com/mortenoh/ocs-storage-exploration/actions/workflows/docs.yml/badge.svg)](https://mortenoh.github.io/ocs-storage-exploration)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)
[![License: BSD-3-Clause](https://img.shields.io/badge/license-BSD--3--Clause-blue.svg)](LICENSE)

The Open Climate Service stores managed raster datasets as Icechunk-backed GeoZarr addressed by bare local
filesystem paths, and its vector side is only half built. This repository is a from-scratch FastAPI and pydantic
sandbox that prototypes one storage abstraction covering raster (Icechunk and GeoZarr) and vector (GeoParquet)
over pluggable backends. It exists to prove the addressing, catalog and publication design and to write up a
migration path, not to become a production service.

## Quick start

```bash
make offline    # everything that needs the network: deps, samples, images, DuckDB extensions
make demo       # ingest the real sample datasets into ./data
make run
curl http://127.0.0.1:8000/health
```

`make offline` is the only step that needs a connection. Everything after it,
including the tests and the documentation site, works with no network at all:
[the offline quickstart](docs/guides/offline-quickstart.md) is the numbered
copy-paste version, with the expected output of each step.

The service answers a JSON API under `/api/v1` and a STAC catalog under `/stac`:

| Endpoint | What it does |
| --- | --- |
| `GET /health` | Report the version and the active backend |
| `GET /api/v1/backends` | Describe the active backend and every registered scheme, without secrets |
| `GET /api/v1/datasets` | List catalog records, optionally filtered by item type |
| `GET`, `DELETE /api/v1/datasets/{id}` | Read or delete one record, whichever engine owns the bytes |
| `POST /api/v1/raster/{id}`, `/append`, `/publish` | Write, extend and publish a coverage |
| `POST /api/v1/raster/{id}/ingest` | Read local GeoTIFF, COG, NetCDF or Zarr files as one coverage |
| `GET /api/v1/raster/{id}/query`, `/versions` | Summarise a window; list the snapshots |
| `POST /api/v1/vector/{id}`, `/publish` | Write and publish a version of a collection |
| `POST /api/v1/vector/{id}/ingest` | Read a local GeoJSON or GeoParquet file as the next version |
| `GET /api/v1/vector/{id}/features` | Read features by envelope, clause and column |
| `GET /stac` | STAC landing page with `conformsTo` and one child link per dataset |
| `GET /stac/collections`, `/stac/collections/{id}` | Every record, or one, projected onto a STAC Collection |

Copy `.env.example` to `.env` to change the backend, the data directory, the guard thresholds, the concurrency
and timeout bounds of the storage calls, or the timeouts and retries of the S3 clients. Every setting is read from
an `OCS_STORAGE_` environment variable, and nested object storage settings use a double underscore, for example
`OCS_STORAGE_S3__ENDPOINT_URL`.

## Make targets

| Target | What it does |
| --- | --- |
| `make install` | Install every dependency into the virtual environment |
| `make offline` | Run every step that needs the network, so the rest works offline |
| `make samples` | Download and clip the sample files; needs the network, once |
| `make demo` | Ingest every sample into the configured backend; offline |
| `make lint` | Run ruff format, ruff check, mypy and pyright |
| `make test` | Run the test suite, excluding the tests marked `s3` |
| `make test-s3` | Start rustfs, run the `s3`-marked tests against it, then stop it again |
| `make coverage` | Run the test suite with coverage reporting |
| `make run` | Run the service with reload on `PORT` (default 8000) |
| `make docs-serve` | Serve the documentation on `DOCS_PORT` (default 8001) |
| `make docs-build` | Build the documentation site in strict mode |
| `make docker-build` | Build the service image for both compose profiles |
| `make docker-run` | Alias for `make docker-run-file` |
| `make docker-run-file` | Run the service on the filesystem backend on port 8000, in the foreground; Ctrl-C stops and removes it |
| `make docker-run-s3` | Run the service on the S3 backend with rustfs on port 8001, in the foreground; Ctrl-C stops and removes it |
| `make clean` | Remove caches and build output |

No target leaves anything running. `make docker-run-file` and `make docker-run-s3`
run `docker compose up` in the foreground under a shell trap that runs
`docker compose down` however the run ends, so Ctrl-C stops and removes the
containers and the network and `docker ps -a` is empty afterwards. `make test-s3`
starts rustfs, runs the tests and stops rustfs again even when a test fails, and
it reports pytest's exit code.

## Running it in Docker

```bash
make docker-run-file    # http://127.0.0.1:8000, data in ./data
make docker-run-s3      # http://127.0.0.1:8001, data in rustfs on http://127.0.0.1:9000
```

The image is built from `Dockerfile` with `uv sync --frozen --no-dev` and runs
as a non-root user. `compose.yml` has two profiles: `file` runs the service on
the filesystem backend with `./data` bind-mounted, and `s3` runs the same image
on the S3 backend next to a rustfs endpoint. The two API services use different
host ports so they never collide.

Because `./data` is bind-mounted and a bind mount keeps the host's ownership on
Linux, `make docker-run-file` creates the directory and passes `OCS_UID` and
`OCS_GID` to compose, which the `api` service reads as
`user: "${OCS_UID:-999}:${OCS_GID:-999}"`. A bare `docker compose --profile file
up` still runs as the uid the image builds.

## Real data

`samples/` holds three small real files (DHIS2 organisation units, Natural Earth
lakes, a WorldPop population grid) and `make samples` downloads two more (14 days
of CHIRPS v3.0 rainfall clipped to Sierra Leone, and the geoBoundaries ADM2
areas). `make demo` ingests all five, and
[the real data guide](docs/guides/real-data.md) explains what they are, what the
ingest does to them and how to point the two ingest endpoints at your own files.

## Documentation

The research notes, the architecture write-up and the generated API reference live at
[mortenoh.github.io/ocs-storage-exploration](https://mortenoh.github.io/ocs-storage-exploration).
`make docs` serves the same site locally, with no network.

## License

BSD-3-Clause. See [LICENSE](LICENSE).
