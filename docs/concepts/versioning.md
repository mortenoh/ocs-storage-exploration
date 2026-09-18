# Versioning

Both halves of the service are versioned, and both publish by moving a pointer,
but the mechanisms underneath are different because the formats are different.
Icechunk already is a version-controlled store; Parquet is an immutable file. The
API hides the difference behind one vocabulary: write, publish, roll back.

## Raster: one Icechunk commit per write

Every `POST /api/v1/raster/{id}` and `POST /api/v1/raster/{id}/append` writes
with `to_icechunk` and commits. The commit identifier is the version, and it is
what the write answers as `snapshot_identifier`. Nothing else in the repository
changes, so `main` is the draft history: every accepted write is visible there
immediately, published or not.

### A write is staged before it becomes the draft

The commit does not land on `main` directly. Writing there would mean that a
write whose catalog record is refused has already changed what every reader of
the draft sees, and two writers racing one identifier would leave the loser's
cube in the store under the winner's record. A write therefore runs on a scratch
branch of its own:

```python
branch = f"write-{uuid4().hex}"
repository.create_branch(branch, previous)          # previous is the tip main had
session = repository.writable_session(branch)
...                                                  # to_icechunk, then commit
snapshot = session.commit(message)
catalog.put(record, create=True)                     # or put(record, revision=...)
repository.reset_branch(MAIN_BRANCH, snapshot, from_snapshot_id=previous)
```

`RasterRepository._staged_commit` is that sequence, and the scratch branch is
deleted in a `finally` either way, so a failed write leaves no branch behind.

The catalog claim in the middle is the arbiter. A create claims the record with
obstore's create mode and an overwrite or an append claims it with a
compare-and-swap against the revision it read, so exactly one of two racing
writers gets past it. The loser raises before `main` has moved: its cube sits on
a scratch branch that is deleted immediately and is reachable from nothing.

Only after the claim succeeds does `main` move, and it moves as a fast-forward
under its own compare-and-swap: `from_snapshot_id=previous` is the tip the write
was staged on, so a commit that landed on `main` in between is not overwritten.
That reset is the one Icechunk conflict a caller can still see, and it is
reported as 409 rather than escaping as a 500. When it happens the record the
write claimed is already one write ahead of the store, so an overwrite or an
append puts the record it replaced back before it raises, and only if what
stands is still the record it just wrote. A crash between the two steps leaves
the disagreement for the next reconciliation to repair.

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

Only a snapshot that holds data can be published. The snapshot a repository is
initialised with holds none, so naming it is refused with 409 rather than
publishing an empty coverage over a good one.

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

### The branch is the publication truth

The branch, not the catalog record, decides what is published. A publication
moves the branch first and writes the record afterwards, so a crash between the
two leaves a record that still says "draft" while the branch already points at
the published snapshot. Readers therefore never consult the record:
`?version=published` opens the `published` branch when the repository has one,
and answers 404 `"dataset has no published version"` when it does not. Falling
back to `main` there would serve the newest draft as if it had been published,
which is exactly the failure a half-finished publication would otherwise cause.

The record is a cache of that truth, kept so a listing can show what is
published without opening any repository. `RasterRepository.reconcile_publication`
rewrites its `publication` block from the branch tip, and both `publish()` and
`versions()` call it first, so the next call after an interrupted publication
repairs the record.

The same call also rewrites everything the store holds. `timestep_count`,
`variables`, `temporal`, the grid with its `bbox`, the title, the licence and the
attribution are read back from `describe(version=draft)` and written to the
record whenever they disagree, because a write that claimed the record and then
lost the fast-forward onto `main` leaves exactly that disagreement. The bbox is
compared to within a millionth of a cell: cell centres are written as floats and
measured back as a median step, so a record that already matches is left alone
rather than rewritten on every call. The title travels the way the terms do: a
create writes it into the commit metadata, an append carries forward the metadata
of the snapshot it extends, and a read of that metadata is what puts the title of
the accepted write back over the one a rejected overwrite left in the record. A
snapshot committed before titles travelled with them carries none, and
reconciliation then leaves the title of the record alone rather than blanking it.
A repository that holds only its initialisation snapshot has nothing to read back
at all, so the extents of its record are left alone rather than blanked.

### An append must extend the time axis

An append is refused unless its timestamps strictly increase and its first one
is later than every timestamp already committed. The check reads the snapshot
the append is staged on, not whatever `main` points at when it runs, so a
concurrent write cannot slip between the check and the commit.

The reason is that Zarr has no notion of an ordered dimension: appending an
earlier block simply concatenates it, and the time axis is then unsorted. A
label window over an unsorted axis is not an error message, it is wrong data. It
returns the wrong timesteps for bounds the axis happens to hold and raises
`KeyError` for every other bound. Refusing the append is the only point where
that is cheap to prevent, so the contract lives there.

