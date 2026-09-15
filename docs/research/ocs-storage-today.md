# OCS storage today

`streaming/store.py:33` is the canonical seam. Every other Icechunk call site
either duplicates it or opens a repository that `open_or_create_repo` created,
and it is the only one whose signature (`store_path: Path` in, repository out)
is already the shape a backend resolver needs. Everything below is verified
against the checkout at `~/dev/dhis2/open-climate-service`.

## The six storage constructors

| File and line | Function | Repository call |
| --- | --- | --- |
| `streaming/store.py:33` | `open_or_create_repo` | `Repository.open` or `Repository.create` on an `exists()` check |
| `data_accessor/services/accessor.py:170` | `open_icechunk_dataset` | `Repository.open` |
| `data_manager/services/downloader.py:293` | time-coordinate rechunk | `Repository.open` |
| `data_manager/services/downloader.py:414` | `write_to_icechunk_store` | `Repository.open_or_create` |
| `ingestions/services.py:1208` | `_open_icechunk_store_or_404` | `Repository.open` |
| `stac/media_types.py:107` | `_read_icechunk_root_attributes` | `Repository.open` |

`streaming/store.py:33` open-or-creates by testing `store_path.exists()` rather
than calling `Repository.open_or_create`, and creates the parent directory
first. Both behaviours are filesystem-specific.

## The Icechunk API surface actually used

Repositories: `open`, `create`, `open_or_create`. Sessions:
`readonly_session("main")`, `writable_session("main")`, `session.store`,
`session.commit(message)`, `session.snapshot_id`. History and refs:
`ancestry` (`streaming/store.py`), `lookup_branch`, `create_branch`,
`reset_branch`, `delete_branch`, `list_branches`, `expire_snapshots`.

Branches are used once, as a rollback safety net rather than as a publication
mechanism: `ingestions/services.py:658` records the pre-ingest HEAD under
`ocs-ingest-rollback-{uuid4().hex}` before a resumable sync, and
`recover_interrupted_swap` deletes any branch with that prefix it finds
(`ingestions/services.py:884`). No tags are created anywhere. `"main"` is the
only long-lived ref.

## Write paths

There are two, and they do not share code.

The streaming orchestrator appends period by period
(`streaming/orchestrator.py:290` onwards). The first period is
`ds.to_zarr(session.store, mode="w", zarr_format=3)`; every later period is
`ds.to_zarr(session.store, group=append_group, append_dim=spec.time_dim,
zarr_format=3)`, where `append_group` comes from `committed_data_group` because
a store already promoted to a pyramid keeps its arrays under level groups.
GeoZarr root attributes are rewritten on every commit, and there is one commit
per period. After the loop, `expire_snapshots(older_than=now)` prunes the
intermediate snapshots; the comment at `orchestrator.py:340` notes that this
marks snapshots expired without reclaiming chunk data, which would need
`garbage_collect`.

The whole-store path is `write_to_icechunk_store`
(`data_manager/services/downloader.py:330`). It normalises the cube to the
raster contract, then builds a topozarr pyramid when the grid exceeds
`_PYRAMID_PIXEL_THRESHOLD = 1024 * 1024` pixels (`downloader.py:37`), leaving
the data in group `"0"`. The docstring records that three CRS calls are
required in order — `proj.assign_crs`, `rio.write_crs`, `proj.assign_crs`
again, because `rio.write_crs` destroys xproj CRS detection.

## Read path

`open_icechunk_dataset` (`data_accessor/services/accessor.py:163`) opens a
readonly session on `"main"`, calls `xr.open_zarr(session.store,
zarr_format=3)`, and falls back to `group="0"` when the root has no data
variables. It sorts by the time dimension and calls `record_snapshot(str(path),
session.snapshot_id)` (`shared/provenance.py`) so a job result can name the
snapshot it read. Note what is absent: `decode_coords="all"` appears nowhere in
the package, so the `spatial_ref` coordinate is not promoted to a coordinate on
read and rioxarray cannot recover the CRS from the returned dataset.

## Publication and recovery

`_swap_store` (`ingestions/services.py:897`) renames the published store to
`.retired` and the staging store into its place. Its docstring concedes two
caveats: the target does not exist between the renames, so a reader in that
window fails; and only an exception is rolled back in-process, so a kill
between renames needs `recover_interrupted_swap` (`ingestions/services.py:853`)
on the next sync. `_rollback_store_swap` and `_finalize_store_swap` complete
the set, with a `.failed` copy as the third directory state.

## HTTP and trust boundaries

`_artifact_storage_roots` (`ingestions/sync_engine.py:420`) returns exactly one
trusted root, and `_resolve_local_artifact_path` rejects any URI whose scheme
is not `file`. `serve_icechunk_file` (`ingestions/services.py:1156`) resolves a
requested path under the store root, checks `is_relative_to`, and returns a
`FileResponse` — a filesystem passthrough serving what
`icechunk.http_storage()` expects.

## Contracts and dispatch

`shared/raster_contract.py` states five invariants enforced at the write
boundary: the temporal dimension is named `t`, spatial dims are `y`/`x` in
`(..., y, x)` order, `y` descends, geographic longitudes run -180 to 180, and
the declared CRS matches its coordinates. `shared/crs.py:103` writes
`proj:code` alongside the `spatial_ref` attributes.

`ArtifactFormat` (`ingestions/schemas.py:11`) is a three-member `StrEnum`
(`zarr`, `netcdf`, `icechunk`). The package contains 17 `ArtifactFormat.`
references, 16 of them `ICECHUNK` — spread across `stac/services.py`,
`openeo/execution.py`, `openeo/jobs.py`, `ingestions/sync_engine.py` and
`ingestions/services.py`. Each is an ad-hoc `if`, not an exhaustive dispatch,
and a fourth format would have to find all of them.

The precedent for a plugin seam already exists: `exports/registry.py` discovers
built-in, installed and instance-local export plugins, and `_register` logs and
skips a broken module rather than taking discovery endpoints down. A storage
backend registry should look like that.

## Tests

Tests reach into the seam directly: `tests/test_datasets.py:1082` and three
other sites `monkeypatch.setattr(services, "open_or_create_repo", lambda _:
transaction_repo)`. That the tests already substitute this one function is
further evidence it is the right seam — but they substitute it by name on a
module, which a protocol-typed backend would replace with an injected object.

## Versions in this checkout

`.venv` has icechunk 2.0.5, xarray 2025.12.0, zarr 3.2.1, geopandas 1.1.3 and
pyarrow 24.0.0. `pyproject.toml:84` declares `icechunk>=2.0,<3` and `uv.lock`
has already moved to 2.2.0, so the upgrade is staged but not installed.
