# Roadmap

What this sandbox has proved, and what it would have to prove next before any
of it lands in the Open Climate Service.

## Delivered

**Pass 1: the model.** URI addressing with `StorageAddress` and one key module
that every key comes from; the `StorageBackend` and `Catalog` protocols; the
filesystem and memory backends; `ObjectCatalog`, one JSON object per dataset
with etag compare-and-swap; the raster engine over Icechunk and GeoZarr and the
vector engine over versioned GeoParquet; one publish vocabulary covering an
Icechunk branch move and a pointer swap, with rollback as the same call against
an older target; the JSON API over both halves; and the research notes,
concepts and guides that explain why each of those is shaped the way it is.

**Pass 2: S3 and Docker.** The S3 backend, building the Icechunk storage, the
obstore store and the pyarrow filesystem from one settings block, verified
against a local rustfs endpoint by the whole test suite rather than by a
handful of backend tests. The conditional-PUT paths of the catalogue record and
the vector pointer merged into one module, with the non-atomic emulation
reachable only on obstore's `LocalStore`. A `Dockerfile` for the service and a
`compose.yml` with a `file` profile and an `s3` profile, plus `make test-s3`,
which starts rustfs, runs the marked tests and stops rustfs again.

**Pass 3: the STAC catalog.** Every dataset record projected onto a STAC
Collection at `/stac`, `/stac/collections` and `/stac/collections/{id}`:
coverages with the datacube extension and an Icechunk asset advertised as
`application/vnd.zarr; version=3`, feature collections with the table extension
(`table:row_count`, `table:primary_geometry`, `table:columns` read from the
Parquet footer) and a GeoParquet asset at `application/x-parquet`. Licence and
attribution are record fields, threaded through both create endpoints and
preserved across an overwrite or an append, and `license` plus `providers` carry
them into the collection. `published_only` defaults to true so a draft is never
advertised. It is a projection, not new state: the record contributes identity,
title, licence and attribution, and everything else is read back from the
version being advertised. See [the STAC catalog](concepts/stac-catalog.md).

**Pass 4: hardening.** A review of the three passes above found eleven places
where the prototype was right in the happy path and wrong under a second writer,
a second thread or a rollback. All eleven are closed:

- The catalogue's compare-and-swap moved onto revision tokens: `get_entry`
  returns the record with the revision it was read at, `put` takes that revision
  or `create=True`, and the per-instance etag memory that let an unrelated read
  launder a stale write is gone.
- The `published` branch became the publication truth: a reader follows the
  branch rather than the record, a coverage with nothing published answers 404
  instead of serving the newest draft, and `reconcile_publication` repairs a
  record left behind by an interrupted publication.
- A coverage now describes the snapshot it serves rather than its record:
  `RasterRepository.describe` reads the dimensions, projection, envelope, time
  axis and variables back from the opened store.
- A vector version now describes itself: a `metadata.json` sidecar records the
  coordinate reference system, feature count, identifier property and selectable
  columns of the version as it was written, and reads, guards and publications
  take those fields from it rather than from the latest write.
- A vector version number is claimed before anything is written, by creating
  `reservation.json` in `mode="create"`, so two writers listing at the same
  moment can no longer pick the same number and overwrite each other.
- The STAC catalog is built from the version being advertised rather than from
  the record, so an unpublished append no longer widens the published temporal
  extent and a draft in another frame no longer changes the published CRS.
- Blocking storage calls moved off the event loop: every route that reaches the
  service layer is a plain `def` that FastAPI runs on the threadpool, and the
  caches that several threads now share are guarded.
- Cube allocation is guarded by `max_cube_cells` before the array is built,
  rather than only after a query had already read it.
- The memory backend drops the Icechunk storages cached under a prefix when the
  prefix is deleted, so a new dataset of the same name no longer inherits the
  old history.
- A raster window is selected by masking coordinate values rather than by
  slicing labels, so a bounding box that crosses the antimeridian selects the
  cells on both sides of it.
- GeoJSON input is validated by geojson-pydantic models that name where a
  malformed document goes wrong, with an unreadable coordinate reference system
  answered as 400 and structurally wrong vector input as 422.