Queries do not rely on it. `_apply_time_window` selects with a boolean mask and
`isel`, the same way `_apply_spatial_window` handles a longitude axis wrapped
across the antimeridian, so a store written before this guard existed still
answers correctly, and an empty window falls to the existing size guard rather
than to a `KeyError`.

The axis itself is nanosecond `datetime64`, which reaches from 1677-09-21 to
2262-04-11. A timestamp outside that is refused with 422, both at a write and
when an ingest plan is built, rather than wrapping silently into another
century; a query bound outside it is clamped onto the end it ran past, because a
window ending in the year 3000 still names every timestep the store holds. A
coarser resolution was ruled out: xarray decodes a time coordinate back onto
nanoseconds whatever unit it was written with, so a coordinate outside the span
could not be read back either.

### Metadata comes from the store, not from the record

A query and a description read their grid from the snapshot they opened:
dimension names and data types from the variables, the projection from
`proj:code` and the `spatial_ref` grid mapping coordinate, the envelope and the
cell sizes from the coordinates themselves, and the time extent from the time
coordinate. The record is used only to find the repository and to answer
listings. That is what keeps a draft that rewrites the grid, the projection or
the bounds from changing what a published query reports.

`RasterRepository.describe(dataset_identifier, version=..., snapshot_identifier=...)`
returns that reading as a `RasterStoreDescription`, so anything that projects a
coverage, the STAC collection included, can describe the snapshot it actually
serves.

The fill value is part of that reading. `apply_geozarr_attributes` stamps the
`nodata` attribute the grid declares onto every data variable as it is written,
unless the variable already carries one, so a query excludes the fill cells of
the snapshot it opened. Nothing falls back to the record: a draft that declares
zero as its fill value would otherwise make a published query over genuine zeros
report no statistics at all.

The coordinates that reading depends on are reserved at write time.
`spatial_ref` and the grid's own `t`, `y` and `x` names are refused as data
variable names with 422, by a create, an append and an ingest alike: assigning
the coordinate replaces a variable of the same name, so the coverage would be
written, and reported as created, with that variable's data silently gone.

## Vector: one Parquet file per version

A Parquet file has no history, so the version is the file. Each write lands in
its own directory and no write ever replaces an earlier one:

```text
ocs/vector/{dataset_identifier}/{generation}/versions/v00001/reservation.json
ocs/vector/{dataset_identifier}/{generation}/versions/v00001/data.parquet
ocs/vector/{dataset_identifier}/{generation}/versions/v00001/metadata.json
ocs/vector/{dataset_identifier}/{generation}/versions/v00002/...
ocs/vector/{dataset_identifier}/{generation}/current.json
```

`{generation}` is a uuid4 in hex, minted when the collection is created. Writing
a collection that has a record stays in the generation that record names; only a
first write mints one. Both engines work this way: a coverage keeps its Icechunk
repository at `ocs/raster/{dataset_identifier}/{generation}`, an overwrite stays
in the generation of the record it read, and only a create without a record
mints a new one. The record's `storage_key` holds exactly that prefix, so the
record is the only thing that says where the live data of a dataset is, and a
dataset deleted and written again under the same identifier never shares a
prefix with the one it replaced. See "Deleting a dataset marks the record before
it sweeps" below for what that buys.

The version number is zero-padded to five digits so that a lexicographic object
listing, which is the only ordering an object store guarantees, is also numeric
order.

### The reservation is what makes a version number unique

Choosing "one past the highest directory found" and then writing the Parquet is
not safe: two writers that list at the same moment pick the same number, and the
second overwrites a file the first may already have published. A version number
is therefore claimed before anything is written, by creating

```text
versions/vNNNNN/reservation.json
```

with obstore's `mode="create"`. That mode is implemented by every store this
service uses, obstore's `LocalStore` included, so the claim is atomic on all
three backends: exactly one writer gets the object and the loser sees
`AlreadyExistsError` and tries the next number, up to a bounded number of
attempts. A candidate is also rejected when its `data.parquet` already exists,
so bytes left behind by a crashed write are never overwritten either.

An abandoned reservation burns its number and nothing else. Version numbers are
labels, not a count, so a gap in the sequence costs nothing.

### The metadata sidecar is what makes a version complete

`versions/vNNNNN/metadata.json` is written after the Parquet, as a create, and
it is what marks the version as finished:

```json
{"version":1,"crs":"EPSG:4326","feature_count":12,"identifier_property":"id",
 "primary_geometry":"geometry","geometry_types":["Point","Polygon"],
 "selectable_columns":["level","path"],"bbox":{"minimum_x":0.0,"minimum_y":0.0,
 "maximum_x":21.5,"maximum_y":21.5},"written_at":"2026-09-15T17:15:18.434733Z",
 "license":"CC-BY-4.0","attribution":"Statistics Norway"}
```

