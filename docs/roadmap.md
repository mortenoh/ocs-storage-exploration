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
advertised. It is a projection, not new state: the records already held the
bounding box, the temporal extent, the variables, the CRS and the feature
detail, and the only reads it adds are the store's root attributes and the
published Parquet footer. See [the STAC catalog](concepts/stac-catalog.md).

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
   `GET /stac/collections` has the same shape of problem: it reads one store and
   one Parquet footer per collection, so it needs paging before it needs a cache.
5. **Pyramids.** Multiscale groups for large grids, mirroring what OCS already
   does, so a coarse read does not pay for the full resolution. That is also
   what would make the Zarr media type worth deriving per store rather than
   pinning it, as OCS does with its `profile=multiscales` variant.
6. **OCS migration steps.** Map the work onto CLIM-555, CLIM-880, CLIM-1067 and
   CLIM-1068, starting from the call-site mapping table in
   [the unified model](research/unified-model.md#mapping-back-to-ocs). Upgrading
   OCS from icechunk 2.0.5 to 2.2 is a prerequisite rather than a follow-up.
