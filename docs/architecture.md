# Architecture

Six layers, and only two of them are protocols. HTTP routers stay thin, the
service layer owns composition, the engines own domain behaviour, addresses and
keys own naming, backends own the three storage handles, and the libraries own
the bytes. A dataset that moves from the filesystem to S3 changes exactly one
layer.

## Layers

```mermaid
flowchart TD
    R["HTTP routers<br/>health, backends, datasets, raster, vector"]
    S["StorageService<br/>composition, exhaustive routing on item_type"]
    E["RasterRepository<br/>VectorCollectionStore<br/>ObjectCatalog"]
    A["StorageAddress and key layout<br/>file:/// memory:// s3://"]
    B["FilesystemBackend<br/>MemoryBackend<br/>S3Backend"]
    L["icechunk.Storage<br/>obstore ObjectStore<br/>pyarrow.fs.FileSystem"]

    R --> S
    S --> E
    E --> A
    A --> B
    B --> L
```

Routers parse and validate request parameters and return response models. They
raise no `HTTPException`; a single exception handler maps `StorageError`
subclasses to their `status_code`, so the status code lives with the error that
knows why it happened.

`StorageService` is a frozen dataclass holding the settings, the backend, the
catalogue and the two engines. It is built once in the lifespan and put on
`app.state`. Operations that span item types — deleting a dataset, listing
everything — route with an exhaustive `match` on `item_type`.

The engines are concrete. `RasterRepository` wraps an Icechunk repository;
`VectorCollectionStore` writes and reads versioned GeoParquet;
`ObjectCatalog` stores one JSON record per dataset through obstore. Only
`ObjectCatalog` satisfies a protocol, because a catalogue backed by a database
is a plausible second implementation and a second raster repository is not.

`StorageAddress` and the key module sit between the engines and the backend so
that no engine constructs a path. Every key a backend ever sees came from one
module that validates identifiers and rejects traversal.

Backends resolve one address into three handles. All three are complete: the
filesystem and memory backends, and the S3 backend, which builds the Icechunk
storage, the obstore store and the pyarrow filesystem from one settings block
and is exercised by the whole test suite against rustfs.
[Backends and key layout](concepts/backends-and-layout.md) has the handle table,
the settings-to-client translation, and the S3 key layout.

## Decisions

| Decision | Rationale |
| --- | --- |
| URI addressing, never a bare `Path` | A `Path` cannot represent `s3://bucket/key`. OCS's `artifact_paths.to_absolute` silently rejoins an `s3://` string under the data root. |
| No credentials in addresses or records | A record is written to disk and read by other processes. Credentials stay in settings as `SecretStr`. |
| One backend yields all three handles | Icechunk, obstore and pyarrow must address the same bytes. Building them separately makes a mismatch invisible. |
| obstore for object ops, `pyarrow.fs` for Parquet | There is no obstore-to-pyarrow adapter, and `obstore.fsspec` is best effort. |
| fsspec rejected | Icechunk does its own I/O through Arrow `object_store` and never sees an fsspec filesystem (CLIM-555). |
| Only `StorageBackend` and `Catalog` are protocols | Those are the seams with a real second implementation. Protocols elsewhere would cost exhaustiveness for no gain. |
| `item_type` discriminated union | Replaces 16 scattered `ArtifactFormat.ICECHUNK` comparisons with one exhaustive `match` the type checker enforces. |
| OGC API Features vocabulary | `coverage` and `feature` are already the terms the API surface will use. |
| Publish by pointer move | Branch reset with `from_snapshot_id` and etag-conditional pointer writes are atomic on S3; a directory rename is not (CLIM-880). |
| Rollback is publish with an older target | One code path, tested by the publish tests, with no recovery routine to maintain. See [versioning](concepts/versioning.md). |
| A record makes a dataset exist | No filesystem discovery, so a store without a record is bytes rather than a half-registered dataset. |
| Commit bytes, then write the record | Orphan bytes are found by a prefix listing; a dangling record lists everywhere and fails on open. |
| Guards refuse loudly with thresholds named | A truncated result that looks complete is worse than an error saying what to narrow. |
| `decode_coords="all"` on every read | Without it `spatial_ref` stays a data variable and the CRS is silently lost. |
| Explicit GeoParquet `schema_version="1.1.0"` | Otherwise the file declares 1.0.0 while carrying a 1.1 covering key. |
| Memory backend caches `Storage` per address | `in_memory_storage()` returns a new store per call; caching per instance keeps test isolation. |

## What the API does not do

No chunk serving and no byte-range proxying. Responses are JSON. Whether a
deployment serves `/zarr` and `/icechunk` for a remote store by presigned
redirect, by proxy, or by pointing clients at `icechunk.http_storage()` is an
open question recorded in [the unified model](research/unified-model.md), and
it is a serving decision rather than a storage one.
