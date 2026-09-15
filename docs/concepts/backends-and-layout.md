# Backends and key layout

A backend answers one question: given an address, what handle does a library
need to touch those bytes? There is one backend class per scheme, every backend
yields the same three handles, and no engine knows which one it is running on.
Changing `OCS_STORAGE_BACKEND` is the whole migration.

## One class per scheme

| Scheme | Class | State |
| --- | --- | --- |
| `file` | `FilesystemStorageBackend` | Complete; everything below one local directory |
| `memory` | `MemoryStorageBackend` | Complete; objects and repositories live in the process |
| `s3` | `S3StorageBackend` | Complete; one bucket, one base prefix, verified against rustfs |

`BaseStorageBackend` implements the parts that do not depend on the scheme:
building an address below the base prefix, `exists`, `list_keys`, and
`delete_prefix` as an obstore listing followed by a bulk delete. The registry
maps a scheme to a factory, so a backend is selected by configuration and never
by an import in an engine.

The S3 backend builds all three handles from one settings block and caches the
two that are per-bucket rather than per-address. `list_keys` and `delete_prefix`
are inherited from `BaseStorageBackend` unchanged, which is the point of keying
the obstore store on the bucket with no prefix of its own: an object key is the
same string for all three libraries and for all three backends.

One settings block, three translations:

| Setting | Icechunk | obstore | pyarrow |
| --- | --- | --- | --- |
| `endpoint_url` | `endpoint_url` | `config["endpoint"]` | `endpoint_override` plus `scheme` |
| `allow_http` | `allow_http` | `client_options={"allow_http": ...}` | `scheme="http"` |
| `force_path_style` | `force_path_style` | `virtual_hosted_style_request=False` | automatic once `endpoint_override` is set |
| `anonymous` | `anonymous` | `config["skip_signature"]` | `anonymous=True` |
| `access_key_id`, `secret_access_key`, `session_token` | same names | `config` keys | `access_key`, `secret_key`, `session_token` |

`icechunk_storage(address)` returns `icechunk.s3_storage(bucket=..., prefix=address.key, ...)`,
so every repository gets its own non-empty prefix, and `parquet_path(address)`
is `"{bucket}/{key}"`, which is the only shape `pyarrow.fs.S3FileSystem`
accepts. `describe()` reports the bucket, the region, the endpoint, the
addressing style and whether credentials were configured, and never a secret.

## Three handles, one address

```python
backend.icechunk_storage(address)  # icechunk.Storage for one repository
backend.object_store()             # obstore ObjectStore for raw object operations
backend.parquet_filesystem()       # pyarrow.fs.FileSystem, or None when bytes must be buffered
backend.parquet_path(address)      # the address rendered the way that filesystem expects
```

Three libraries have to address the same bytes, and none of them accepts the
others' handle: Icechunk does its own I/O, pyarrow reads Parquet through
`pyarrow.fs`, and obstore does the raw object operations that neither offers.
Building them from one settings block in one place is what keeps them pointing
at the same bucket.

Each part of the storage package uses exactly what it needs:

| Component | Handles it uses |
| --- | --- |
| `RasterRepository` | `icechunk_storage` for every read and write; `delete_prefix` to remove a repository |
| `VectorCollectionStore` | `parquet_filesystem` and `parquet_path` to write and read Parquet, falling back to obstore plus a `pyarrow.BufferReader` when the filesystem is `None`; obstore for the `current.json` pointer and for listing versions |
| `ObjectCatalog` | obstore only: `put` with etag compare-and-swap, `get`, `delete`, `list` |

The catalogue record and the vector pointer are both written through
`storage/objects.py`, which is the one place that knows how a conditional PUT
fails: `AlreadyExistsError` and `PreconditionError` become
`PublicationConflictError`, and the non-atomic head-then-put emulation is
reached only on obstore's `LocalStore`. Any other store that reports
`NotImplementedError` for a conditional write raises `BackendNotSupportedError`
rather than quietly losing the guarantee.

The memory backend returns `None` from `parquet_filesystem()`, which is why the
vector engine has the buffered path at all. That path is not a test-only
branch: it is also what an object store without a pyarrow filesystem would use.