`versions()` lists only the directories that have one, so a version whose write
never finished is never published, never read and never resolved to.

The sidecar also fixes what a read is allowed to believe. The catalog record
carries the metadata of the **latest** write, so reading the coordinate
reference system, the feature count, the identifier property or the selectable
columns from it means describing version 2 while serving version 1: a draft that
reprojects the collection makes a published bbox query return nothing, and a
draft with fewer features lets an unqualified read past the feature-count guard.
`read()`, `publish()` and the pointer therefore resolve every one of those
fields from the sidecar of the version they selected. `version_metadata(id, n)`
and `published_metadata(id)` expose the same reading to anything that projects a
collection.

The record keeps the latest-write metadata for listings, plus the `publication`
block, and nothing else depends on it.

`current.json` is the published pointer. It names the version, the key of its
data file, the feature count and when it was published:

```json
{"version":1,"key":"ocs/vector/districts-demo/{generation}/versions/v00001/data.parquet",
 "feature_count":3,"published_at":"2026-09-15T17:15:18.434733Z"}
```

Publication writes that object under a compare-and-swap: the first write uses
obstore's `mode="create"`, which fails if a concurrent publication got there
first, and every later write uses `mode={"e_tag": ...}` with the etag last read.
Create mode is atomic everywhere, the local directory included. The etag replace
is not: obstore's `LocalStore` answers a conditional put with

```text
NotImplementedError: Operation `put_opts` with mode `PutMode::Update`
not yet implemented by LocalFileSystem(...)
```

so the filesystem backend falls back to reading the etag back and comparing it
before writing. That check narrows the race but does not remove it, which is
acceptable for a local development backend and is exactly the reason the S3
backend is the one that matters for moving a pointer concurrently. Claiming a
version number does not share that weakness, because it only ever uses create
mode.

A rollback is publish with an older version, the same as raster. Reads take
`?version=N` to pin one, and default to the published version, falling back to
the newest written one when nothing is published yet.

Old versions are never collected. There is no expiry pass for vector data in
this pass: deleting a collection deletes every version below its prefix, and
anything finer is a retention policy that does not exist yet.

## Deleting a dataset marks the record before it sweeps

Deletion used to remove the catalog record first and then empty the prefix. That
order has a window: between the two, the dataset looks like it never existed, so
a writer using the same identifier starts writing into a prefix that is about to
be swept, and the sweep then erases what it wrote.

The record now goes last, and it is never removed. A delete reads the record raw,
marks it `lifecycle: "deleting"` under a compare-and-swap, sweeps every object
below the `storage_key` of the record it marked - and nothing else - and then
swaps that mark for a tombstone: the same record under `lifecycle: "deleted"`,
written under the revision of the mark it holds. A record marked `deleting` is a
deletion in progress and a tombstone is a dataset that is gone; neither is a
dataset. Listings skip both, and reads, publications, `DELETE`, the datasets API
and the STAC projection answer 404 for both - a tombstone exactly as for an
identifier nothing was ever written under. Deleting and writing read the record
raw, because they are the two calls that can finish a deletion.

That makes the state after a crash recoverable rather than ambiguous. A delete
that stops after the mark leaves a marked record and some objects behind, and
either a second delete or a write of the same identifier finishes the sweep
before doing its own work. A write that takes a deletion over swaps the mark
straight for its own record rather than tombstoning it first, so two writers
taking over the same deletion meet at that one compare-and-swap and exactly one
of them wins; the loser is told the dataset already exists, 409, and sweeps the
generation it minted.

The commoner case, though, is a deletion that has not crashed at all and is
simply still running. It finishes while the write that took it over is putting
its bytes down, so by the time that write claims the record the mark it read has
already become a tombstone. That is not losing the identifier, and it is covered
in "Writing over a tombstone" below.

Nothing is swept without that reservation. A mark that lost its
compare-and-swap to a writer used to sweep anyway, emptying the prefix a record
that was still live pointed at. It is now retried against the record that
writer left behind, `MAXIMUM_DELETION_ATTEMPTS` (four) times before it gives
up. A retry that finds the record already marked finishes that deletion
instead. One that finds no record, a tombstone, or a record in a generation this
call never read - because another deleter finished this deletion and the
identifier was written again - sweeps nothing and returns: what stands under the
identifier now belongs to whoever made it, not to this call. A delete that loses
every attempt is refused with `PublicationConflictError`, 409, and removes
nothing.

### Writing over a tombstone

