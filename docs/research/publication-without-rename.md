# Publication without a rename

A pointer move beats a directory rename on all five criteria that matter, and
it is the only one of the three options that works on S3 at all. Raster
publishes by resetting a branch; vector publishes by overwriting a small pointer
object under an etag precondition. Rollback is the same code path as publish in
both cases, so there is no recovery routine to write and none to test.

## The three options

| Criterion | Rename swap | Branch reset | Pointer object |
| --- | --- | --- | --- |
| Window where the dataset is absent | two metadata operations wide | none | none |
| In-flight reader survives | no | yes | yes, for a reader already holding bytes |
| Atomic on S3 | no such operation | yes | yes |
| Rollback uses the same code path | no, a separate restore | yes | yes |
| Concurrent publish detected | no | yes, compare-and-swap | yes, etag precondition |

The rename row is not a criticism of the OCS implementation. `_swap_store`
(`ingestions/services.py:897`) states both of its caveats in its own docstring
and points at CLIM-880 as the durable answer. The point is that the window and
the recovery routine are properties of publishing by rename, not defects that
better code removes.

## Raster: a `published` branch

Ingest writes commits to `main`. Publication moves a second branch,
`published`, to the snapshot that should be visible.

```python
target = snapshot_id or repository.lookup_branch("main")
current = repository.lookup_branch("published")   # None when never published
if current is None:
    repository.create_branch("published", target)
else:
    repository.reset_branch("published", target, from_snapshot_id=current)
```

`from_snapshot_id` makes the move a compare-and-swap: if another publish landed
between the read and the write, the reset fails and the caller gets a conflict
rather than silently overwriting someone else's publication.

Before the move, the target snapshot is checked against `repository.ancestry`
to confirm it belongs to this repository's history. Publishing an unrelated
snapshot id should be an error that names the id, not a pointer into nothing.

Readers open `repository.readonly_session("published")`. A readonly session
pins its snapshot when it is opened, so a reader that started before a publish
finishes reading the version it started with. Nothing disappears underneath it,
and nothing needs to be retained on the side for it.

Rollback is `publish(snapshot_id=<older>)`. It is not a distinct operation: the
same compare-and-swap, the same ancestry check, the same conflict behaviour.
`versions()` reads `ancestry` and marks which snapshot the `published` branch
currently names, so a caller can pick a target.

Tags cannot do this job. A tag is immutable and its name cannot be recreated
after deletion, so a pointer that has to move must be a branch.

## Vector: an immutable version plus a pointer

GeoParquet has no equivalent of a commit graph, so versioning is explicit.
Each write lands at a new, immutable key:

```
vector/{id}/versions/v00001/data.parquet
vector/{id}/versions/v00002/data.parquet
vector/{id}/current.json
```

`current.json` is a small pointer object naming the published version. It is
written with obstore's conditional put: `mode="create"` for the first publish,
`mode={"e_tag": <etag read before>}` for every later one. A stale etag raises
`PreconditionError`, which surfaces as a publication conflict.

A reader resolves `current.json`, then reads that version's Parquet file. The
window between resolving and reading is not a correctness problem because
version objects are immutable and are never overwritten — the file named by a
pointer a reader has already read stays exactly as it was.

Rollback is `publish(version=<older>)`. Same call, same precondition.

## What is deleted rather than ported

`recover_interrupted_swap` (`ingestions/services.py:853`), `_retired_path`,
`_finalize_store_swap`, `_rollback_store_swap`, the `.retired` and `.failed`
directory states, and the stale `ocs-ingest-rollback-{uuid}` branch cleanup
that recovery performs. None of these are ported. They exist to repair a
half-finished rename, and there is no half-finished state to repair when
publication is a single conditional write. A kill at any point either leaves
the pointer where it was or leaves it where it was moved to.

This feeds CLIM-880 directly: the ticket asks for atomic publication, and the
answer is that it is atomic because it is one object write, not because the
rename was made cleverer.

## Retention caveat

Rollback depth is bounded by snapshot retention, and OCS currently expires
aggressively. `streaming/orchestrator.py:347` calls
`expire_snapshots(older_than=datetime.now(tz=timezone.utc))` after every
streaming ingest, keeping only whatever `main` points at. Under a `published`
branch that call has to be constrained so that every snapshot reachable from
`published`, and as much prior history as the rollback policy promises,
survives expiry. Getting this wrong produces a rollback target that lists in
`versions()` and fails on open.

The equivalent on the vector side is a retention policy over
`vector/{id}/versions/`: old versions can be deleted, but only versions older
than the deepest rollback the service promises, and never the version
`current.json` names.

How deep that promise should go is not decided. It is a policy question about
what an operator needs to undo, not a technical constraint of either mechanism.