One asymmetry between the filesystem and the two object stores is worth naming,
because it cost a debugging session. `GeoDataFrame.to_parquet` will not create
the version directory on a local filesystem, so the vector engine creates it
first — but only when the Parquet filesystem is a `LocalFileSystem`. On S3 the
same `create_dir` call materialises a listable entry per path segment that a
list-then-delete prefix sweep cannot remove, which left deleted collections
half-alive. An object store has no directories, so it is not asked to make one.

## Settings

Settings are read from `OCS_STORAGE_` environment variables, with `__` as the
nesting delimiter, and a `.env` file is honoured. The four that decide where
bytes land:

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCS_STORAGE_BACKEND` | `file` | Which scheme to build: `file`, `memory` or `s3` |
| `OCS_STORAGE_DATA_DIRECTORY` | `data` | Root directory of the filesystem backend |
| `OCS_STORAGE_BASE_PREFIX` | `ocs` | Key prefix every address starts with |
| `OCS_STORAGE_S3__*` | unset | The object storage block: `BUCKET`, `PREFIX`, `REGION`, `ENDPOINT_URL`, `ALLOW_HTTP`, `ACCESS_KEY_ID`, `SECRET_ACCESS_KEY`, `SESSION_TOKEN`, `FORCE_PATH_STYLE`, `ANONYMOUS` |

Secrets are `SecretStr`, they never reach a catalog record or a backend
description, and `.env.example` carries a commented block that matches the
rustfs service in `compose.yml`.

## The key layout

The layout is owned by one module and is identical on every scheme. On the
filesystem these are directories under the data directory; on S3 they are keys
under the bucket:

```text
{base_prefix}/catalog/datasets/{dataset_identifier}.json
{base_prefix}/raster/{dataset_identifier}/                       Icechunk repository
{base_prefix}/vector/{dataset_identifier}/current.json           published pointer
{base_prefix}/vector/{dataset_identifier}/versions/vNNNNN/data.parquet
```

Identifiers are validated against `[a-z0-9][a-z0-9_-]{0,127}` before they become
part of a key, and addresses reject empty, `.` and `..` segments, backslashes and
any URI userinfo. No engine builds a path itself, so there is one place where
traversal and credential leakage are prevented.

[Inspecting the data](../guides/inspecting-the-data.md) walks the same layout on
disk, and [versioning](versioning.md) explains the `vNNNNN` numbering and the
pointer objects.

## On S3: one bucket, many datasets, isolated by prefix

An instance gets one bucket and one base prefix, and every dataset is a prefix
below it:

```text
s3://{bucket}/{base_prefix}/catalog/datasets/{dataset_identifier}.json
s3://{bucket}/{base_prefix}/raster/{dataset_identifier}/...
s3://{bucket}/{base_prefix}/vector/{dataset_identifier}/versions/v00001/data.parquet
```

Per-dataset isolation is by prefix rather than by bucket, for three reasons:

- Icechunk needs its own non-empty prefix per repository. From version 2.1 it
  refuses an empty prefix, and two repositories may not share one. A prefix per
  dataset satisfies that without a bucket per dataset.
- Delete is a prefix sweep: list everything below the dataset prefix and hand the
  keys to a bulk delete. That works the same for a directory and for a bucket
  prefix, and it is why there is no delete-repository API to miss.
- IAM scopes on prefixes. A reader that may see one dataset gets a policy on
  `{base_prefix}/raster/{dataset_identifier}/*` without a new bucket, and buckets
  are a limited, region-bound, slow-to-create resource.

Several instances can share one bucket by giving each a different base prefix:
`OCS_STORAGE_S3__PREFIX=staging` and `=production` keep two catalogues,
two sets of repositories and two sets of collections apart with no key ever
colliding, because the base prefix is the first segment of every key the service
builds. The same trick separates a test run from real data.

`compose.yml` carries the rustfs endpoint, which pre-creates the default bucket
(`OCS_STORAGE_S3__BUCKET`, `ocs-storage-exploration` unless overridden) before
starting the server, so there is no bucket creation step in the service itself.
The S3 API is on port 9000 and the console on port 9001.

`make test-s3` starts it, runs the `s3`-marked tests against it and stops it
again; `make docker-run-s3` runs the service itself on the S3 backend next to
rustfs, in the foreground, so Ctrl-C stops both. obstore and Icechunk reach S3
through Rust and bypass botocore, so `moto` cannot intercept those requests and
a real endpoint is the only honest test; see
[testing S3 locally](../research/testing-s3-locally.md) for what rustfs was
observed to do.
