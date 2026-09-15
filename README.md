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
make install
make test
make run
curl http://127.0.0.1:8000/health
```

Copy `.env.example` to `.env` to change the backend, the data directory or the guard thresholds. Every setting is
read from an `OCS_STORAGE_` environment variable, and nested object storage settings use a double underscore, for
example `OCS_STORAGE_S3__ENDPOINT_URL`.

## Make targets

| Target | What it does |
| --- | --- |
| `make install` | Install every dependency including the optional extras |
| `make lint` | Run ruff format, ruff check, mypy and pyright |
| `make test` | Run the test suite, excluding the tests marked `s3` |
| `make test-s3` | Start rustfs, run the `s3`-marked tests against it, then stop it again |
| `make coverage` | Run the test suite with coverage reporting |
| `make run` | Run the service with reload on `PORT` (default 8000) |
| `make docs-serve` | Serve the documentation on `DOCS_PORT` (default 8001) |
| `make docs-build` | Build the documentation site in strict mode |
| `make docker-build` | Build the service image for both compose profiles |
| `make docker-run` | Alias for `make docker-run-file` |
| `make docker-run-file` | Run the service on the filesystem backend on port 8000, in the foreground; Ctrl-C stops it |
| `make docker-run-s3` | Run the service on the S3 backend with rustfs on port 8001, in the foreground; Ctrl-C stops it |
| `make docker-down` | Stop and remove the containers of both profiles, after an interrupted run |
| `make clean` | Remove caches and build output |

No target leaves anything running. `make docker-run-file` and `make docker-run-s3`
run `docker compose up` in the foreground, so Ctrl-C stops the stack, and
`make docker-down` is the cleanup for a run that was interrupted some other way.
`make test-s3` starts rustfs, runs the tests and stops rustfs again even when a
test fails, and it reports pytest's exit code.

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

## Documentation

The research notes, the architecture write-up and the generated API reference live at
[mortenoh.github.io/ocs-storage-exploration](https://mortenoh.github.io/ocs-storage-exploration).

## License

BSD-3-Clause. See [LICENSE](LICENSE).
