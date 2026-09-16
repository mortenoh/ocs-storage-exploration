# Agent Instructions

This file is the guidance for coding agents working in this repository.
It applies to every coding agent (Claude Code, Codex, OpenCode) and is the only rules file here.

## Git Commits

- Use conventional commits format (feat:, fix:, docs:, chore:, refactor:, etc.)
- No attribution or co-authored-by lines
- Keep commit messages concise and descriptive

## Style

- No emojis ever in any output or files

That means no emojis in commit messages, pull request titles, pull request
descriptions, code, comments, docstrings, documentation, design documents or any
other output. Use plain text instead:

- "[x]" instead of a check mark
- "[ ]" instead of a cross mark
- "WARNING:" instead of a warning sign
- "Note:" instead of a memo sign

## Overall guidelines

- Be concise and to the point.
- Follow the existing code style and patterns.
- Use full descriptive names. The only abbreviations allowed are the wire names
  the specifications use: `bbox` and `crs`.
- Use type annotations everywhere; both mypy and pyright must stay clean.
- Line length is 120 characters.
- Ask before creating branches or pull requests.
- Always run `make lint && make test` after making changes.

## Code quality

- `schemas.py` holds pydantic `BaseModel` classes; `models.py` is reserved for
  ORM models and must not contain pydantic schemas.

## Documentation standards

- Every Python module: one-line module docstring at the top.
- Every class: one-line docstring.
- Every method and function: one-line docstring.
- Format: triple quotes, Google style, one line preferred.

```python
"""Module for resolving storage addresses."""


class AddressResolver:
    """Resolves storage addresses into backend handles."""

    def resolve(self, uri: str) -> str:
        """Resolve a storage URI into an object key."""
        ...
```

## Dependencies

- Never hand-edit the dependency tables in `pyproject.toml`.
- Add runtime dependencies with `uv add <package>`.
- Add optional dependencies with `uv add --optional <extra> <package>`.
  There is no extra today: rioxarray became a runtime dependency when the ingest
  endpoints started reading GeoTIFF and COG files.
- Add development dependencies with `uv add --dev <package>`.
- Install everything with `make install` (`uv sync --all-extras`).
- `make offline` is the one target that needs the network: it runs `install`,
  `samples`, `docker-build`, pulls rustfs and warms the DuckDB extensions.

## Layout

```
src/ocs_storage_exploration/
  main.py             FastAPI application factory and the StorageError handler
  settings.py         Settings and ObjectStorageSettings, read from OCS_STORAGE_ variables
  __main__.py         uvicorn entry point
  demo.py             the sample ingest behind `make demo` and the compose seed containers
  api/                routers, dependencies, query parameters and wire schemas
  storage/
    addresses.py      StorageScheme and StorageAddress
    keys.py           object key layout and identifier validation
    errors.py         StorageError hierarchy with HTTP status codes
    failures.py       obstore, Icechunk and pyarrow transport failures as one storage error
    schemas.py        catalog records, the tagged dataset union and result models
    protocols.py      StorageBackend, Catalog and AsyncCatalog protocols
    plugins.py        pluginkit extension points a backend plugin implements
    catalog.py        ObjectCatalog over the backend object store
    catalog_async.py  AsyncObjectCatalog over the same records, through obstore's async API
    objects.py        object reads, writes and listings, sync and async
    service.py        StorageService composing the backend, the catalog and both engines
    service_async.py  AsyncStorageService: native catalog reads, bounded worker threads
    backends/         filesystem, memory and S3 backends, their plugins and the plugin manager
    raster/           Icechunk and GeoZarr engine, including ingest.py for real files
    vector/           GeoParquet engine, including ingest.py for real files
    paths.py          ingest path and glob resolution, bounded by Settings.ingest_roots
tests/                pytest suite, parametrised over the filesystem, memory and s3 backends
scripts/              fetch_samples.py (make samples)
samples/              real sample files in git; samples/downloaded/ is fetched and gitignored
docs/                 mkdocs sources
examples/plugins/     external plugin packages, deliberately not installed
```

## Backend plugins

- A backend is a pluginkit plugin, not a table entry. Add one by implementing
  the extensions declared on `StorageBackendSpecs` in `storage/plugins.py`, and
  register it in `build_plugin_manager` (built in) or through the
  `ocs_storage_exploration.plugins` entry-point group (external).
- The built-in plugins are registered first and `storage_backend` is a
  `firstresult` extension point, so a plugin cannot displace `file`, `memory` or
  `s3`.
- Never install `examples/plugins/ocs-storage-null`: its entry point would add a
  `null` scheme to every `GET /api/v1/backends` response in a development
  checkout. The tests import it from its source tree instead.

## Testing

- `make test` runs everything except the tests marked `s3`.
- `make test-s3` starts rustfs, runs everything marked `s3` against it and stops
  rustfs again, including when a test fails.
- The `s3` marker is not only on `tests/test_s3_*.py`: the `backend_scheme`
  fixture is parametrised over the filesystem, memory and S3 backends, and the
  S3 parameter carries the marker, so `pytest -m s3` runs the whole suite
  against the live endpoint.
- Never leave a container running. `make docker-run-file` and `make docker-run-s3`
  run `docker compose up` in the foreground under a `trap ... EXIT` that runs
  `docker compose down`, so Ctrl-C stops and removes the containers and the
  network. Check with `docker ps -a` after a run: it must be empty.
- Both compose profiles carry a one-shot seed container that runs the demo before
  the API starts, so the stack answers with the five sample datasets already in it.
  The flag is `--abort-on-container-failure`: `--abort-on-container-exit` reads the
  seed's successful exit as a reason to stop the whole stack.
- Tests that need the files `make samples` downloads are marked `samples` and
  skip themselves when those files are absent, so `make test` is green either way.
- obstore and Icechunk talk to S3 through Rust, bypassing botocore, so moto and
  `mock_aws` cannot intercept their requests. Use the local rustfs endpoint instead.
- Never disable Icechunk conditional writes to make a test pass.
