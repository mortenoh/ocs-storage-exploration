"""Tests for the backends router, including that it never answers with a secret."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ocs_storage_exploration.main import create_app
from ocs_storage_exploration.settings import ObjectStorageSettings, Settings
from ocs_storage_exploration.storage.registry import registered_schemes

SECRET_VALUE = "rustfsadminsecret"
SESSION_TOKEN_VALUE = "rustfssessiontoken"


@pytest.fixture
def configured_client(settings: Settings) -> Iterator[TestClient]:
    configured = settings.model_copy(
        update={
            "s3": ObjectStorageSettings(
                bucket="ocs-exploration",
                region="eu-north-1",
                endpoint_url="http://localhost:9000",
                allow_http=True,
                access_key_id="rustfsadmin",
                secret_access_key=SecretStr(SECRET_VALUE),
                session_token=SecretStr(SESSION_TOKEN_VALUE),
            ),
        },
    )
    with TestClient(create_app(settings=configured)) as test_client:
        yield test_client


def test_the_active_backend_is_listed_first(client: TestClient, settings: Settings) -> None:
    response = client.get("/api/v1/backends")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == len(registered_schemes())
    assert items[0]["scheme"] == settings.backend
    assert items[0]["available"] is True
    assert items[0]["base_prefix"] == settings.base_prefix


def test_the_other_registered_schemes_are_reported_as_inactive(client: TestClient, settings: Settings) -> None:
    items = client.get("/api/v1/backends").json()["items"]

    inactive = [item for item in items if item["scheme"] != settings.backend]
    assert {item["scheme"] for item in inactive} == {
        str(scheme) for scheme in registered_schemes() if scheme != settings.backend
    }
    assert all(item["available"] is False for item in inactive)


def test_no_configured_secret_reaches_the_response_body(configured_client: TestClient) -> None:
    response = configured_client.get("/api/v1/backends")

    assert response.status_code == 200
    assert SECRET_VALUE not in response.text
    assert SESSION_TOKEN_VALUE not in response.text
