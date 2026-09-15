# Versioning

Both halves of the service are versioned, and both publish by moving a pointer,
but the mechanisms underneath are different because the formats are different.
Icechunk already is a version-controlled store; Parquet is an immutable file. The
API hides the difference behind one vocabulary: write, publish, roll back.

## Raster: one Icechunk commit per write

Every `POST /api/v1/raster/{id}` and `POST /api/v1/raster/{id}/append` opens a
writable session on the `main` branch, writes with `to_icechunk` and commits.
The commit identifier is the version, and it is what the write answers as
`snapshot_identifier`. Nothing else in the repository changes, so `main` is the
draft history: every write is visible there immediately, published or not.

`GET /api/v1/raster/{id}/versions` walks `Repository.ancestry(branch="main")`
newest first and marks the snapshot the published branch points at:

```json
{
  "snapshot_identifier": "8FMG55R5397CKBMH99D0",
  "message": "append",
  "written_at": "2026-09-15T17:15:18.166688Z",
  "is_published": true
}
```

Icechunk's `SnapshotInfo` also carries `parent_id` and a `metadata` mapping. The
endpoint does not surface them today: the list is already in ancestry order, so
the parent is the next entry. Anything that needs the real parent link reads the
repository directly, as shown in
[inspecting the data](../guides/inspecting-the-data.md).

Publication creates the `published` branch on first use and afterwards moves it
with a compare-and-swap against the snapshot it currently points at:

```python
repository.reset_branch(PUBLISHED_BRANCH, target, from_snapshot_id=previous)
```

If another writer published in between, the reset fails and the service answers
409 rather than overwriting the other pointer. Publishing the snapshot that is
already published is not an error: the result says `"changed": false`.

A rollback is the same call with an older snapshot, which is why there is no
rollback endpoint and no recovery routine. Readers that want one specific
version do not have to publish at all: `?snapshot_identifier=...` pins it, and
`?version=draft` reads `main` while `?version=published` follows the pointer.

Snapshots share chunks. An append writes only the chunks it adds, so keeping ten
versions of a coverage costs ten manifests plus the new data, not ten copies.
That is what makes rollback cheap enough to be the only recovery mechanism.

The bound on that is expiry. `Repository.expire_snapshots(older_than=...)`
drops snapshots from the history and
`Repository.garbage_collect(delete_object_older_than=...)` then deletes the
objects no reachable snapshot needs. Neither is called by this service. When a
deployment starts calling them, retention has to keep the published snapshot and
however far back a rollback is expected to reach, because an expired snapshot is
not a version that can be published again. Choosing that retention window is an
open item, recorded in
[the unified model](../research/unified-model.md).

## Vector: one Parquet file per version

A Parquet file has no history, so the version is the file. Each write lands in
its own directory and no write ever replaces an earlier one:

```text
ocs/vector/{dataset_identifier}/versions/v00001/data.parquet
ocs/vector/{dataset_identifier}/versions/v00002/data.parquet
ocs/vector/{dataset_identifier}/current.json
```

The version number is zero-padded to five digits so that a lexicographic object
listing, which is the only ordering an object store guarantees, is also numeric
order. The next version is one past the highest directory found, so the listing
is the history.

`current.json` is the published pointer. It names the version, the key of its
data file, the feature count and when it was published:

```json
{"version":1,"key":"ocs/vector/districts-demo/versions/v00001/data.parquet",
 "feature_count":3,"published_at":"2026-09-15T17:15:18.434733Z"}
```

Publication writes that object under a compare-and-swap: the first write uses
obstore's `mode="create"`, which fails if a concurrent publication got there
first, and every later write uses `mode={"e_tag": ...}` with the etag last read.
On a real object store that is atomic. On a local directory it is not: obstore's
`LocalStore` answers a conditional put with

```text
NotImplementedError: Operation `put_opts` with mode `PutMode::Update`
not yet implemented by LocalFileSystem(...)
```

so the filesystem backend falls back to reading the etag back and comparing it
before writing. That check narrows the race but does not remove it, which is
acceptable for a local development backend and is exactly the reason the S3
backend is the one that matters for concurrent publication.

A rollback is publish with an older version, the same as raster. Reads take
`?version=N` to pin one, and default to the published version, falling back to
the newest written one when nothing is published yet.

Old versions are never collected. There is no expiry pass for vector data in
this pass: deleting a collection deletes every version below its prefix, and
anything finer is a retention policy that does not exist yet.

## Side by side

| | Raster (Icechunk) | Vector (GeoParquet) |
| --- | --- | --- |
| Unit of a version | A commit on `main`, named by snapshot identifier | A directory `versions/vNNNNN` holding one Parquet file |
| Where history lives | Inside the repository: ancestry of `main` | In the object listing of the versions prefix |
| Publish mechanism | `reset_branch("published", target, from_snapshot_id=previous)` | `current.json` written with etag compare-and-swap |
| Rollback | Publish an older snapshot | Publish an older version |
| Pinning on read | `?snapshot_identifier=...`, or `?version=draft` for `main` | `?version=N` |
| Storage cost of a version | Only the new chunks; snapshots share the rest | A full copy of the collection |
| Garbage collection | `expire_snapshots` then `garbage_collect`, not called here | None; versions stay until the dataset is deleted |

## What the API answers

Both engines answer a publication with the same model, so a client that moves a
pointer does not care which kind of dataset it moved. The fields that do not
apply to the item type are null:

```json
{
  "dataset_identifier": "districts-demo",
  "item_type": "feature",
  "published": true,
  "changed": true,
  "snapshot_identifier": null,
  "version": 2,
  "previous_snapshot_identifier": null,
  "previous_version": 1
}
```

A coverage fills in `snapshot_identifier` and `previous_snapshot_identifier` and
leaves the version fields null. Naming the wrong selector when publishing (a
version for a coverage, a snapshot for a collection) is refused with 422 rather
than quietly ignored, and naming both is refused by the request model.

The catalog record carries the same pointer state under `publication`, so a
listing shows what is published without opening any repository. See the
[API walkthrough](../guides/api-walkthrough.md) for the full sequence and
[publication without a rename](../research/publication-without-rename.md) for
why a pointer move rather than a directory swap.
