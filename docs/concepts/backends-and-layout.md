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
`delete_prefix` as an obstore listing followed by a bulk delete. A plugin maps a
scheme to a backend, so a backend is selected by configuration and never by an
import in an engine.

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
addressing style, the client bounds below and whether credentials were
configured, and never a secret.

## Timeouts and retries

Three clients reach the same bucket, so a request that hangs must be bounded in
three places or it is not bounded at all. Four settings are translated once, in
`S3StorageBackend`, into whatever each client calls them:

| Setting | Default | obstore | Icechunk | pyarrow |
| --- | --- | --- | --- | --- |
| `connect_timeout_seconds` | 5 | `client_options["connect_timeout"]` | `StorageTimeoutSettings(connect_timeout_ms=)` | `connect_timeout` |
| `request_timeout_seconds` | 30 | `client_options["timeout"]` | `network_stream_timeout_seconds`, `read_timeout_ms` and `operation_attempt_timeout_ms` | `request_timeout` |
| `max_retries` | 3 | `retry_config["max_retries"]` | `StorageRetriesSettings(max_tries=max_retries + 1)` | `AwsStandardS3RetryStrategy(max_attempts=max_retries + 1)` |
| `retry_backoff_seconds` | 0.5 | `retry_config["backoff"]` | `initial_backoff_ms` and `max_backoff_ms` | fixed by the retry strategy |

Two counting conventions meet here. obstore counts retries *after* the first
attempt, while Icechunk and pyarrow count tries *including* it, so three retries
is four tries and the translation adds the one.

The retry budget follows from the rest:
`request_timeout_seconds * (max_retries + 1) + retry_backoff_seconds * max_retries`,
which is 121.5 seconds at the defaults above. It becomes obstore's
`retry_timeout` and Icechunk's `operation_timeout_ms`, so both give up on the
same wall clock rather than on their own defaults of three minutes.

