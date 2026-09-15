"""Tests that the routers run in the threadpool and that the shared caches survive several threads."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from inspect import iscoroutinefunction
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from ocs_storage_exploration.api import backends, datasets, health, raster, stac, vector
from ocs_storage_exploration.main import create_app
from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.addresses import StorageScheme

COVERAGE = "concurrent-coverage"
COLLECTION = "concurrent-collection"
REQUEST_COUNT = 8
# Generous by two orders of magnitude against the milliseconds the probe needs: the test is a
# regression guard against a router that blocks the event loop, not a latency benchmark.
HEALTH_BUDGET_SECONDS = 5.0

RASTER_BODY: dict[str, Any] = {
    "title": "Synthetic temperature",
    "shape": [8, 16],
    "timestep_count": 3,
    "publish": True,
}
VECTOR_BODY: dict[str, Any] = {
    "title": "Demo districts",
    "publish": True,
    "feature_collection": {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"id": "oslo"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[10.6, 59.8], [10.9, 59.8], [10.9, 60.0], [10.6, 60.0], [10.6, 59.8]]],
                },
            },
        ],
    },
}


@pytest.fixture
def memory_settings() -> Settings:
    return Settings(backend=StorageScheme.MEMORY, base_prefix="ocs")


def test_every_storage_route_is_offloaded_to_the_threadpool() -> None:
    routers = (backends.router, datasets.router, health.router, raster.router, stac.router, vector.router)

    coroutine_routes = sorted(
        route.path
        for router in routers
        for route in router.routes
        if isinstance(route, APIRoute) and iscoroutinefunction(route.endpoint)
    )

    # The health probe is the one route that touches no storage, so it stays on the event loop.
    assert coroutine_routes == ["/health"]


def test_concurrent_reads_are_served_while_health_answers_promptly(client: TestClient) -> None:
    assert client.post(f"/api/v1/raster/{COVERAGE}", json=RASTER_BODY).status_code == 201
    assert client.post(f"/api/v1/vector/{COLLECTION}", json=VECTOR_BODY).status_code == 201

    with ThreadPoolExecutor(max_workers=REQUEST_COUNT * 2) as pool:
        pending = [pool.submit(client.get, f"/api/v1/raster/{COVERAGE}/query") for _ in range(REQUEST_COUNT)]
        pending += [pool.submit(client.get, f"/api/v1/vector/{COLLECTION}/features") for _ in range(REQUEST_COUNT)]
        # The probe is issued while the threadpool is saturated: it is an async route, so it is
        # answered on the event loop rather than queued behind the storage calls.
        started = time.perf_counter()
        health = client.get("/health")
        elapsed = time.perf_counter() - started
        responses: list[httpx.Response] = [future.result() for future in pending]

    assert health.status_code == 200
    assert elapsed < HEALTH_BUDGET_SECONDS
    assert [response.status_code for response in responses] == [200] * (REQUEST_COUNT * 2)
    assert {response.json()["timestep_count"] for response in responses[:REQUEST_COUNT]} == {3}
    assert {response.json()["number_returned"] for response in responses[REQUEST_COUNT:]} == {1}


def test_two_concurrent_creates_of_different_datasets_both_succeed(memory_settings: Settings) -> None:
    with TestClient(create_app(settings=memory_settings)) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [
                pool.submit(client.post, f"/api/v1/raster/{identifier}", json=RASTER_BODY)
                for identifier in ("concurrent-alpha", "concurrent-beta")
            ]
            responses: list[httpx.Response] = [future.result() for future in pending]

        assert [response.status_code for response in responses] == [201, 201]
        listed = client.get("/api/v1/datasets").json()["items"]
        # Each repository keeps its own cached in-memory storage, so neither create lost its cube.
        assert sorted(record["dataset_identifier"] for record in listed) == ["concurrent-alpha", "concurrent-beta"]
        assert {record["timestep_count"] for record in listed} == {3}
