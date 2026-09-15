.PHONY: help install lint test test-s3 coverage run docs docs-serve docs-build \
	docker-build docker-run docker-run-file docker-run-s3 docker-down clean

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
	@echo "  install         Install dependencies including optional extras"
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
	@echo "  docker-run-file Run the service on the filesystem backend in the foreground (Ctrl-C stops it)"
	@echo "  docker-run-s3   Run the service on the s3 backend with rustfs in the foreground (Ctrl-C stops it)"
	@echo "  docker-down     Stop and remove every container of both profiles"
	@echo "  clean           Clean up temporary files"

install:
	@echo ">>> Installing dependencies"
	@$(UV) sync --all-extras

lint:
	@echo ">>> Running formatter and linter"
	@$(UV) run ruff format .
	@$(UV) run ruff check . --fix
	@echo ">>> Running type checkers"
	@$(UV) run mypy --explicit-package-bases src tests
	@$(UV) run pyright

test:
	@echo ">>> Running tests"
	@$(UV) run pytest -q

# rustfs is always stopped again, including when pytest fails, and the pytest exit code is kept.
# rustfs runs as uid 10001; Linux Docker creates the ./.rustfs bind mount as root 755, so make it writable first.
test-s3:
	@echo ">>> Running the s3-marked tests against rustfs"
	@mkdir -p .rustfs && chmod a+rwx .rustfs
	@docker compose up -d --wait rustfs
	@set -e; \
	trap 'docker compose down rustfs' EXIT; \
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

# The stack runs in the foreground so Ctrl-C stops it; nothing is left running afterwards.
docker-run-file:
	@echo ">>> Running the service on the filesystem backend at http://127.0.0.1:8000"
	@docker compose --profile file up --build --abort-on-container-exit

# rustfs runs as uid 10001; Linux Docker creates the ./.rustfs bind mount as root 755, so make it writable first.
docker-run-s3:
	@echo ">>> Running the service on the s3 backend at http://127.0.0.1:8001"
	@mkdir -p .rustfs && chmod a+rwx .rustfs
	@docker compose --profile s3 up --build --abort-on-container-exit

docker-down:
	@echo ">>> Stopping every container of both profiles"
	@docker compose --profile file --profile s3 down

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
