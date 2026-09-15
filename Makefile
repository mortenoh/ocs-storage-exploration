.PHONY: help install lint test coverage run docs docs-serve docs-build s3-up s3-down clean

# ==============================================================================
# Venv
# ==============================================================================

UV := $(shell command -v uv 2> /dev/null)
VENV_DIR?=.venv
PYTHON := $(VENV_DIR)/bin/python
PORT?=8000
DOCS_PORT?=8001

# ==============================================================================
# Targets
# ==============================================================================

help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  install      Install dependencies including optional extras"
	@echo "  lint         Run formatter, linter and both type checkers"
	@echo "  test         Run tests (excludes tests marked s3)"
	@echo "  coverage     Run tests with coverage reporting"
	@echo "  run          Run the service with reload on port $(PORT)"
	@echo "  docs-serve   Serve documentation locally on port $(DOCS_PORT)"
	@echo "  docs-build   Build the documentation site in strict mode"
	@echo "  docs         Alias for docs-serve"
	@echo "  s3-up        Start the local rustfs S3-compatible object storage"
	@echo "  s3-down      Stop the local rustfs S3-compatible object storage"
	@echo "  clean        Clean up temporary files"

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

s3-up:
	@echo ">>> Starting rustfs"
	@docker compose up -d rustfs

s3-down:
	@echo ">>> Stopping rustfs"
	@docker compose down

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
