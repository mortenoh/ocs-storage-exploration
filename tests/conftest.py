"""Shared fixtures for the storage exploration tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import geopandas
import pytest
import shapely.geometry
from fastapi.testclient import TestClient

from ocs_storage_exploration.main import create_app
from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.addresses import StorageScheme
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.registry import build_backend
from ocs_storage_exploration.storage.service import StorageService


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.startswith("OCS_STORAGE_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture(params=[StorageScheme.FILE, StorageScheme.MEMORY], ids=["filesystem", "memory"])
def backend_scheme(request: pytest.FixtureRequest) -> StorageScheme:
    scheme: StorageScheme = request.param
    return scheme


@pytest.fixture
def settings(tmp_path: Path, backend_scheme: StorageScheme) -> Settings:
    return Settings(
        backend=backend_scheme,
        data_directory=tmp_path / "data",
        base_prefix="ocs",
    )


@pytest.fixture
def storage_backend(settings: Settings) -> StorageBackend:
    return build_backend(settings)


@pytest.fixture
def catalog(storage_backend: StorageBackend) -> ObjectCatalog:
    return ObjectCatalog(storage_backend)


@pytest.fixture
def storage_service(settings: Settings) -> StorageService:
    return StorageService.from_settings(settings)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings=settings)) as test_client:
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
