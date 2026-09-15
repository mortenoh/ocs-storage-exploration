"""Tests for the health router."""

from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from ocs_storage_exploration import __version__
from ocs_storage_exploration.main import create_app, handle_storage_error
from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.errors import DatasetNotFoundError


def test_health_reports_version_and_backend(client: TestClient, settings: Settings) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__, "backend": settings.backend}


def test_lifespan_builds_backend_and_catalog(settings: Settings) -> None:
    application = create_app(settings=settings)

    with TestClient(application):
        assert application.state.backend.scheme is settings.backend
        assert application.state.catalog is not None


async def test_storage_errors_are_rendered_with_their_status_code() -> None:
    request = Request(scope={"type": "http", "method": "GET", "path": "/health", "headers": []})

    response = await handle_storage_error(request, DatasetNotFoundError("no dataset record for 'absent'"))

    assert response.status_code == 404
    assert b"DatasetNotFoundError" in response.body
    assert b"no dataset record" in response.body


async def test_other_exceptions_are_not_swallowed() -> None:
    request = Request(scope={"type": "http", "method": "GET", "path": "/health", "headers": []})

    with pytest.raises(RuntimeError):
        await handle_storage_error(request, RuntimeError("boom"))
