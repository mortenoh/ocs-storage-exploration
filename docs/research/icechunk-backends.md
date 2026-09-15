# Icechunk storage backends

Every Icechunk storage constructor returns the same concrete type,
`icechunk.Storage`. There is no backend protocol to implement and no subclass
to write: a function that returns `icechunk.Storage` is the entire seam, and
everything above it — repositories, sessions, branches, xarray — is backend
agnostic. That single fact is what makes a storage abstraction for OCS small.

## The constructors

| Constructor | Returns | Notes |
| --- | --- | --- |
| `local_filesystem_storage(path)` | `Storage` | what all six OCS call sites use |
| `in_memory_storage()` | `Storage` | per-instance; two calls are two stores |
| `s3_storage(...)` | `Storage` | native AWS SDK path |
| `s3_object_store_storage(...)` | `Storage` | Arrow `object_store` path |
| `http_storage(...)` | `Storage` | read-only over HTTP |

`s3_storage` takes `bucket`, `prefix`, `region`, `endpoint_url`, `allow_http`,
`access_key_id`, `secret_access_key`, `session_token`, `anonymous`, `from_env`
and `force_path_style`. Two of those matter for local testing:
`endpoint_url` plus `allow_http=True` point at rustfs, and `force_path_style=True`
avoids virtual-hosted bucket DNS that a local endpoint does not have.

From Icechunk 2.1 onwards the `prefix` must be non-empty. A repository at the
bucket root is not addressable, so the key layout has to start with a prefix
segment whether or not the deployment wants one.

`icechunk.s3_store` is not a `Storage`. It is an `ObjectStoreConfig` used to
describe where *virtual chunks* live — the original NetCDF or GRIB files a
virtual dataset references. Passing it where a `Storage` is expected is a type
error, and the name similarity is the easiest mistake to make here.

## Repository lifecycle

`Repository.exists(storage)` tests for a repository without creating one.
`Repository.create(storage)` refuses a prefix that is not clean, which makes
"create" genuinely mean create. `Repository.open_or_create(storage)` is the
one to call from a service: it removes the `exists()`-then-branch dance that
`streaming/store.py:33` performs today, and that dance is filesystem-specific
anyway.

There is no delete-repository API. Removing a repository means deleting its
objects directly, which on S3 means a prefix listing and a bulk delete through
obstore.

`Storage.list_objects_metadata` is available for inspecting what is actually
under a prefix, which is useful in tests and in a cleanup path.

## Sessions

`repo.readonly_session(branch=...)` pins a snapshot at the moment it is opened.
A reader that already holds a session is unaffected by any later publish — this
is the property that makes pointer-move publication safe, and it is exactly
what the rename swap cannot offer.

`repo.writable_session("main")` is not picklable. For distributed writes the
session is forked (`session.fork()`), the workers write, and the results are
merged back. This constrains the write API more than it constrains storage, but
it is the reason `icechunk.xarray.to_icechunk` exists.

## Branches, tags and pointers

```
repo.lookup_branch(name)                      -> snapshot id
repo.create_branch(name, snapshot_id)
repo.reset_branch(name, snapshot_id, from_snapshot_id=...)
repo.delete_branch(name)
repo.list_branches()
repo.ancestry(branch=... | snapshot_id=...)
```

`reset_branch` with `from_snapshot_id` is a compare-and-swap: the move applies
only if the branch still points where the caller thinks it does. That is the
concurrency primitive a publish needs, and it works identically on filesystem
and S3.

Tags cannot serve as a moving pointer. A tag is immutable, and once deleted its
name cannot be recreated. A published pointer therefore has to be a branch.
Tags remain useful for naming a snapshot permanently, which is a different job.

`repo.rearrange_session().move()` performs a metadata-only group move, which
makes a group-level swap inside one repository cheap — relevant if a design ever
wants `published` and `draft` as groups rather than branches.

## Expiry and collection

`expire_snapshots(older_than=...)` marks snapshots unreachable; it does not
free bytes. `garbage_collect` is what reclaims chunk and manifest storage.
OCS calls the former after every streaming ingest
(`streaming/orchestrator.py:347`) and never calls the latter, so stores keep
their chunk data. Any design where rollback means "publish an older snapshot"
has to bound expiry so that published snapshots survive it.

## Configuration

`RepositoryConfig` and `StorageSettings` expose `unsafe_use_conditional_create`
and `unsafe_use_conditional_update`. These turn off the conditional writes that
make Icechunk safe against concurrent writers. They exist for object stores
that genuinely lack conditional PUT. They must never be disabled to make a test
pass: a test that only passes with them off is testing a configuration nobody
should run.

## xarray integration

Write with `icechunk.xarray.to_icechunk(ds, session, mode=...)` or
`append_dim=...`. It is required rather than preferred for distributed writes,
because plain `ds.to_zarr(session.store)` cannot participate in session forking
and merging. OCS uses `ds.to_zarr(session.store, ...)` throughout
(`streaming/orchestrator.py:292` and `:304`), which works for single-process
ingest and closes the door on distributed writes.

Read with:

```python
dataset = xr.open_zarr(
    session.store,
    consolidated=False,
    zarr_format=3,
    decode_coords="all",
)
```

`consolidated=False` because Icechunk has its own manifest.
`decode_coords="all"` is mandatory, not stylistic: without it `spatial_ref`
stays a data variable, rioxarray finds no CRS, and the dataset silently loses
its georeferencing. OCS does not pass it anywhere.

NaN in a Zarr attribute breaks Icechunk metadata handling (icechunk#1238), so
attributes have to be checked for finiteness before they are written. OCS's
`atomic_json` deliberately allows NaN when persisting job records
(`shared/persistence.py`), which is a different file but the same trap one layer
over.

## Version note

This checkout of OCS has icechunk 2.0.5 installed while `uv.lock` has moved to
2.2.0. 2.2.0 is the version to target: it fixes a `to_icechunk` `TypeError`
against xarray 2026.x, and 2.1.2 carried an etag fix and a reworked exception
tree. Upgrading Icechunk is a prerequisite for the migration, not a
consequence of it.
