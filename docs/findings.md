# Findings

The executive summary of this exploration for the OCS team. Six passes stand
behind it: the storage model, the S3 backend and Docker, the STAC catalog, a
hardening review that found eleven ways the prototype was right in the happy
path and wrong under a second writer and closed them all, pluggable backends,
and an async surface over bounded S3 clients. Every claim about OCS is cited as
a `file:line` against [the checkout](research/ocs-storage-today.md).

## TLDR

- A unified raster and vector storage layer with pluggable backends is doable: it is built here, and one test suite passes on the filesystem, in memory and against a live S3 endpoint.
- It is advisable for OCS, staged rather than in one cut, because each step below replaces one seam and leaves the rest of the service working.
- What it costs: six Icechunk constructor call sites, `ArtifactRecord.path` and its normaliser, the `_swap_store` rename, the `records.json` index, and an icechunk 2.0.5 to 2.2 upgrade as a prerequisite.
- What it buys first is S3 by configuration: `OCS_STORAGE_BACKEND=s3` plus credentials, with nothing above the backend rewritten.
- Publication becomes one conditional write with no window where the dataset is absent and no recovery routine; rollback is the same call against an older target.
- The vector half arrives at the same time: GeoParquet 1.1 with a covering bounding-box column, bounding-box and attribute pushdown, and per-version metadata.
- One catalogue and one tagged union cover both kinds of dataset, and a STAC catalog falls out as a projection of those records rather than a second index to keep in sync.
- The API is awaitable without pretending the engines are: every route is an `async def`, the catalogue is awaited natively through obstore, and the blocking engines run on worker threads behind a capacity limiter and a 504 timeout, with the S3 clients bounded per request in all three libraries.
- The largest risks: Icechunk's conformance on non-AWS S3, rustfs being pre-1.0, retention bounding how far a rollback reaches, serving Zarr chunks for remote stores, and one JSON object per dataset at thousands of datasets.
- Not proven: real ingestion sources, multiscale pyramids, moto as a Docker-free S3 double, and any scale beyond synthetic data.

## What was tested and how

Synthetic data only: a small cube and a handful of sample features, no OCS
dataset, no real ingestion source, and no file large enough to make row-group
pruning or pyramids measurable. The fixtures are cheap enough to run the whole
suite once per backend.
Addresses, keys, schemas, both catalogues, both engines and the API are
parametrised over all three. The default run is 771 tests at about 95 percent
statement and branch coverage; 300 of them carry the `s3` marker and run again
against rustfs `1.0.0-rc.6` under `make test-s3`, which starts the container and
stops it again even when a test fails.

The marked suite is not smoke tests. It asserts that the conditional PUT is the
real one, not the local emulation, that two catalogues racing one
record lose the create and the etag compare-and-swap, that two vector writers
reserve different version numbers, that a stale raster publish loses the branch
compare-and-swap, and that a second vector publisher can neither create the
pointer twice nor overwrite it with a stale etag. It also points a backend at an
endpoint nothing listens on and asserts that a raster create, a catalogue put
and a Parquet write each give up in under a second rather than hanging. Prefix
deletes are swept on teardown and a full run leaves the bucket empty.

## Verdict per design question

| Question | Decision | Evidence | Confidence | Open risk |
| --- | --- | --- | --- | --- |
| Addressing model | A `StorageAddress` URI, never a bare `Path`; userinfo rejected | Round-trips through records on all three backends, replacing `artifact_paths.to_absolute` | High | Stored records need a migration |
| One resolver | One backend yields all three handles from one settings block | All three backends implement it and no engine knows which | High | Icechunk and obstore reach S3 through different clients |
| Publication without rename (raster) | `reset_branch` with `from_snapshot_id`; the branch is the publication truth | On rustfs the losing publish gets 409 and an unpublished coverage answers 404 | High | Expiry must keep published snapshots reachable |
| Publication without rename (vector) | An immutable `versions/vNNNNN` plus a conditionally written `current.json` | Create and etag replace both lose correctly on rustfs; `reservation.json` makes the number unique | High | `LocalStore` has no conditional update, so the filesystem backend emulates it |
| obstore vs fsspec vs `pyarrow.fs` | obstore for objects, `pyarrow.fs` for Parquet, fsspec rejected | Icechunk does its own I/O through Arrow `object_store` and never sees fsspec | High | obstore 0.11 is young and has no pyarrow adapter |
| GeoParquet encoding and pushdown | 1.1.0 declared, WKB, covering bbox, zstd, Hilbert sort | `bbox=` prunes row groups and composes with `filters=`; envelope hits are re-filtered | Medium | The row-group hit rate is unmeasured |
| Catalogue storage | One JSON object per dataset, conditional create or revision compare-and-swap | Replaces `records.json` under portalocker; concurrent writers lose correctly on S3 | Medium | Thousands of datasets need a derived index |
| `item_type` discriminator | A pydantic tagged union of `coverage` and `feature` | One exhaustive `match` replaces 16 scattered `ArtifactFormat.ICECHUNK` comparisons | High | A third item type has not been tried |
| STAC projection | Projected from the version advertised, never stored | Round-tripped through `pystac` and checked once with `stac-validator` | Medium | datacube v2.2.0 has a dead schema reference; listings need paging |
| Local S3 testing | rustfs behind a marker, with the whole suite re-run | Found what two local backends could not: `create_dir` leaves undeletable markers | Medium | rustfs is pre-1.0, MinIO untried, moto unverified |
| Sync engines behind an async facade | Routes are `async def`; the catalogue is awaited through obstore and the engines run on bounded worker threads | A saturation test answers `/health` while sixteen storage calls are in flight, the limiter caps them and an over-long call answers 504 | High | A timed-out thread is abandoned, not cancelled, because no library here can be interrupted |
| Bounded S3 clients | One block of timeouts and retries translated into obstore, Icechunk and pyarrow | An unreachable endpoint fails a raster create, a catalogue put and a Parquet write in under a second each, as 503 | Medium | The three clients only approximate one another; a slow endpoint, as opposed to an absent one, is untested |

