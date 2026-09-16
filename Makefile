.PHONY: help install offline samples demo lint test test-s3 coverage run docs docs-serve docs-build \
	docker-build docker-run docker-run-file docker-run-s3 clean

# ==============================================================================
# Venv
# ==============================================================================

UV := $(shell command -v uv 2> /dev/null)
VENV_DIR?=.venv
PYTHON := $(VENV_DIR)/bin/python
PORT?=8000
DOCS_PORT?=8001

# Settings the s3-marked tests are run with; they match the rustfs service in compose.yml.
S3_ENDPOINT_URL?=http://127.0.0.1:9000
S3_BUCKET?=ocs-storage-exploration
S3_REGION?=us-east-1
S3_ACCESS_KEY_ID?=rustfsadmin
S3_SECRET_ACCESS_KEY?=rustfsadmin

# ==============================================================================
# Targets
# ==============================================================================

help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  install         Install every dependency into the virtual environment"
	@echo "  offline         Run every step that needs the network, so the rest works offline"
	@echo "  samples         Download and clip the sample files (needs the network, once)"
	@echo "  demo            Ingest every sample into the configured backend (offline)"
	@echo "  lint            Run formatter, linter and both type checkers"
	@echo "  test            Run tests (excludes tests marked s3)"
	@echo "  test-s3         Run the s3-marked tests against rustfs, stopping it again afterwards"
	@echo "  coverage        Run tests with coverage reporting"
	@echo "  run             Run the service with reload on port $(PORT)"
	@echo "  docs-serve      Serve documentation locally on port $(DOCS_PORT)"
	@echo "  docs-build      Build the documentation site in strict mode"
	@echo "  docs            Alias for docs-serve"
	@echo "  docker-build    Build the service image"
	@echo "  docker-run      Alias for docker-run-file"
	@echo "  docker-run-file Seed ./data and run the service on the filesystem backend in the foreground (Ctrl-C stops it)"
	@echo "  docker-run-s3   Seed the bucket and run the service on the s3 backend with rustfs in the foreground (Ctrl-C stops it)"
	@echo "  clean           Clean up temporary files"

install:
	@echo ">>> Installing dependencies"
	@$(UV) sync --all-extras

# Everything that needs the network, in one target, so a laptop can be prepared before going offline.
# The DuckDB warm-up is best effort: it only makes the snippet in the inspecting guide work offline.
offline: install samples docker-build
	@echo ">>> Pulling the rustfs image"
	@docker compose pull rustfs
	@echo ">>> Warming the DuckDB extensions"
	@command -v uvx >/dev/null 2>&1 && \
		uvx --with duckdb python -c "import duckdb; c = duckdb.connect(); c.execute('INSTALL spatial; INSTALL httpfs')" \
		|| echo "    skipped: uvx is not on the PATH"
	@echo ""
	@echo ">>> Ready for offline use. Cached on this machine:"
	@echo "    - the virtual environment in $(VENV_DIR) (uv sync --all-extras)"
	@echo "    - the sample files in samples/ and samples/downloaded/"
	@echo "    - the service and seed images for the file and s3 compose profiles"
	@echo "    - the rustfs image compose.yml pins"
	@echo "    - the DuckDB spatial and httpfs extensions in ~/.duckdb"
	@echo "    Next, offline: make demo, make run, make test, make docs"

samples:
	@echo ">>> Fetching the downloadable samples"
	@$(UV) run python scripts/fetch_samples.py

demo:
	@echo ">>> Ingesting every sample into the $${OCS_STORAGE_BACKEND:-file} backend"
	@$(UV) run ocs-storage-exploration-demo

lint:
	@echo ">>> Running formatter and linter"
	@$(UV) run ruff format .
	@$(UV) run ruff check . --fix
	@echo ">>> Running type checkers"
	@$(UV) run mypy --explicit-package-bases src tests scripts
	@$(UV) run pyright

test:
	@echo ">>> Running tests"
	@$(UV) run pytest -q

