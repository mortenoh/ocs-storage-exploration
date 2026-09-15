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
| `s3` | `S3StorageBackend` | Stub in this pass: full constructor, honest refusal |

`BaseStorageBackend` implements the parts that do not depend on the scheme:
building an address below the base prefix, `exists`, `list_keys`, and
`delete_prefix` as an obstore listing followed by a bulk delete. The registry
maps a scheme to a factory, so a backend is selected by configuration and never
by an import in an engine.

The S3 backend holds a complete configuration and answers `describe()` with
`available: false`; every operation that would touch S3 raises
`BackendNotSupportedError`, which the API reports as 501. It is listed by
`GET /api/v1/backends` so a deployment can see that the scheme is registered
without discovering the gap at write time.

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

The memory backend returns `None` from `parquet_filesystem()`, which is why the
vector engine has the buffered path at all. That path is not a test-only
branch: it is also what an object store without a pyarrow filesystem would use.

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

`make s3-up` starts the rustfs endpoint from `compose.yml`, which pre-creates
the default bucket (`OCS_STORAGE_S3__BUCKET`, `ocs-storage-exploration` unless
overridden) before starting the server, so there is no bucket creation step in
the service itself. The S3 API is on port 9000 and the console on port 9001.

Because the S3 backend still refuses every operation in this pass, the endpoint
is there for the `s3`-marked tests and for the second pass rather than for
running the service against S3 today. obstore and Icechunk reach S3 through Rust
and bypass botocore, so `moto` cannot intercept those requests and a real
endpoint is the only honest test; see
[testing S3 locally](../research/testing-s3-locally.md).
