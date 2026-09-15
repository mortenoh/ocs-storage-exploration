# Testing S3 locally

Test everything above the backend on the filesystem and in memory by default,
and run the same suite again against rustfs behind a marker. That split keeps
the default test run fast and dependency-free while still exercising a real S3
implementation, on demand, everywhere it matters rather than only in the backend
class. moto is not part of the plan, and not because of preference: the two
libraries that do the I/O here never touch botocore, so `mock_aws` cannot
intercept anything they do.

## Above the backend: tmp_path and in-memory

Every test of addresses, keys, models, the catalogue, the raster repository and
the vector collection store is parametrised over backends: a filesystem backend
rooted at pytest's `tmp_path`, a memory backend built on
`icechunk.in_memory_storage()` plus obstore's `MemoryStore`, and — under the
`s3` marker, so not by default — a live S3 backend. All three are real
implementations of the same protocol, so a test that passes on them is testing
behaviour rather than a filesystem.

This is what Icechunk's own test suite does, and it works because the
conditional-write semantics the design depends on — create-if-absent and
compare-and-swap — behave the same on `LocalStore`, `MemoryStore` and S3.

One detail the memory backend must get right: `icechunk.in_memory_storage()`
returns a *new* store on every call, so two calls for the same address are two
unrelated repositories. The memory backend caches the `Storage` per address in
a dict. The cache is per backend instance, which is load-bearing — two
`StorageService` instances in one process are two separate universes, and tests
depend on that isolation.

## The backend layer: rustfs

rustfs in Docker is the S3 target. Icechunk's own compose file runs rustfs as
the primary endpoint its test suite points at; MinIO is in that compose file
only to cover one leading-slash regression. Following the same choice here
means the local endpoint is the one Icechunk itself exercises on every run.

rustfs is a pre-1.0 release candidate, pinned to `rustfs/rustfs:1.0.0-rc.6`,
the version Icechunk pins. If a conformance issue shows up — anything where
rustfs and real S3 disagree — MinIO remains the fallback, and the settings the
tests use are the same either way.

```makefile
make test-s3    # docker compose up -d --wait rustfs, pytest -m s3, then compose down
```

`make test-s3` owns the whole lifecycle: it starts rustfs, waits for its
healthcheck, runs the marked tests with the endpoint settings exported, and
stops rustfs again from a shell `trap ... EXIT`, so the container is removed
even when pytest fails and the pytest exit code is what `make` reports. Nothing
is left running. `docker compose up -d --wait rustfs` on its own does the same
first step; naming the service enables its compose profile automatically, so the
API containers stay down.

The compose service pre-creates the bucket by making a directory under `/data`
before starting the server, so there is no separate bucket-creation step.
Ports 9000 (S3) and 9001 (console) are published on the host, and the bucket
lives in the `./.rustfs` bind mount.

S3 tests are marked and skipped by default:

```toml
[tool.pytest.ini_options]
markers = ["s3: requires a running S3-compatible endpoint"]
addopts = "-m 'not s3'"
```

```python
@pytest.mark.s3
def test_s3_backend_round_trip(...): ...
```

The marker is not only on the two S3-specific test modules: the `backend_scheme`
fixture is parametrised over the filesystem, the memory *and* the S3 backend,
with the S3 parameter carrying `pytest.mark.s3`. So `pytest -m s3` runs the
entire catalogue, raster, vector and HTTP API suite a third time, against the
live endpoint, and `pytest` without it stays Docker-free. Each S3 test gets its
own base prefix, `test-{uuid4().hex[:12]}`, which the fixture sweeps with
`delete_prefix` on teardown; a full run leaves the bucket empty.

Configuration comes from the same settings the service uses, with the nested
delimiter:

```
OCS_STORAGE_BACKEND=s3
OCS_STORAGE_S3__BUCKET=ocs
OCS_STORAGE_S3__ENDPOINT_URL=http://127.0.0.1:9000
OCS_STORAGE_S3__REGION=us-east-1
OCS_STORAGE_S3__ACCESS_KEY_ID=rustfsadmin
OCS_STORAGE_S3__SECRET_ACCESS_KEY=rustfsadmin
OCS_STORAGE_S3__ALLOW_HTTP=true
OCS_STORAGE_S3__FORCE_PATH_STYLE=true
```

`ALLOW_HTTP` and `FORCE_PATH_STYLE` are both required for a local endpoint:
without the first the client refuses a plaintext URL, and without the second it
tries to resolve `ocs.localhost`.

## What rustfs actually did

The backend was implemented against `rustfs/rustfs:1.0.0-rc.6` and the whole
suite was run on it. What follows is observed rather than expected.

