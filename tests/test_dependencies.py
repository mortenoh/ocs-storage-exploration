"""Tests for the FastAPI dependencies that read the application state."""

from __future__ import annotations

from fastapi import Request
from fastapi.testclient import TestClient

from ocs_storage_exploration.api.dependencies import (
    get_catalog,
    get_settings_from_request,
    get_storage_backend,
)
from ocs_storage_exploration.main import create_app
from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.protocols import Catalog, StorageBackend


def test_dependencies_resolve_the_objects_built_during_the_lifespan(settings: Settings) -> None:
    application = create_app(settings=settings)

    with TestClient(application):
        request = Request(scope={"type": "http", "method": "GET", "path": "/health", "headers": [], "app": application})

        assert get_settings_from_request(request) is settings
        assert isinstance(get_storage_backend(request), StorageBackend)
        assert isinstance(get_catalog(request), Catalog)
        assert get_storage_backend(request).scheme is settings.backend