That budget bounds one request. An engine call is many requests, and it is
bounded instead by the facade's
`OCS_STORAGE_STORAGE_OPERATION_TIMEOUT_SECONDS`, described under
[threading and async](../architecture.md#threading-and-async). The two are
deliberately of the same order: a deployment that raises one without the other
gets either a client that outlives the call it belongs to or a call that is
abandoned while its first request is still being retried.

Icechunk takes its settings in two places: `s3_storage(...)` takes the stream
timeout, and everything else arrives as a `RepositoryConfig` with a
`StorageSettings` block. `StorageBackend.repository_config()` is what carries
it: the S3 backend returns one, the filesystem and memory backends return
`None`, and `RasterRepository` passes whatever comes back to
`Repository.open_or_create`. The conditional-write switches in that same
`StorageSettings` block, `unsafe_use_conditional_create` and
`unsafe_use_conditional_update`, are never touched: publication depends on them.

When the budget runs out the three libraries spell the failure three ways -
obstore raises its fallback `GenericError`, Icechunk raises
`icechunk.StorageError`, and pyarrow raises a builtin `OSError`.
`storage/failures.py` is the one place that reads all three as the same thing
and reports `BackendUnavailableError`, a 503 that keeps the original message. A
missing object is not an outage, so `FileNotFoundError` still passes through to
the callers that read it as "absent".

## Writing a backend plugin

The framework underneath this section - its vocabulary, its dispatch modes and
why it was chosen - is described in [plugin framework](pluginkit.md).

Backends are [pluginkit](https://winterop-com.github.io/pluginkit) plugins. The
service declares three extension points on `StorageBackendSpecs` in
`storage/plugins.py`, and a plugin is any object whose methods are marked with
the matching `@extension`:

| Extension point | Dispatch | Answer |
| --- | --- | --- |
| `storage_backend(settings, scheme)` | `firstresult` | The backend serving that scheme, or `None` |
| `storage_backend_description(settings, scheme)` | `firstresult` | A `BackendDescription` built without touching a credential, or `None` |
| `storage_schemes()` | collecting | The schemes this plugin provides |

A plugin answers `None` for every scheme it does not own, so the three built-in
plugins (`FilesystemBackendPlugin`, `MemoryBackendPlugin`, `S3BackendPlugin`)
are three small classes next to the backends they build:

```python
from ocs_storage_exploration.storage.plugins import extension


class GcsBackendPlugin:
    """Provides the gs scheme."""

    @extension
    def storage_schemes(self) -> list[str]:
        """Report the gs scheme."""
        return ["gs"]

    @extension
    def storage_backend(self, settings: Settings, scheme: str) -> StorageBackend | None:
        """Build the GCS backend, or None for another scheme."""
        if scheme != "gs":
            return None
        return GcsStorageBackend.from_settings(settings)
```

`build_plugin_manager()`, in `storage/backends/__init__.py`, registers the
built-ins and then calls `load_entrypoints("ocs_storage_exploration.plugins")`,
so an installed distribution joins in by advertising itself:

```toml
[project.entry-points."ocs_storage_exploration.plugins"]
gs = "ocs_storage_gcs:plugin"
```

The entry-point value resolves to the plugin object itself, and the entry-point
name becomes the plugin name the manager registers it under. `examples/plugins/ocs-storage-null/`
is a complete worked example: a separate package providing a `null` scheme that
describes itself and refuses every operation. It is deliberately not installed
by `make install`, because installing it would add `null` to every
`GET /api/v1/backends` response of a development checkout;
`tests/test_plugins.py` registers it in process and stubs
`importlib.metadata.entry_points` instead.

Three rules follow from the dispatch modes:

- **First registered wins.** `storage_backend` is a `firstresult` extension
  point, and the built-ins are registered before the entry points are loaded, so
  a plugin claiming `file`, `memory` or `s3` never displaces the built-in. Two
  external plugins claiming the same scheme are resolved by entry-point order.
- **A scheme is a name, not an enum member.** `StorageScheme` still lists the
  three built-in schemes, but a scheme is carried as a string matching
  `[a-z][a-z0-9+.-]*`, so an external plugin can name its own.
  `backend_for_scheme` is the single place that refuses a scheme no plugin
  provides, with `BackendNotSupportedError`.
- **Describing costs nothing.** `describe_backends` asks the plugin of every
  inactive scheme to describe it, so listing the backends never creates a
  directory, builds a client or reads a credential.

The manager is built once per process by `default_plugin_manager()` and lives on
the storage service as `service.plugin_manager`; `StorageService.from_settings`
takes another one when a test needs an isolated set of plugins. Nothing wraps
the manager: `build_plugin_manager`, `default_plugin_manager` and the two
helpers above are the whole surface.

The manager is the sync `PluginManager`, even though the service is awaited
through `AsyncStorageService`. It is called once per process to build a backend
and once per request to describe the inactive schemes, and no hook awaits
anything: a backend constructor opens no connection. pluginkit also ships
`AsyncPluginManager`, with the same extension points and `async` callers, and
that is the one to move to the day a plugin has to await during construction -
fetching a token, say, or probing an endpoint. Until then it would add an await
to a call that never yields.

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
| `VectorCollectionStore` | `parquet_filesystem` and `parquet_path` to write and read Parquet, falling back to obstore plus a `pyarrow.BufferReader` when the filesystem is `None`; obstore for the `current.json` pointer, the per-version `reservation.json` and `metadata.json`, and for listing versions |
| `ObjectCatalog`, `AsyncObjectCatalog` | obstore only: `put` as a conditional create, an etag compare-and-swap or a plain overwrite, `get`, `list`. There is no `delete`: a deletion tombstones its record instead, so every transition is a compare-and-swap |

The catalogue record and every conditional vector object go through
`storage/objects.py`, which is the one place that knows how a conditional PUT
fails: `AlreadyExistsError` and `PreconditionError` become
`PublicationConflictError`, `create_object_if_absent` reports a lost create as
`None` for the callers that retry rather than fail, and the non-atomic
head-then-put emulation is reached only on obstore's `LocalStore`, and only for
an etag replace. Any other store that reports `NotImplementedError` for a
conditional write raises `BackendNotSupportedError` rather than quietly losing
the guarantee.

Every helper there comes in two spellings, `create_object` and
`create_object_async` and so on down the module, over obstore's sync and async
functions. The failure mapping is written once and shared, so the two cannot
disagree about what a lost create means. The engines call the sync ones, because
they already run on a worker thread; `AsyncObjectCatalog` calls the async ones,
because it runs on the event loop.

`ObjectCatalog` remembers nothing between calls. A caller that intends to edit a
record reads it with `get_entry`, which returns a `CatalogEntry` carrying the
record and the revision it was read at, and passes that revision back to `put`;
a caller creating a record passes `create=True`. A `put` with neither is a
documented, unconditional overwrite. There is no instance-wide etag memory, so
one component reading a record can no longer refresh the revision another
component is about to write against.

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
| `OCS_STORAGE_BACKEND` | `file` | Which scheme to build: `file`, `memory`, `s3`, or any scheme a plugin provides |
| `OCS_STORAGE_DATA_DIRECTORY` | `data` | Root directory of the filesystem backend |
| `OCS_STORAGE_BASE_PREFIX` | `ocs` | Key prefix every address starts with |
| `OCS_STORAGE_S3__*` | unset | The object storage block: `BUCKET`, `PREFIX`, `REGION`, `ENDPOINT_URL`, `ALLOW_HTTP`, `ACCESS_KEY_ID`, `SECRET_ACCESS_KEY`, `SESSION_TOKEN`, `FORCE_PATH_STYLE`, `ANONYMOUS`, and the four bounds of [timeouts and retries](#timeouts-and-retries) |

Two more decide how the service spends itself rather than where bytes land:

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCS_STORAGE_MAX_CONCURRENT_STORAGE_OPERATIONS` | `16` | How many blocking engine calls may run at once |
| `OCS_STORAGE_STORAGE_OPERATION_TIMEOUT_SECONDS` | `180` | How long one engine call may wait for a slot and run before it answers 504 |

Secrets are `SecretStr`, they never reach a catalog record or a backend
description, and `.env.example` carries a commented block that matches the
rustfs service in `compose.yml`.

## The key layout

The layout is owned by one module and is identical on every scheme. On the
filesystem these are directories under the data directory; on S3 they are keys
under the bucket:

```text
{base_prefix}/catalog/datasets/{dataset_identifier}.json
{base_prefix}/raster/{dataset_identifier}/{generation}/                     Icechunk repository
{base_prefix}/vector/{dataset_identifier}/{generation}/current.json         published pointer
{base_prefix}/vector/{dataset_identifier}/{generation}/versions/vNNNNN/reservation.json   version claim
{base_prefix}/vector/{dataset_identifier}/{generation}/versions/vNNNNN/data.parquet       the features
{base_prefix}/vector/{dataset_identifier}/{generation}/versions/vNNNNN/metadata.json      version metadata
```

`{generation}` is a uuid4 in hex minted when the dataset is created, so a dataset
deleted and written again under the same identifier never shares a prefix with the
one it replaced. Neither engine derives a key from the identifier: every one of
them is built from the `storage_key` of the record.
[Versioning](versioning.md) explains what that buys a deletion.

Three objects make up a collection version, and the order they are written in is
the whole safety argument. `reservation.json` is created first, with obstore's
`mode="create"`, so exactly one writer owns that version number. `data.parquet`
follows, and is never written over a key that already holds one. `metadata.json`
is created last and is what marks the version as finished: a directory without
one is a claim, not a version, and is skipped by every listing that matters.

Identifiers are validated against `[a-z0-9][a-z0-9_-]{0,127}` before they become
part of a key, and addresses reject empty, `.` and `..` segments, backslashes and
any URI userinfo. No engine builds a path itself, so there is one place where
traversal and credential leakage are prevented.

A catalog record holds a key, never a root. `storage_key` is exactly what
`backend.address(...)` takes, `raster/{dataset_identifier}/{generation}` or
`vector/{dataset_identifier}/{generation}`, without the base prefix and without a
scheme, and it is validated as an object key so an absolute or climbing one never
reaches a record. The absolute URI is built at serve time from the backend that is running,
so the same record serves `file:///app/data/ocs/...` inside a container,
`file:///srv/data/ocs/...` on the host and `s3://bucket/ocs/...` on S3. A record
that stored the URI it was written under would name a root that does not exist
anywhere else, which is what moving a data directory used to break.

[Inspecting the data](../guides/inspecting-the-data.md) walks the same layout on
disk, and [versioning](versioning.md) explains the `vNNNNN` numbering and the
pointer objects.

## On S3: one bucket, many datasets, isolated by prefix

An instance gets one bucket and one base prefix, and every dataset is a prefix
below it:

```text
s3://{bucket}/{base_prefix}/catalog/datasets/{dataset_identifier}.json
s3://{bucket}/{base_prefix}/raster/{dataset_identifier}/{generation}/...
s3://{bucket}/{base_prefix}/vector/{dataset_identifier}/{generation}/versions/v00001/data.parquet
```

Per-dataset isolation is by prefix rather than by bucket, for three reasons:

- Icechunk needs its own non-empty prefix per repository. From version 2.1 it
  refuses an empty prefix, and two repositories may not share one. A prefix per
  dataset satisfies that without a bucket per dataset.
- Delete is a prefix sweep: list everything below the dataset prefix and hand the
  keys to a bulk delete. That works the same for a directory and for a bucket
  prefix, and it is why there is no delete-repository API to miss. The catalog
  record above it is not swept: it is replaced by a tombstone, because obstore
  offers no conditional delete and a compare-and-swap is what the next writer of
  that identifier needs. See [versioning](versioning.md).
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
Its objects live in the `./.rustfs` bind mount, which `make test-s3` and
`make docker-run-s3` create and make world-writable before starting the
container: rustfs runs as uid 10001, and on Linux Docker would otherwise create
that directory as root, leaving the entrypoint unable to pre-create the bucket.

`make test-s3` starts it, runs the `s3`-marked tests against it and stops it
again; `make docker-run-s3` runs the service itself on the S3 backend next to
rustfs, in the foreground, so Ctrl-C stops both. obstore and Icechunk reach S3
through Rust and bypass botocore, so `moto` cannot intercept those requests and
a real endpoint is the only honest test; see
[testing S3 locally](../research/testing-s3-locally.md) for what rustfs was
observed to do.
