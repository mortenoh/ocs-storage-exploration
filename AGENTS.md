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
- Add optional dependencies with `uv add --optional raster-io <package>`.
- Add development dependencies with `uv add --dev <package>`.
- Install everything with `make install` (`uv sync --all-extras`).

## Layout

```
src/ocs_storage_exploration/
  main.py             FastAPI application factory and the StorageError handler
  settings.py         Settings and ObjectStorageSettings, read from OCS_STORAGE_ variables
  __main__.py         uvicorn entry point
  api/                routers, dependencies, query parameters and wire schemas
  storage/
    addresses.py      StorageScheme and StorageAddress
    keys.py           object key layout and identifier validation
    errors.py         StorageError hierarchy with HTTP status codes
    schemas.py        catalog records, the tagged dataset union and result models
    protocols.py      StorageBackend and Catalog protocols
    registry.py       scheme to backend factory registry
    catalog.py        ObjectCatalog over the backend object store
    objects.py        conditional object writes shared by the catalog and the pointer
    backends/         filesystem, memory and S3 backends
    raster/           Icechunk and GeoZarr engine
    vector/           GeoParquet engine
tests/                pytest suite, parametrised over the filesystem, memory and s3 backends
docs/                 mkdocs sources
```

## Testing

- `make test` runs everything except the tests marked `s3`.
- `make test-s3` starts rustfs, runs everything marked `s3` against it and stops
  rustfs again, including when a test fails.
- The `s3` marker is not only on `tests/test_s3_*.py`: the `backend_scheme`
  fixture is parametrised over the filesystem, memory and S3 backends, and the
  S3 parameter carries the marker, so `pytest -m s3` runs the whole suite
  against the live endpoint.
- Never leave a container running. `make docker-run-file` and `make docker-run-s3`
  run in the foreground; `make docker-down` cleans up after an interrupted run.
- obstore and Icechunk talk to S3 through Rust, bypassing botocore, so moto and
  `mock_aws` cannot intercept their requests. Use the local rustfs endpoint instead.
- Never disable Icechunk conditional writes to make a test pass.