# rustfs is always stopped again, including when pytest fails, and the pytest exit code is kept.
# The trap is installed before `up --wait`, in the same recipe shell, so a container that never turns
# healthy is torn down too rather than left behind by a recipe line that failed before the trap existed.
# rustfs runs as uid 10001; Linux Docker creates the ./.rustfs bind mount as root 755, so make it writable first.
test-s3:
	@echo ">>> Running the s3-marked tests against rustfs"
	@mkdir -p .rustfs && chmod a+rwx .rustfs
	@set -e; \
	trap 'docker compose down rustfs --remove-orphans' EXIT; \
	docker compose up -d --wait rustfs; \
	OCS_STORAGE_S3__ENDPOINT_URL=$(S3_ENDPOINT_URL) \
	OCS_STORAGE_S3__BUCKET=$(S3_BUCKET) \
	OCS_STORAGE_S3__REGION=$(S3_REGION) \
	OCS_STORAGE_S3__ACCESS_KEY_ID=$(S3_ACCESS_KEY_ID) \
	OCS_STORAGE_S3__SECRET_ACCESS_KEY=$(S3_SECRET_ACCESS_KEY) \
	OCS_STORAGE_S3__ALLOW_HTTP=true \
	OCS_STORAGE_S3__FORCE_PATH_STYLE=true \
	$(UV) run pytest -m s3 -q

coverage:
	@echo ">>> Running tests with coverage"
	@$(UV) run coverage run -m pytest -q
	@$(UV) run coverage report
	@$(UV) run coverage xml

run:
	@echo ">>> Running the service on port $(PORT)"
	@$(UV) run uvicorn ocs_storage_exploration.main:create_app --factory --reload --port $(PORT)

# NO_MKDOCS_2_WARNING silences the Material for MkDocs promotional banner.
docs-serve:
	@echo ">>> Serving documentation at http://127.0.0.1:$(DOCS_PORT)"
	@NO_MKDOCS_2_WARNING=1 $(UV) run mkdocs serve --dev-addr 127.0.0.1:$(DOCS_PORT)

docs-build:
	@echo ">>> Building documentation site"
	@NO_MKDOCS_2_WARNING=1 $(UV) run mkdocs build --strict

docs: docs-serve

docker-build:
	@echo ">>> Building the service image"
	@docker compose --profile file --profile s3 build

docker-run: docker-run-file

# The stack runs in the foreground and the trap runs `down` however the run ends, so Ctrl-C leaves
# neither a container nor a network behind. `docker ps -a` is empty afterwards.
# ./data is bind mounted into the container, so the container runs as the host user that owns it;
# without this the service cannot write its catalog on Linux, where the bind mount keeps host ids.
# The flag is --abort-on-container-failure rather than --abort-on-container-exit: the profile holds
# a one-shot seed that exits 0 once the demo datasets are written, and abort-on-container-exit reads
# that success as a reason to stop the stack, racing the API that its completion just released.
docker-run-file:
	@echo ">>> Running the service on the filesystem backend at http://127.0.0.1:8000"
	@mkdir -p data
	@set -e; \
	trap 'docker compose --profile file down' EXIT; \
	OCS_UID=$$(id -u) OCS_GID=$$(id -g) docker compose --profile file up --build --abort-on-container-failure

# rustfs runs as uid 10001; Linux Docker creates the ./.rustfs bind mount as root 755, so make it writable first.
docker-run-s3:
	@echo ">>> Running the service on the s3 backend at http://127.0.0.1:8001"
	@mkdir -p .rustfs && chmod a+rwx .rustfs
	@set -e; \
	trap 'docker compose --profile s3 down' EXIT; \
	docker compose --profile s3 up --build --abort-on-container-failure

clean:
	@echo ">>> Cleaning up"
	@find . -type f -name "*.pyc" -delete
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .coverage htmlcov coverage.xml
	@rm -rf .pyright
	@rm -rf site
	@rm -rf dist build *.egg-info

# ==============================================================================
# Default
# ==============================================================================

.DEFAULT_GOAL := help
