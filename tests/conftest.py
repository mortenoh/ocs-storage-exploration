"""Shared fixtures for the storage exploration tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import geopandas
import pytest
import shapely.geometry
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ocs_storage_exploration.main import create_app
from ocs_storage_exploration.settings import ObjectStorageSettings, Settings
from ocs_storage_exploration.storage.addresses import StorageScheme
from ocs_storage_exploration.storage.backends import default_plugin_manager
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.catalog_async import AsyncObjectCatalog
from ocs_storage_exploration.storage.plugins import backend_for_scheme
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.service import StorageService
from ocs_storage_exploration.storage.service_async import AsyncStorageService
from tests.ingest_helpers import SAMPLES_DIRECTORY

# The environment is read at import time: the isolated_environment fixture clears every
# OCS_STORAGE_ variable before a test runs, so a fixture body would only ever see the defaults.
S3_ENDPOINT_URL = os.environ.get("OCS_STORAGE_S3__ENDPOINT_URL", "http://127.0.0.1:9000")
S3_BUCKET = os.environ.get("OCS_STORAGE_S3__BUCKET", "ocs-storage-exploration")
S3_REGION = os.environ.get("OCS_STORAGE_S3__REGION", "us-east-1")
S3_ACCESS_KEY_ID = os.environ.get("OCS_STORAGE_S3__ACCESS_KEY_ID", "rustfsadmin")
S3_SECRET_ACCESS_KEY = os.environ.get("OCS_STORAGE_S3__SECRET_ACCESS_KEY", "rustfsadmin")
S3_ALLOW_HTTP = os.environ.get("OCS_STORAGE_S3__ALLOW_HTTP", "true")
S3_FORCE_PATH_STYLE = os.environ.get("OCS_STORAGE_S3__FORCE_PATH_STYLE", "true")


def backend_for_settings(settings: Settings) -> StorageBackend:
    return backend_for_scheme(default_plugin_manager(), settings, settings.backend)


def boolean_from_environment(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def unique_test_prefix() -> str:
    return f"test-{uuid4().hex[:12]}"


def s3_settings(base_prefix: str) -> ObjectStorageSettings:
    return ObjectStorageSettings(
        bucket=S3_BUCKET,
        prefix=base_prefix,
        region=S3_REGION,
        endpoint_url=S3_ENDPOINT_URL,
        allow_http=boolean_from_environment(S3_ALLOW_HTTP),
        access_key_id=S3_ACCESS_KEY_ID,
        secret_access_key=SecretStr(S3_SECRET_ACCESS_KEY),
        force_path_style=boolean_from_environment(S3_FORCE_PATH_STYLE),
    )


def s3_test_settings() -> Settings:
    base_prefix = unique_test_prefix()
    return Settings(backend=StorageScheme.S3, base_prefix=base_prefix, s3=s3_settings(base_prefix))


def remove_s3_test_prefix(settings: Settings) -> None:
    backend = backend_for_settings(settings)
    backend.delete_prefix(backend.address())


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.startswith("OCS_STORAGE_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture(
    params=[
        StorageScheme.FILE,
        StorageScheme.MEMORY,
        pytest.param(StorageScheme.S3, marks=pytest.mark.s3),
    ],
    ids=["filesystem", "memory", "s3"],
)
def backend_scheme(request: pytest.FixtureRequest) -> StorageScheme:
    scheme: StorageScheme = request.param
    return scheme


@pytest.fixture
def settings(tmp_path: Path, backend_scheme: StorageScheme) -> Iterator[Settings]:
    if backend_scheme is not StorageScheme.S3:
        yield Settings(backend=backend_scheme, data_directory=tmp_path / "data", base_prefix="ocs")
        return
    configured = s3_test_settings()
    try:
        yield configured
    finally:
        remove_s3_test_prefix(configured)


@pytest.fixture
def live_s3_settings() -> Iterator[Settings]:
    configured = s3_test_settings()
    try:
        yield configured
    finally:
        remove_s3_test_prefix(configured)


@pytest.fixture
def live_s3_backend(live_s3_settings: Settings) -> StorageBackend:
    return backend_for_settings(live_s3_settings)


@pytest.fixture
def storage_backend(settings: Settings) -> StorageBackend:
    return backend_for_settings(settings)


@pytest.fixture
def catalog(storage_backend: StorageBackend) -> ObjectCatalog:
    return ObjectCatalog(storage_backend)


@pytest.fixture
def async_catalog(storage_backend: StorageBackend) -> AsyncObjectCatalog:
    return AsyncObjectCatalog(storage_backend)


@pytest.fixture
def storage_service(settings: Settings) -> StorageService:
    return StorageService.from_settings(settings)


@pytest.fixture
def async_storage_service(storage_service: StorageService) -> AsyncStorageService:
    return AsyncStorageService(storage_service)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


@pytest.fixture
def ingest_settings(settings: Settings) -> Settings:
    # The autouse fixture runs every test from tmp_path, so the committed samples are named absolutely
    # and the ingest root is the directory they live in.
    return settings.model_copy(update={"ingest_roots": [SAMPLES_DIRECTORY]})


@pytest.fixture
def ingest_client(ingest_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings=ingest_settings)) as test_client:
        yield test_client


@pytest.fixture
def sample_features() -> geopandas.GeoDataFrame:
    squares = [
        shapely.geometry.Polygon([(x, y), (x + 1, y), (x + 1, y + 1), (x, y + 1)]) for y in range(3) for x in range(3)
    ]
    # A U shape opening upwards: its envelope covers the notch between the two prongs, which no geometry touches.
    horseshoe = shapely.geometry.Polygon(
        [(10, 0), (13, 0), (13, 3), (12, 3), (12, 1), (11, 1), (11, 3), (10, 3)],
    )
    points = [shapely.geometry.Point(20.5, 20.5), shapely.geometry.Point(21.5, 21.5)]
    return geopandas.GeoDataFrame(
        {
            "id": [f"square-{index}" for index in range(9)] + ["horseshoe", "point-0", "point-1"],
            "level": [1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 5, 5],
            "path": [
                "/root/a/b",
                "/root/a/b",
                "/root/a/b",
                "/root/a/c",
                "/root/a/c",
                "/root/a/c",
                "/root/d/e",
                "/root/d/e",
                "/root/d/e",
                "/root/d/f",
                "/root/a/g",
                "/root/a/h",
            ],
        },
        geometry=[*squares, horseshoe, *points],
        crs="EPSG:4326",
    )