A tombstone is not an obstacle. It is a free identifier that happens to carry a
revision, and a writer that finds one replaces it with its own record under a
compare-and-swap against that revision. The write is a first write in every other
respect: a generation of its own, a fresh `created_at`, no title, terms or
publication block inherited from the dataset that is gone, and, for a coverage,
no `overwrite` flag needed. The tombstone check runs before the item type check,
so a coverage may be created over the tombstone of a vector collection and a
collection written over the tombstone of a coverage: what an identifier used to
hold says nothing about what it may hold next.

Which dead record a write swaps away is decided when it claims, not when it
starts. Writing the bytes takes as long as it takes, and the record moves while
it happens: the deletion the write took over finishes and tombstones the mark, or
a deleter reserves the record again. Losing the compare-and-swap to one of those
means nothing owns the identifier, so the claim is read again and retried against
whatever the record has become, and the generation the write already filled is
kept rather than swept and written a second time. It is safe to keep precisely
because no record names that generation yet and nobody else knows its token, so
nothing a competing actor does can have touched it. A retry that finds a
`deleting` mark empties the generation *that mark* names before replacing it,
exactly as a takeover does, and never its own. Only a live record means another
writer really took the name, which is the 409 `DatasetAlreadyExistsError` a
create has always answered, with the generation this write minted swept behind
it. `MAXIMUM_CLAIM_ATTEMPTS` (four) bounds the retries; an identifier that
changed hands under every one of them is refused with `PublicationConflictError`,
409, and swept the same way. Two writers retrying against the same tombstone
still meet at one compare-and-swap, so exactly one of them wins the name.

### Every record transition is a compare-and-swap

obstore exposes no conditional delete on any of the three backends -
`obstore.delete(store, paths)` takes no etag and no mode - so for as long as a
deletion removed its record, dropping it had to be a read followed by a delete
rather than one compare-and-swap. Between that read and that delete, another
actor could finish the same deletion, drop the record, and leave a writer free to
create the dataset again; the delete then removed the new record. No bytes were
lost, because the sweep only ever emptied the deleter's own generation, but the
dataset answered 404 after a write that had succeeded, and its objects were left
unreferenced under a prefix nothing named.

Not deleting the record at all is what closes it. Every transition a record makes
- live to `deleting`, `deleting` to `deleted`, and `deleting` or `deleted` back to
live - is a `put` under the revision the caller read, so exactly one of two racing
callers lands each one. A deleter that loses its tombstone leaves the record
alone, because whoever moved it on owns what stands under the name now, and a
writer that loses its claim to a record that is still dead reads it again and
claims that one instead. The `Catalog.delete` that used to remove a record went
with the window: nothing in the service removes a catalog record any more.

Generations closed a second version of the same failure, and still do. The sweep
is a listing followed by a delete, so a writer that took the deletion over while
the original deleter was between those two steps had its new objects removed by
that deleter's sweep. A dataset written again lives under a generation the dead
record does not name, so a deleter that resumes late finds its own generation
already empty and never reaches the new one, however long it was away. A writer
that then loses its claim sweeps the generation it minted itself, which nobody
else knows the token of, so a loser leaves no orphan behind either.

What it costs is a record that outlives its dataset: one small JSON object per
identifier ever deleted, read and skipped by every listing. There is deliberately
no purge operation, because purging a tombstone is precisely the unconditional
record delete this design removes. It would be a read of the tombstone followed
by a delete, with the same window between them, and the prize for losing that
race is the old failure back again: a dataset written in between answering 404
over objects that are perfectly intact. A deployment with enough tombstones to
care would need a conditional delete first, and with one it would not need the
tombstones.

## Side by side

| | Raster (Icechunk) | Vector (GeoParquet) |
| --- | --- | --- |
| Unit of a version | A commit on `main`, named by snapshot identifier | A directory `versions/vNNNNN` holding one Parquet file |
| Where history lives | Inside the repository: ancestry of `main` | In the object listing of the versions prefix |
| How a version number is claimed | The commit identifier is assigned by Icechunk | `reservation.json` created with `mode="create"` before the write |
| Where a write lands first | A scratch branch `write-{uuid}`, fast-forwarded onto `main` after the record is claimed | Its own `versions/vNNNNN` directory, which no other write can name |
| When a version counts as written | The commit returns | `metadata.json` exists next to the Parquet |
| Where a reader takes its metadata | The snapshot it opened | The `metadata.json` of the version it selected |
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

The catalog record carries the same pointer state under `publication`. For a
coverage it is a cache of the branch, reconciled on the next `publish` or
`versions` call, so a listing can show what is published without opening any
repository while the branch stays the authority. See the
[API walkthrough](../guides/api-walkthrough.md) for the full sequence and
[publication without a rename](../research/publication-without-rename.md) for
why a pointer move rather than a directory swap.
