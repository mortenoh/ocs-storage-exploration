"""Tests that every route is awaitable, that the limiter bounds the engines and that the timeout answers 504."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from inspect import iscoroutinefunction
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from ocs_storage_exploration.api import backends, datasets, health, raster, stac, vector
from ocs_storage_exploration.main import create_app
from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.addresses import StorageAddress, StorageScheme
from ocs_storage_exploration.storage.backends.base import BaseStorageBackend
from ocs_storage_exploration.storage.errors import BackendUnavailableError
from ocs_storage_exploration.storage.paths import resolve_ingest_path, resolve_ingest_paths
from ocs_storage_exploration.storage.raster import ingest as raster_ingest
from ocs_storage_exploration.storage.raster.repository import RasterRepository
from ocs_storage_exploration.storage.vector import ingest as vector_ingest

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
RECREATED_VECTOR_BODY: dict[str, Any] = {
    "title": "Demo districts, again",
    "publish": True,
    "feature_collection": {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"id": "bergen"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[5.2, 60.3], [5.5, 60.3], [5.5, 60.5], [5.2, 60.5], [5.2, 60.3]]],
                },
            },
        ],
    },
}


# A storage call slow enough that the timeout below always wins the race.
SLOW_CALL_SECONDS = 0.6
TIMEOUT_BUDGET_SECONDS = 1.0
CONCURRENCY_LIMIT = 2
# What the probe may take while a slow storage call is in flight. It is well under SLOW_CALL_SECONDS
# on purpose: a call that blocks the event loop holds the probe for its whole duration, which this
# budget catches, while an answer from a free loop takes milliseconds.
BLOCKED_LOOP_BUDGET_SECONDS = 0.25

RASTER_INGEST_BODY: dict[str, Any] = {
    "files": ["absent.tif"],
    "variable": "rain",
    "timestamp": "2024-01-01T00:00:00Z",
}
VECTOR_INGEST_BODY: dict[str, Any] = {"path": "absent.geojson", "identifier_property": "id"}


@pytest.fixture
def memory_settings() -> Settings:
    return Settings(backend=StorageScheme.MEMORY, base_prefix="ocs")


def test_every_route_is_a_coroutine_awaiting_the_async_facade() -> None:
    routers = (backends.router, datasets.router, health.router, raster.router, stac.router, vector.router)
    routes = [route for router in routers for route in router.routes if isinstance(route, APIRoute)]

    blocking_routes = sorted(route.path for route in routes if not iscoroutinefunction(route.endpoint))

    # Nothing is left for FastAPI to offload: the async facade decides which calls reach a worker thread.
    assert blocking_routes == []
    assert len(routes) > 1


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


def test_a_storage_call_that_outlives_the_timeout_answers_504(
    memory_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow_query(*arguments: Any, **keywords: Any) -> Any:
        time.sleep(SLOW_CALL_SECONDS)
        raise AssertionError("the timeout should have answered long before this call returned")

    monkeypatch.setattr(RasterRepository, "query", slow_query)
    settings = memory_settings.model_copy(update={"storage_operation_timeout_seconds": 0.1})

    with TestClient(create_app(settings=settings)) as client:
        started = time.perf_counter()
        response = client.get(f"/api/v1/raster/{COVERAGE}/query")
        elapsed = time.perf_counter() - started

    assert response.status_code == 504
    assert response.json()["error"] == "StorageTimeoutError"
    # The worker thread is abandoned rather than cancelled, so the answer must not wait for it.
    assert elapsed < TIMEOUT_BUDGET_SECONDS


def test_both_ingest_routes_resolve_their_paths_on_a_worker_thread(
    memory_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[str] = []

    def recording_paths(*arguments: Any, **keywords: Any) -> list[Path]:
        threads.append(threading.current_thread().name)
        return resolve_ingest_paths(*arguments, **keywords)

    def recording_path(*arguments: Any, **keywords: Any) -> Path:
        threads.append(threading.current_thread().name)
        return resolve_ingest_path(*arguments, **keywords)

    monkeypatch.setattr(raster_ingest, "resolve_ingest_paths", recording_paths)
    monkeypatch.setattr(vector_ingest, "resolve_ingest_path", recording_path)
    settings = memory_settings.model_copy(update={"ingest_roots": [tmp_path]})

    with TestClient(create_app(settings=settings)) as client:
        raster_response = client.post(f"/api/v1/raster/{COVERAGE}/ingest", json=RASTER_INGEST_BODY)
        vector_response = client.post(f"/api/v1/vector/{COLLECTION}/ingest", json=VECTOR_INGEST_BODY)

    # Expanding a glob walks the filesystem, so it runs where the writes it plans run: on a worker
    # thread, inside the limiter and inside the timeout, rather than on the event loop in front of them.
    assert [name.startswith("AnyIO worker thread") for name in threads] == [True, True]
    # Raising the refusal inside the runner leaves it on the status it always answered with.
    assert raster_response.status_code == 400, raster_response.text
    assert raster_response.json()["error"] == "IngestPathError"
    assert vector_response.status_code == 400, vector_response.text
    assert vector_response.json()["error"] == "IngestPathError"


def test_a_slow_ingest_path_lookup_times_out_while_health_answers_promptly(
    memory_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started_lookup = threading.Event()

    def slow_lookup(*arguments: Any, **keywords: Any) -> list[Path]:
        started_lookup.set()
        time.sleep(SLOW_CALL_SECONDS)
        raise AssertionError("the timeout should have answered long before this lookup returned")

    monkeypatch.setattr(raster_ingest, "resolve_ingest_paths", slow_lookup)
    settings = memory_settings.model_copy(
        update={"ingest_roots": [tmp_path], "storage_operation_timeout_seconds": 0.1},
    )

    with TestClient(create_app(settings=settings)) as client, ThreadPoolExecutor(max_workers=1) as pool:
        ingesting = pool.submit(client.post, f"/api/v1/raster/{COVERAGE}/ingest", json=RASTER_INGEST_BODY)
        assert started_lookup.wait(timeout=HEALTH_BUDGET_SECONDS), "the ingest never reached the path lookup"
        # The probe is issued while the lookup is still going: a lookup on the event loop would hold it
        # for the whole of SLOW_CALL_SECONDS, which is what this test is a regression guard against.
        probed = time.perf_counter()
        health = client.get("/health")
        elapsed = time.perf_counter() - probed
        response: httpx.Response = ingesting.result()

    assert health.status_code == 200
    assert elapsed < BLOCKED_LOOP_BUDGET_SECONDS
    # The lookup is inside the runner, so the storage timeout is what bounds it rather than nothing.
    assert response.status_code == 504, response.text
    assert response.json()["error"] == "StorageTimeoutError"


def test_the_limiter_caps_how_many_engine_calls_run_at_once(
    memory_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = threading.Lock()
    observed = {"active": 0, "peak": 0}
    real_query = RasterRepository.query

    def counting_query(self: RasterRepository, *arguments: Any, **keywords: Any) -> Any:
        with lock:
            observed["active"] += 1
            observed["peak"] = max(observed["peak"], observed["active"])
        try:
            time.sleep(0.05)
            return real_query(self, *arguments, **keywords)
        finally:
            with lock:
                observed["active"] -= 1

    monkeypatch.setattr(RasterRepository, "query", counting_query)
    settings = memory_settings.model_copy(update={"max_concurrent_storage_operations": CONCURRENCY_LIMIT})

    with TestClient(create_app(settings=settings)) as client:
        assert client.post(f"/api/v1/raster/{COVERAGE}", json=RASTER_BODY).status_code == 201
        with ThreadPoolExecutor(max_workers=REQUEST_COUNT) as pool:
            pending = [pool.submit(client.get, f"/api/v1/raster/{COVERAGE}/query") for _ in range(REQUEST_COUNT)]
            responses: list[httpx.Response] = [future.result() for future in pending]

    assert [response.status_code for response in responses] == [200] * REQUEST_COUNT
    assert 0 < observed["peak"] <= CONCURRENCY_LIMIT


def test_a_recreate_after_a_deletion_that_never_swept_inherits_nothing(
    memory_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sweep = BaseStorageBackend.delete_prefix
    pending = {"failure": True}

    def failing_sweep(self: BaseStorageBackend, address: StorageAddress) -> int:
        # Only the delete request fails; the sweep the recreate runs to take the deletion over works.
        if pending["failure"]:
            pending["failure"] = False
            raise BackendUnavailableError("the object store went away mid sweep")
        return sweep(self, address)

    monkeypatch.setattr(BaseStorageBackend, "delete_prefix", failing_sweep)

    with TestClient(create_app(settings=memory_settings)) as client:
        assert client.post(f"/api/v1/vector/{COLLECTION}", json=VECTOR_BODY).status_code == 201
        assert client.delete(f"/api/v1/datasets/{COLLECTION}").status_code == 503

        assert client.post(f"/api/v1/vector/{COLLECTION}", json=RECREATED_VECTOR_BODY).status_code == 201
        recreated = client.get(f"/api/v1/vector/{COLLECTION}/features")
        stale = client.get(f"/api/v1/vector/{COLLECTION}/features", params={"version": 1})

    # The recreated collection starts from an empty prefix: the dead collection left no version behind
    # for it to serve, and nothing of it is reachable under the identifier that was reused.
    assert recreated.status_code == 200
    assert recreated.json()["version"] == 1
    assert [feature["properties"]["id"] for feature in recreated.json()["features"]] == ["bergen"]
    assert stale.status_code == 200
    assert [feature["properties"]["id"] for feature in stale.json()["features"]] == ["bergen"]