**Conditional PUT works, on both clients.** `obstore.put(..., mode="create")`
answers `AlreadyExistsError` when the object exists, and
`obstore.put(..., mode={"e_tag": stale})` answers `PreconditionError` with a
plain `412 Precondition Failed` from rustfs. Neither raises
`NotImplementedError`, which is the whole point: `NotImplementedError` is what
obstore's `LocalStore` raises, and it is the only signal that would send the
catalogue and the vector pointer down the non-atomic emulation. That path is now
guarded — `storage/objects.py` re-raises as `BackendNotSupportedError` if any
store other than `LocalStore` reports no conditional put, so an endpoint that
silently lacks it fails loudly instead of losing a write. `MemoryStore`, for the
record, raises `PreconditionError` like S3 does, so the memory backend exercises
the real path too and only the filesystem backend emulates.

Icechunk commits and `reset_branch(..., from_snapshot_id=...)` behave the same
way: a stale `from_snapshot_id` answers `icechunk.ConflictError`, which the
raster repository maps to `PublicationConflictError`.

**One real quirk: `pyarrow.fs.S3FileSystem.create_dir`.** The vector engine used
to call `create_dir(parent, recursive=True)` before writing Parquet, because
`GeoDataFrame.to_parquet` will not create the version directory on a local
filesystem. On rustfs that call materialises one listable entry per path
segment — `vector`, `vector/one`, `vector/one/versions`,
`vector/one/versions/v00001` — stored on disk as `<name>__XLDIR__`. obstore
lists them with the trailing slash stripped, and deleting the stripped name is a
no-op, so they survive a list-then-delete prefix sweep. The visible symptoms
were a `delete` that left four keys behind and a `versions()` that kept
reporting a version whose objects were gone. The fix is not a workaround: an
object store has no directories, so `create_dir` is now called only when the
Parquet filesystem is a `LocalFileSystem`. After it, a full S3 run leaves the
bucket with zero objects.

**Two non-quirks worth writing down.** `obstore.list(store, prefix)` lists
`prefix/`, so listing a full object key answers nothing on S3 exactly as it does
on the other two backends; use `exists` for a single object. And a bulk
`obstore.delete(store, keys)` that includes a key which is not there succeeds
rather than raising, which is what makes `delete_prefix` idempotent.

**Timing.** 176 tests run on S3 in roughly 23 seconds on a laptop against a
container on the same machine, and `make test-s3` takes about 33 seconds end to
end including starting and stopping rustfs. The 490-test default run is
unchanged at about 6 seconds.

No conformance gap was found, so MinIO stayed unused.

## Why moto is unproven

`mock_aws` works by patching botocore. Icechunk's S3 client is the native AWS
SDK compiled into its Rust extension, and obstore's is Arrow's `object_store`
crate. Neither imports botocore, neither goes through a Python HTTP stack, and
neither can be patched from Python. So the decorator form of moto is not
"discouraged" here — it is inert.

moto's *server* mode is a different proposition: it runs a real HTTP endpoint
that any client can be pointed at, which sidesteps the patching problem
entirely. The open question is whether it implements the conditional PUT
semantics this design depends on, specifically `If-None-Match: *` for
create-if-absent and `If-Match: <etag>` for compare-and-swap. moto 5.1.5 and
later claim support. Nobody in this project has verified it against Icechunk,
so it is listed as unproven rather than rejected. If it works it would remove
the Docker dependency from the S3 tests, which is worth someone's afternoon.

localstack is not used anywhere in this ecosystem and has not been evaluated.

## The rule that is not negotiable

Never set `unsafe_use_conditional_create=False` or
`unsafe_use_conditional_update=False` to make a test pass. Those flags exist
for object stores that genuinely lack conditional PUT, and turning them off
disables the exact mechanism that makes publication safe against concurrent
writers. A test that only passes with conditional writes disabled has proved
that the code is broken under the configuration everyone actually runs. If an
S3 implementation cannot do conditional PUT, the answer is a different
implementation, not a different flag.

## What the layers cover

| Layer | Test target | Runs by default |
| --- | --- | --- |
| addresses, keys, models, errors | pure functions | yes |
| catalogue, raster, vector | filesystem and memory backends | yes |
| HTTP API | `TestClient` over both backends | yes |
| S3 backend | rustfs | no, `-m s3` |
| everything above, a third time | rustfs | no, `-m s3` |

The gap this leaves is deliberate. A bug that only appears on S3 and not on
filesystem or memory is a bug in the backend class, and that is the one place
the marked tests cover. Everything else is backend-agnostic by construction,
and if it is not, the two default backends disagree and the test fails.

That claim was worth checking rather than asserting, which is why the whole
suite also runs on S3 under the marker. It found exactly one thing the two
default backends could not have found: the `create_dir` markers above, which are
invisible on a local filesystem because there directories are free.
