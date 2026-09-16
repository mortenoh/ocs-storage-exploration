# OCS storage exploration

A sandbox for one question: what does a storage abstraction for the Open
Climate Service have to look like so that adding S3 is a configuration change
rather than a redesign?

This is a from-scratch FastAPI and pydantic service, not a fork of OCS. It
implements one storage model covering both halves of the problem — raster
datasets as Icechunk-backed GeoZarr and vector collections as versioned
GeoParquet — over pluggable backends. All three backends work today: filesystem,
memory and S3, the last of them verified against a local rustfs endpoint by the
whole test suite rather than by a handful of backend tests.

## The question

OCS opens every Icechunk store with `icechunk.local_filesystem_storage`, at six
call sites. Swapping that constructor is easy. What is not easy is everything
the constructor's argument touches: stores are addressed as bare filesystem
paths that a record round-trip mangles into nonsense when they are URIs,
publication is a two-step directory rename that S3 has no equivalent for, and
the artifact index is a single JSON file guarded by an advisory lock that does
not cross hosts. On top of that, the vector side of the service does not exist
yet, so any abstraction built for raster alone would be rebuilt within the year.

This repo answers the question by building the thing, then writing down what it
took. Start with the [problem statement](research/problem-statement.md).

The short answer is on the [findings](findings.md) page: what was proved, what
it costs, and what to do about it.

## What it demonstrates

- URI addressing (`file:///`, `memory://`, `s3://bucket/...`) with credentials
  kept out of every stored record.
- One backend protocol resolving one address into three handles: an
  `icechunk.Storage`, an obstore `ObjectStore`, and a `pyarrow.fs.FileSystem`.
- A catalogue of one JSON record per dataset, written with etag
  compare-and-swap, where the record is what makes a dataset exist.
- A discriminated union on `item_type` (`coverage` or `feature`) that turns
  scattered format comparisons into one exhaustive `match`.
- Publication as a pointer move: a branch reset with compare-and-swap for
  raster, an etag-conditional pointer object for vector. No renames, and
  rollback is the same call with an older target.
- A synthetic raster and vector round trip over HTTP, so the model can be
  exercised without any OCS data, and a real one beside it: CHIRPS rainfall,
  WorldPop population, DHIS2 organisation units and geoBoundaries areas ingested
  from files through the same engines.
- A STAC catalog at `/stac` that is a projection of the same records rather than
  a second index: coverages with the datacube extension, feature collections
  with the table extension, and only what is published.

## Running it

Going offline? [The offline quickstart](guides/offline-quickstart.md) is the
numbered copy-paste sequence, with the expected output of every step:
`make offline` once while online, then everything else with no network.

```bash
make offline      # the one online step: deps, samples, images, DuckDB extensions
make demo         # ingest the real sample datasets into ./data
make run          # uvicorn on http://localhost:8000
make test         # pytest, S3 tests skipped by default
make test-s3      # start rustfs, run the S3-marked tests, stop rustfs again
make docs         # build and serve these docs
```

`make help` lists every target. `make lint` runs ruff, mypy and pyright and
`make coverage` produces a coverage report. `make test-s3` is self-contained: it
starts rustfs, runs everything marked `s3` against it and stops it again even
when a test fails. `make docker-run-file` and `make docker-run-s3` run the
service itself in a container, in the foreground under a trap, so Ctrl-C stops
and removes the stack and nothing is left behind. Both seed themselves with the
five sample datasets first, so the API answers with data in it.

Once the service is up, `GET /health` and `GET /api/v1/backends` confirm which
backend is active — the backend description never contains a secret. The raster
lifecycle is create, append, query with a bounding box, publish, list versions,
roll back. The vector lifecycle is create from a GeoJSON feature collection,
query by bounding box and attribute, publish. Both are deleted through
`DELETE /api/v1/datasets/{id}` and both are readable as STAC at `/stac`,
`/stac/collections` and `/stac/collections/{id}`.

## Concepts and guides

Three pages explain the model as built rather than as designed:
[versioning](concepts/versioning.md) covers Icechunk snapshots, GeoParquet
version directories and the one publish vocabulary over both,
[backends and key layout](concepts/backends-and-layout.md) covers the three
handles a backend yields, the `OCS_STORAGE_` settings and the key layout on S3,
and [the STAC catalog](concepts/stac-catalog.md) covers the projection of a
record onto a Collection and why only published versions are advertised.

Four more are meant to be followed with a terminal open: the
[offline quickstart](guides/offline-quickstart.md) prepares a machine and then
runs everything without a network, [real data](guides/real-data.md) covers the
five sample datasets and the two ingest endpoints, the
[API walkthrough](guides/api-walkthrough.md) exercises every endpoint with
`curl`, and [inspecting the data](guides/inspecting-the-data.md) opens what it
wrote with Icechunk, xarray, geopandas, pyarrow and DuckDB.

## Research

The [research section](research/problem-statement.md) is the point of the
repo. It covers, in order:

| Page | Subject |
| --- | --- |
| [Problem statement](research/problem-statement.md) | Why S3 is an addressing and publication change |
| [OCS storage today](research/ocs-storage-today.md) | Verified inventory of what OCS does now |
| [Icechunk backends](research/icechunk-backends.md) | What Icechunk actually supports, and the version to target |
| [GeoParquet](research/geoparquet.md) | Covering bounding boxes, pruning, and the identity contract |
| [obstore and fsspec](research/obstore-vs-fsspec.md) | Why two client libraries rather than one abstraction |
| [Unified model](research/unified-model.md) | The design, and the mapping back to OCS |
| [Publication without a rename](research/publication-without-rename.md) | Three publication mechanisms on five criteria |
| [Testing S3 locally](research/testing-s3-locally.md) | rustfs, in-memory, and why moto cannot help |

[Architecture](architecture.md) has the layer diagram and the decision table,
and [the roadmap](roadmap.md) has what is delivered and what is next.
Every claim about OCS in these pages cites a `file:line` in
`dhis2/open-climate-service` and was verified against the checkout rather than
recalled. Where a number should be measured rather than asserted, the page says
so instead of guessing.

## Status

Seventh pass. The storage model, both engines, all three backends, the STAC
projection, the async surface and the ingest of real files are implemented and
tested, and the whole suite also runs against a local rustfs endpoint.
[The roadmap](roadmap.md) lists what is delivered and what would come next,
starting with fetching a period rather than only reading a file that is already
on the machine. Nothing here is a migration; it is the argument for one.