**Pass 5: pluggable backends.** The backend seam became a
[pluginkit](https://winterop-com.github.io/pluginkit) extension point rather
than a table of factories. `StorageBackendSpecs` declares three typed extension
points, the filesystem, memory and S3 backends are three plugins registered by
name before any entry point is loaded, and an external distribution adds a
scheme by advertising itself under the `ocs_storage_exploration.plugins` group.
`examples/plugins/ocs-storage-null/` is the worked external plugin, deliberately
not installed. `GET /api/v1/backends` describes every provided scheme without
building a client or reading a credential, and a findings page summarises the
exploration for the OCS team.

**Pass 6: async surface and bounded S3 clients.** The plain `def` routes of pass
4 became `async def` over `AsyncStorageService`, because the caller this is
meant for is an async FastAPI service that wants awaitables rather than a
threadpool hop it does not control:

- `AsyncObjectCatalog` answers record reads and writes natively through
  obstore's `get_async`, `put_async`, `head_async`, `delete_async` and async
  listing, with the same compare-and-swap semantics as `ObjectCatalog` and the
  key layout, record encoding and failure mapping shared rather than restated.
  obstore is the one library here with an async API, so it is the one layer that
  does not need a thread.
- Every raster and vector engine call runs on a worker thread through
  `anyio.to_thread.run_sync`, behind an `anyio.CapacityLimiter` sized by
  `OCS_STORAGE_MAX_CONCURRENT_STORAGE_OPERATIONS` and inside an
  `asyncio.timeout` sized by `OCS_STORAGE_STORAGE_OPERATION_TIMEOUT_SECONDS`,
  which reports `StorageTimeoutError` as 504.
- The S3 clients are bounded where they are built: `connect_timeout_seconds`,
  `request_timeout_seconds`, `max_retries` and `retry_backoff_seconds` are
  translated once into obstore's `client_options` and `RetryConfig`, pyarrow's
  `connect_timeout`, `request_timeout` and retry strategy, and Icechunk's
  `network_stream_timeout_seconds` plus a `RepositoryConfig` carrying
  `StorageSettings(timeouts=..., retries=...)`, which
  `StorageBackend.repository_config()` hands to `Repository.open_or_create`. An
  unreachable endpoint now fails in under a second with
  `BackendUnavailableError` (503) instead of hanging, and the conditional-write
  switches are never touched.
- The plugin manager moved next to the plugins it registers:
  `build_plugin_manager` and `default_plugin_manager` are in
  `storage/backends/`, the facade that wrapped them is gone, and
  `plugins.backend_for_scheme` is the single place that refuses a scheme no
  plugin provides.

**Pass 7: real-data ingestion.** The model is now exercised by files somebody
else produced rather than only by synthetic cubes and hand-written features.
`POST /api/v1/raster/{id}/ingest` reads local GeoTIFF, COG, NetCDF and Zarr
files and `POST /api/v1/vector/{id}/ingest` reads local GeoJSON and GeoParquet,
both bounded by `Settings.ingest_roots` so a path outside the named directories
is refused before anything is opened:

- `storage/raster/ingest.py` normalises an opened raster onto the coverage
  contract with the rules the Open Climate Service applies to a fetched period,
  in the order that makes them safe: axis names, then the projection, then the
  longitude roll and the y reversal that need to know whether x is a longitude.
  The nodata sentinel is masked to NaN and kept as a finite attribute, the CF
  encoding is dropped, and the `GridSpecification` is derived from the data
  rather than declared by the request, so an ingest cannot claim a grid the file
  does not have. A static raster such as a population grid gets its single
  timestep from the request, so the `t` axis is always present.
- A glob is expanded, ordered by timestamp and written through the same `create`
  and `append` as the synthetic path, so the cube guard, the coordinate check
  and the variable check all still apply. The 14 CHIRPS days become one Icechunk
  repository with fourteen commits and a published branch.
- `samples/` carries three small real files and `make samples` fetches two more
  by clipping cloud optimised GeoTIFFs over HTTP range requests, so 14 days of
  global rainfall arrive as 60 kB rather than 420 MB. `make demo` ingests all
  five offline and `make offline` is the single online step that prepares a
  machine. See [real data](guides/real-data.md) and
  [the offline quickstart](guides/offline-quickstart.md).

**Pass 8: second review.** A second review, run after the real-data pass,
found thirteen more places where the prototype held in the happy path and not
under a second writer, a rollback or an unusual input, and one more came from
reading the demo output. All fourteen are closed, each with a regression test
that was red before the fix:

- A raster write is staged on a scratch branch and `main` is fast-forwarded only
  after the catalog compare-and-swap succeeds, so a losing writer never touches
  the draft; an Icechunk commit conflict answers 409 rather than 500.
- Deletion is a reservation: the record is marked `deleting` under a
  compare-and-swap, the prefix is swept, and the record goes last, so a reused
  identifier never inherits the objects of the dataset it replaced.
- A timed-out storage call keeps its limiter token until its thread returns, so
  the concurrency bound holds under timeouts, and queued callers time out waiting
  for a token instead of starting more threads.
- Scaled GeoTIFFs are decoded on open and the nodata sentinel is recorded in the
  decoded units; NetCDF has an engine (`h5netcdf`) and a test; Zarr directory
  stores resolve through the ingest path resolver.
- The synthetic cube is built on the worker thread inside the limiter and the
  timeout, and the cube size guard reads the settings of the application that
  was asked rather than a process-wide default.
- Nodata, licence and attribution are facts of the selected snapshot or version:
  the grid stamps nodata on every variable, commit metadata carries the raster
  licence, and the STAC projection reads both from the advertised version.
- An append must strictly extend the time axis, and a time window is selected
  by mask rather than by label slice, so a non-monotonic axis can no longer be
  written or misread.
- Records hold a backend-relative storage key rather than an absolute URI, so a
  data directory mounted elsewhere or moved to another machine serves correct
  hrefs.
- The filesystem Docker profile runs as the host user over the bind mount, the
  rustfs lifecycle in `make test-s3` installs its cleanup trap before starting
  the container, and the S3 job in CI is required.

**Pass 9: seeded demo stacks and an end-to-end demo test.** The two compose
profiles now come up with data in them. Each carries a one-shot seed container
running the same demo as `make demo`, from the same image as the API and with
`./samples` bind mounted read-only, and the API depends on it with
`condition: service_completed_successfully`, so the first request to
`/api/v1/datasets` lists five datasets rather than nothing. The data outlives
the containers, in `./data` and in `./.rustfs`, and a second run overwrites each
coverage under the same identifier and writes the next version of each
collection: the seed is idempotent in the set of datasets rather than in the
number of versions. The demo moved from
`scripts/demo.py` into `ocs_storage_exploration.demo` with an
`ocs-storage-exploration-demo` entry point, which is what makes the same code
reachable from the image, from `make demo` and from the tests. It reports a
refused dataset as a failed outcome and exits non-zero, which aborts the stack
instead of bringing up an API with half a catalog, while a sample `make samples`
never downloaded only skips. The run targets moved to
`--abort-on-container-failure`, because `--abort-on-container-exit` reads the
seed's successful exit as a reason to stop everything and races the API its
completion just released. `tests/test_demo_end_to_end.py` drives the demo's own
functions over the filesystem, memory and S3 backends and then asserts the API:
the listing and its publication states, a WorldPop window, a `where` clause on a
column the demo declares, a re-seeded collection rolled back to version 1, the
published-only STAC listing with its table row count, and the relative storage
keys in the records.

**Pass 10: third review.** A third review, run after the demo stacks, found five
more places where the prototype held in the happy path and not under a second
actor, an unusual window or a large answer. All five are closed, each with a
regression test that was red before the fix:

- A deletion never sweeps without its reservation: a lost compare-and-swap is
  retried, a persistent conflict answers 409, and a record another writer
  recreated after the mark is left alone rather than swept out from under it.
- Raster ingest checks the cube guard before any cell of a source is read, so an
  oversized file is refused rather than loaded and then refused.
- A longitude window spanning the full circle selects every cell, instead of
  wrapping onto an empty intersection.
- A draft STAC asset names the `main` branch, pins the snapshot it describes and
  carries the matching selector on its API href, so what it advertises and what
  the href returns cannot disagree.
- The GeoJSON conversion of a vector read runs on the bounded worker call that
  produced the handle, through `AsyncVectorCollectionStore.read_as`.

**Pass 11: fourth review.** A fourth review found five more, in the seams the
third one had just moved. All five are closed the same way:

- Each generation of a dataset gets its own storage prefix, named by the record,
  so a deletion only sweeps the generation it reserved and can no longer erase a
  dataset that was created again under the same identifier.
- Reconciliation restores the grid, the envelope and the terms from the
  committed snapshot, and a rejected overwrite puts the record it replaced back.
- Ingest plans resolve on the worker inside the timeout, and a glob is refused
  where its walk would start rather than after it has been expanded.
- `spatial_ref` and the grid's own axis names are refused as variable names,
  because assigning the coordinate would otherwise write over the data variable
  of the same name and report the coverage as created.
- Timestamps outside the range a nanosecond time coordinate holds are refused at
  write, and query bounds outside it are clamped onto it rather than wrapped.

**Pass 12: tombstones, a recoverable title and an answer rendered off the loop.**
A fifth review found the last window in the deletion protocol, and four smaller
things around it. All five are closed, each with a regression test that was red
before the fix:

- A deletion never removes its catalog record. It marks the record, sweeps the
  one generation that mark names, and then swaps the mark for a tombstone under
  the revision it holds, so every transition a record makes is a compare-and-swap
  even though obstore exposes no conditional delete. Dropping the record used to
  be a read followed by an unconditional delete, which could erase the record of
  a dataset written again between the two. A tombstone answers 404 exactly as an
  identifier nothing was ever written under, and the next write of that name
  replaces it in a generation of its own, inheriting nothing and, because the
  tombstone is checked before the item type, of either item type.
- A write that takes over a deletion that is still running keeps the bytes it has
  already written. The deletion tombstones the mark that write is claiming
  against, so the claim is re-read and retried against the tombstone rather than
  answering 409 for an identifier nobody owns. Only a live record means another
  writer took the name.
- The title of a coverage travels in the Icechunk commit metadata beside the
  licence and the attribution, so reconciliation restores the title a rejected
  overwrite left behind. It was the one field an overwrite changed that the store
  never held. A snapshot committed before the title travelled carries none, and
  reconciliation then leaves the record alone rather than blanking it.
- A feature read is serialised to its response bytes on the bounded worker call
  that produced the handle, so nothing of a fifty thousand feature answer is
  encoded on the event loop.
- A data variable name the coordinates of the grid being written would take is
  refused at the request boundary, by the engine's own check rather than a copy
  of its reserved names.


## Next

1. **Fetching as well as reading.** The ingest reads what is already on the
   machine. Driving an OCS dataset plugin's `fetch_period` straight into
   `RasterRepository.append`, so a period is fetched, normalised and stored in
   one call, is what would make this the OCS write path rather than a sandbox
   one.
2. **Retention.** `expire_snapshots` and garbage collection constrained to keep
   every published snapshot reachable, and a pruning policy for vector version
   directories. Both bound how far a rollback can go, so the policy has to be
   written down before it is automated.
3. **Serving Zarr and Icechunk for remote stores.** Decide between a redirect
   with a presigned URL, proxying the bytes, and pointing clients at
   `icechunk.http_storage()` against a read-only endpoint. OCS's
   `serve_icechunk_file` is a `FileResponse` passthrough and does not port. The
   STAC `icechunk` asset hands out the repository URI the record's storage key
   resolves to today, which is the right answer only for a client that can reach
   the object store itself.
4. **A derived catalog index for large listings.** One object per dataset is
   right for writes and wrong for listing thousands; an index needs a builder,
   an invalidation rule and etag-guarded listing so a stale page is detectable.
   `GET /stac/collections` has the same shape of problem: it describes one store
   or one Parquet file per collection, so it needs paging before it needs a cache.
5. **Pyramids.** Multiscale groups for large grids, mirroring what OCS already
   does, so a coarse read does not pay for the full resolution. That is also
   what would make the Zarr media type worth deriving per store rather than
   pinning it, as OCS does with its `profile=multiscales` variant.
6. **A tombstone is a one-way step for a bucket.** A record marked
   `lifecycle: deleted` is a value a binary that predates tombstones cannot
   validate, so it fails the listing that reads it rather than skipping it. One
   bucket must therefore not be served by two binaries that disagree about the
   state, which makes the rollout ordered rather than free: every reader moves
   before the first deleter does. Nothing in the layout detects the mistake
   today, and a `schema_version` the reader checks is the candidate guard.
7. **Tombstones are never purged, and they keep the whole record.** One small
   JSON object stays behind per identifier ever deleted, and it is the dead
   record entire: title, attribution, envelope and the rest of the descriptive
   metadata, although only the lifecycle and the revision are ever read from it.
   For a dataset deleted because it may not be kept that is the wrong default,
   and a purge is not the answer, because purging is exactly the unconditional
   record delete tombstones removed. Blanking everything but the identifier, the
   item type and the lifecycle is, and it needs a decision about what a tombstone
   is for: an audit trail of what an identifier used to hold, or nothing but a
   revision to swap against.
8. **OCS migration steps.** Map the work onto CLIM-555, CLIM-880, CLIM-1067 and
   CLIM-1068, starting from the call-site mapping table in
   [the unified model](research/unified-model.md#mapping-back-to-ocs). Upgrading
   OCS from icechunk 2.0.5 to 2.2 is a prerequisite rather than a follow-up.
