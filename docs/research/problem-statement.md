# Problem statement

Adding S3 to OCS is an addressing and publication change, not a configuration
change. Three properties of the current design assume a local filesystem in the
data model rather than in the I/O layer: stores are addressed as bare
`pathlib.Path` values, publication is a directory rename, and the artifact index
is a single JSON file guarded by a POSIX advisory lock. A `backend: s3` setting
would satisfy none of them. A fourth problem is adjacent rather than caused by
storage: the vector half of the service does not exist yet, so whatever
abstraction is built has to cover it before it is written a second time.

## Six constructors, one assumption

Every Icechunk store in OCS is opened by one of six direct calls to
`icechunk.local_filesystem_storage`:

| Call site | Role |
| --- | --- |
| `streaming/store.py:33` | `open_or_create_repo`, the ingest write path |
| `data_accessor/services/accessor.py:170` | `open_icechunk_dataset`, the read path |
| `data_manager/services/downloader.py:293` | time-coordinate rechunk |
| `data_manager/services/downloader.py:414` | `write_to_icechunk_store`, whole-store write |
| `ingestions/services.py:1208` | readonly session behind the HTTP store routes |
| `stac/media_types.py:107` | root-attribute read for media-type advertisement |

Each one takes a `Path` and calls `str()` on it. Replacing the constructor is
mechanical; the reason this is not a configuration change is everything the
`Path` touches on the way there.

## Addressing: a path is not an address

Managed stores live at `{data_dir}/downloads/{id}.icechunk`
(`config.py:115` defines the `downloads` subdirectory, `downloader.py:270`
builds the name). That location is carried in `ArtifactRecord.path`
(`ingestions/schemas.py:111`), a plain `str | None`.

The record is normalised on the way in and out by
`ingestions/artifact_paths.py`. `to_absolute` treats any non-absolute string as
a path relative to the data root and rejoins it, and treats any absolute string
as a legacy record to be re-rooted against `downloads`. An `s3://bucket/key`
value is not absolute, so it is silently rejoined into
`{data_root}/s3:/bucket/key` — a mangled local path, not an error. Nothing
downstream can recover the original URI.

The one place that does parse the string as a URI,
`ingestions/sync_engine.py:436`, exists to reject anything that is not local:
a scheme other than `file` returns `"non-local URI"`, and a path outside the
single trusted root from `_artifact_storage_roots` (`sync_engine.py:420`)
returns `"untrusted local path"`. That check is correct for the current design
and is exactly the check that has to change.

`ingestions/services.py:1156`, `serve_icechunk_file`, is the other end of the
same assumption: it resolves a relative path under the store root and returns a
`FileResponse`, so the HTTP surface that lets a client use
`icechunk.http_storage()` is a filesystem passthrough.

## Publication: a rename

Publishing a rebuilt store is `_swap_store` (`ingestions/services.py:897`): two
directory renames, the published store moved aside to `.retired` and the
staging store moved into its place. The docstring concedes two caveats without
apology, both real. The target does not exist between the renames, so a reader
that opens in that window fails. A process kill between the renames leaves the
published path missing, which `recover_interrupted_swap`
(`ingestions/services.py:853`) repairs on the next sync. The same docstring
names the durable answer — publish through a pointer that can be switched
atomically — and files it as CLIM-880.

S3 has no directory rename. It has no directories. Porting `_swap_store` to an
object store means copying every object and then deleting the old ones, which
is slower than the write it is publishing and still not atomic. The rename is
not an implementation detail of the filesystem backend; it is the publication
model.

## The catalogue: one file, one host

Artifact records are persisted to `{data_dir}/artifacts/records.json` and
rewritten whole under an exclusive `portalocker` lock on a sibling lock file
(`shared/persistence.py`). `atomic_json` writes a temporary file, `fsync`s it,
`os.replace`s it into position and `fsync`s the parent directory. That is a
correct single-host design and has no equivalent on S3: advisory locks do not
cross hosts, and neither `os.replace` nor a directory `fsync` exists.

## The vector gap

The raster side is built. The vector side is not. `aggregate_spatial`
(`plugins/processes/aggregate_spatial.py:178`) produces a vector datacube by
carrying WKT geometries alongside feature-id labels, and GeoParquet exists only
as an openEO job result: `openeo/jobs.py:1585` is a bare
`gdf.to_parquet(path)` — no covering bounding box, no explicit schema version,
no versioning, no catalogue entry. CLIM-836, CLIM-1067 and CLIM-1068 design a
feature store that does not exist in this checkout. Building a storage
abstraction for raster alone would guarantee it is rebuilt for vector.

## Tickets

CLIM-555 (pluggable storage backend), CLIM-880 (atomic publication),
CLIM-836 / CLIM-1067 / CLIM-1068 (the GeoParquet feature store), and
climate-api issues #47 and #64, which prefer obstore over fsspec for raw object
operations. `docs/project_description.md:185` already records the CLIM-555
target list: European S3-compatible providers, AWS `af-south-1` and
`ap-southeast-1`, and self-hosted Ceph/RGW for sovereign deployments.

## What follows

The rest of this section inventories what OCS does today, establishes what
Icechunk and GeoParquet actually support, and proposes one model — URI
addressing, one backend protocol, a record-driven catalogue, and publication by
pointer move — that covers both raster and vector on filesystem, memory and S3.
