# Testing S3 locally

Test everything above the backend on the filesystem and in memory, and test the
backend itself against rustfs. That split keeps the default test run fast and
dependency-free while still exercising a real S3 implementation where it
matters. moto is not part of the plan, and not because of preference: the two
libraries that do the I/O here never touch botocore, so `mock_aws` cannot
intercept anything they do.

## Above the backend: tmp_path and in-memory

Every test of addresses, keys, models, the catalogue, the raster repository and
the vector collection store is parametrised over two backends: a filesystem
backend rooted at pytest's `tmp_path`, and a memory backend built on
`icechunk.in_memory_storage()` plus obstore's `MemoryStore`. Both are real
implementations of the same protocol, so a test that passes on both is testing
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
make s3-up      # docker compose up -d rustfs
make s3-down
```

The compose service pre-creates the bucket by making a directory under `/data`
before starting the server, so there is no separate bucket-creation step.
Ports 9000 (S3) and 9001 (console) are published on the host.

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

Run them with `pytest -m s3` once rustfs is up. Configuration comes from the
same settings the service uses, with the nested delimiter:

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

The gap this leaves is deliberate. A bug that only appears on S3 and not on
filesystem or memory is a bug in the backend class, and that is the one place
the marked tests cover. Everything else is backend-agnostic by construction,
and if it is not, the two default backends disagree and the test fails.