## Recommendation for OCS

A staged path, each step shippable alone.

1. **Upgrade icechunk to 2.2 first.** `uv.lock` has already moved there while the
   checkout runs 2.0.5. 2.1.2 carried an etag fix and a reworked exception tree,
   2.2 fixes `to_icechunk` against xarray 2026.x, and from 2.1 an S3 prefix must
   be non-empty, which the key layout depends on.
2. **CLIM-555, first seam.** Put `StorageAddress` and a backend resolver behind
   `streaming/store.py:33`. It is the only call site already shaped like a
   resolver, and four tests monkeypatch it by name, so injecting a backend
   replaces a substitution the suite already makes.
3. **CLIM-555, remaining call sites.** Move `accessor.py:170`,
   `downloader.py:293` and `:414`, `ingestions/services.py:1208` and
   `stac/media_types.py:107` onto the resolver, and replace `ArtifactRecord.path`
   and `artifact_paths.to_absolute` with a URI, migrating stored records once.
   The rejection at `sync_engine.py:436` becomes a scheme allowlist.
4. **CLIM-555, catalogue and backend.** Replace `records.json` under portalocker
   with one conditionally written object per dataset, then enable S3 by
   configuration.
5. **CLIM-880.** Publish by resetting a `published` branch, and delete
   `_swap_store` (`ingestions/services.py:897`), `recover_interrupted_swap` and
   the `.retired` and `.failed` states. Constrain the
   `expire_snapshots(older_than=now)` call at `streaming/orchestrator.py:347`
   before that branch exists, or the first rollback target will list and
   fail on open.
6. **CLIM-1067 and CLIM-1068.** Build the feature store on the vector engine,
   replacing the bare `gdf.to_parquet(path)` at `openeo/jobs.py:1585`. Two notes
   for the CLIM-1067 schema: `primary_geometry` should name the geometry column,
   and the geometry types present belong in a separate field, because
   administrative boundaries mix `Polygon` and `MultiPolygon`.

Two behaviours change for clients. A dataset with nothing published answers 404
for `version=published` rather than serving the newest draft, which is the point, not
a regression. And `serve_icechunk_file` is a `FileResponse`
passthrough that does not port, so `/zarr` and `/icechunk` for a remote store
need a decision — presigned redirect, byte proxy, or `icechunk.http_storage()`
against a read-only endpoint — before a client that cannot reach the bucket can
be served at all.

## What we would do differently

- Write per-version metadata from the first write rather than adding a sidecar
  later. Taking a version's coordinate reference system, feature count and
  identifier property from the catalog record meant describing the newest write
  while serving an older one, and every symptom was found in review, not by a
  test.
- Make the pointer the publication truth from the start. Treating the record as
  the answer produced a reader that served a draft as published after an
  interrupted publication.
- Use a conditional create everywhere a thing is registered once. An
  unconditional first write for a new record, and a version number chosen by
  listing, both looked safe only because the happy path never collides.
- State the threading model before writing the first route. The routes were
  written `async def`, then turned into plain `def` with the caches guarded
  afterwards, then made `async def` again over a facade that bounds the threads.
  Shared state was discovered rather than designed, and the route signatures
  changed twice for a decision that could have been made once.
- Bound every client at the moment it is built. The three S3 clients ran on
  their own defaults for four passes, which meant a wrong endpoint hung instead
  of failing, and nothing in the suite would have caught it.

## Not advisable

- An fsspec mapper as the storage abstraction. Icechunk never sees it, so it
  wraps everything except the component doing the reading.
- A bucket per dataset. Bucket creation is privileged, rate-limited and globally
  named; a prefix per dataset gives the same isolation with none of that.
- Tags as publication pointers. A tag is immutable and its name cannot be
  recreated once deleted, so a pointer that has to move must be a branch.
- Disabling `unsafe_use_conditional_create` or `unsafe_use_conditional_update`
  to make a test pass. A test that only passes with them off has proved the code
  is broken under the configuration everyone runs.
