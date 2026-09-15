# One model for raster and vector

One address type, one backend protocol, one catalogue, and one tagged union of
dataset records covers raster and vector on filesystem, memory and S3. The
design has exactly two protocols — `StorageBackend` and `Catalog` — and
everything else is a concrete class, because those two are the only seams where
a second implementation is actually expected.

## Addresses

A `StorageAddress` is a URI: `file:///var/lib/ocs`, `memory://ocs`,
`s3://bucket/ocs`. Never a bare `Path`. It is frozen, carries a scheme, a root
and a key, and validates the key: no empty segments, no `.` or `..`, no
backslashes. It also rejects any URI carrying userinfo, so a credential can
never be smuggled into a stored record. Credentials live in settings as
`SecretStr` and nowhere else.

This is the direct answer to `ingestions/artifact_paths.py`, whose `to_absolute`
treats `s3://bucket/key` as a relative path and rejoins it under the data root.
A URI that round-trips through a record is a URI when it comes back.

## Backend and catalogue

```python
class StorageBackend(Protocol):
    def icechunk_storage(self, address: StorageAddress) -> icechunk.Storage: ...
    def object_store(self) -> obstore.store.ObjectStore: ...
    def parquet_filesystem(self) -> pyarrow.fs.FileSystem | None: ...
    def describe(self) -> BackendDescription: ...
```

Three handles from one settings block, guaranteed to address the same bytes.
`describe()` never returns a secret.

```python
class Catalog(Protocol):
    def put(self, dataset: Dataset) -> None: ...
    def get(self, dataset_id: str) -> Dataset | None: ...
    def list(self, item_type: ItemType | None = None) -> list[Dataset]: ...
    def delete(self, dataset_id: str) -> None: ...
```

One JSON object per dataset, written through obstore with etag compare-and-swap
when the record was read first. A record is what makes a dataset exist: there
is no filesystem discovery anywhere, so a store whose record was never written
is not a half-registered dataset, it is bytes. That replaces both
`records.json` under a portalocker lock and the directory scanning that
single-file index implies.

## Datasets

`Dataset` is a discriminated union on `item_type`, taking the vocabulary from
OGC API Features:

| `item_type` | Record | Format |
| --- | --- | --- |
| `coverage` | `CoverageDataset` (grid, variables, temporal extent) | Icechunk |
| `feature` | `FeatureDataset` (crs, feature detail) | GeoParquet |

`Annotated[CoverageDataset | FeatureDataset, Field(discriminator="item_type")]`
gives pydantic a tagged union and gives call sites an exhaustive `match`. That
is the replacement for the 16 scattered `ArtifactFormat.ICECHUNK` comparisons
in OCS: a new item type breaks every `match` at type-check time instead of
falling through 16 `if`s at runtime.

## Why the engines are not protocols

`RasterRepository` and `VectorCollectionStore` are concrete classes over a
backend. There will not be a second raster repository — the variation is in
where the bytes are, and the backend already absorbs it. Making them protocols
would add indirection with no second implementation behind it, and would push
the union's exhaustiveness out of reach of the type checker.

## Guards

Read paths refuse loudly rather than degrading: an unqualified feature read
above `max_unqualified_feature_count`, or a raster window above
`max_query_cell_count`, raises an error that names the threshold and the
observed value. A truncated answer that looks complete is worse than an error
that says what to narrow. The thresholds themselves are placeholders.

## Ordering

Commit the bytes, then write the record. Delete the record, then delete the
bytes. Both orders prefer orphan bytes to a dangling record: orphan bytes cost
storage and are found by a prefix listing, a dangling record is a dataset that
appears in every listing and fails on open.

## Key layout

```
catalog/datasets/{id}.json
raster/{id}/
vector/{id}/current.json
vector/{id}/versions/v00001/data.parquet
```

Flat, prefix-addressable, no directory semantics assumed.

## Mapping back to OCS

| OCS today | Replacement |
| --- | --- |
| `streaming/store.py:33` | `backend.icechunk_storage(address)` + `Repository.open_or_create` |
| `data_accessor/services/accessor.py:170` | `RasterRepository.read()` context manager |
| `data_manager/services/downloader.py:293` | repository handle from the backend |
| `data_manager/services/downloader.py:414` | `RasterRepository.create(overwrite=)` |
| `ingestions/services.py:1208` | `RasterRepository.read(version="published")` |
| `stac/media_types.py:107` | `RasterRepository.root_attributes()` |
| `_swap_store` at `ingestions/services.py:897` | `reset_branch("published", ..., from_snapshot_id=)` |
| `recover_interrupted_swap` | deleted; nothing to recover |
| `ArtifactRecord.path` + `artifact_paths.to_absolute` | `StorageAddress` URI in the record |
| 16 `ArtifactFormat.ICECHUNK` comparisons | one exhaustive `match` on `item_type` |
| `records.json` under portalocker | `ObjectCatalog`, one object per dataset, etag CAS |
| `gdf.to_parquet(path)` at `openeo/jobs.py:1585` | `VectorCollectionStore.write(...)` |

## Settled since

- The S3 backend is implemented and the whole suite runs on it against rustfs,
  not only a handful of backend tests. Conditional PUT works on both clients:
  obstore answers `AlreadyExistsError` and `PreconditionError`, Icechunk answers
  `ConflictError` on a stale `from_snapshot_id`, and nothing falls back to the
  non-atomic emulation, which is now reachable only on obstore's `LocalStore`.
- The compare-and-swap asymmetry between the catalogue record and the vector
  pointer is gone. Both go through one module, `storage/objects.py`, with the
  same create-if-absent and etag-replace pair and the same conflict mapping;
  the duplicated private helpers on `ObjectCatalog` and `VectorCollectionStore`
  were deleted. The remaining difference is inherent rather than structural: the
  raster side swaps an Icechunk branch and the vector side swaps a pointer
  object, and both are read-then-swap with a real compare-and-swap closing the
  window.

## Open questions

- moto is still unproven against Icechunk. rustfs showed no conformance gap, so
  nothing forced the question, and MinIO stayed unused as the fallback. moto's
  server mode remains the only variant that could work at all, and nobody has
  run Icechunk against it.
- Snapshot expiry bounds rollback depth. `expire_snapshots` must be constrained
  to keep every published snapshot reachable, and that policy is unwritten.
- The catalogue is one object per dataset. At what collection size a derived
  index is needed, and what maintains it, is not decided.
- Mixed geometry types in one collection are recorded but not yet reconciled
  with consumers that expect a single type.
- Serving `/zarr` and `/icechunk` for a remote store is unresolved: redirect
  with a presigned URL, proxy the bytes, or have clients use
  `icechunk.http_storage()` against a read-only endpoint. OCS's
  `serve_icechunk_file` is a `FileResponse` passthrough and does not port.
- Guard thresholds are placeholders pending real workloads.
- Upgrading OCS from icechunk 2.0.5 to 2.2 is a prerequisite, not a follow-up.
