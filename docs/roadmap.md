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

## Next

1. **Real-data ingestion through the same API.** Drive an OCS dataset plugin's
   `fetch_period` into `RasterRepository.append`, and local GeoJSON or
   GeoParquet into the vector store, so the model is exercised by real files
   rather than by synthetic cubes and sample features.
2. **Retention.** `expire_snapshots` and garbage collection constrained to keep
   every published snapshot reachable, and a pruning policy for vector version
   directories. Both bound how far a rollback can go, so the policy has to be
   written down before it is automated.
3. **Serving Zarr and Icechunk for remote stores.** Decide between a redirect
   with a presigned URL, proxying the bytes, and pointing clients at
   `icechunk.http_storage()` against a read-only endpoint. OCS's
   `serve_icechunk_file` is a `FileResponse` passthrough and does not port. The
   STAC `icechunk` asset hands out the record's address URI today, which is the
   right answer only for a client that can reach the object store itself.
4. **A derived catalog index for large listings.** One object per dataset is
   right for writes and wrong for listing thousands; an index needs a builder,
   an invalidation rule and etag-guarded listing so a stale page is detectable.
   `GET /stac/collections` has the same shape of problem: it describes one store
   or one Parquet file per collection, so it needs paging before it needs a cache.
5. **Pyramids.** Multiscale groups for large grids, mirroring what OCS already
   does, so a coarse read does not pay for the full resolution. That is also
   what would make the Zarr media type worth deriving per store rather than
   pinning it, as OCS does with its `profile=multiscales` variant.
6. **OCS migration steps.** Map the work onto CLIM-555, CLIM-880, CLIM-1067 and
   CLIM-1068, starting from the call-site mapping table in
   [the unified model](research/unified-model.md#mapping-back-to-ocs). Upgrading
   OCS from icechunk 2.0.5 to 2.2 is a prerequisite rather than a follow-up.
