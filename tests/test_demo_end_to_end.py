"""End-to-end test of the demo seed: the functions `make demo` runs, then the API they seeded."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ocs_storage_exploration.demo import (
    MISSING_SAMPLE_HINT,
    RasterDemo,
    VectorDemo,
    failed_outcome,
    ingest_raster_demo,
    ingest_vector_demo,
    outcome_state,
    raster_demos,
    run_demos,
    vector_demos,
)
from ocs_storage_exploration.storage.errors import StorageError
from ocs_storage_exploration.storage.service import StorageService
from tests.ingest_helpers import SAMPLES_DIRECTORY

CHIRPS = "chirps3-sle-daily"
WORLDPOP = "worldpop-sle-2026"
DISTRICTS = "sle-districts"
GEOBOUNDARIES = "sle-adm2-geoboundaries"
LAKES = "ne-lakes"

DEMO_DATASET_COUNT = 5
DISTRICT_COUNT = 13
LAKE_COUNT = 25
GEOBOUNDARIES_COUNT = 14
CHIRPS_DAY_COUNT = 14
SIERRA_LEONE_BBOX = "-13.3,7.9,-12.0,9.0"


def raster_demo(dataset_identifier: str) -> RasterDemo:
    return next(demo for demo in raster_demos(SAMPLES_DIRECTORY) if demo.dataset_identifier == dataset_identifier)


def vector_demo(dataset_identifier: str) -> VectorDemo:
    return next(demo for demo in vector_demos(SAMPLES_DIRECTORY) if demo.dataset_identifier == dataset_identifier)


def service_of(client: TestClient) -> StorageService:
    # The memory backend holds its objects on the instance, so the demo has to seed through the
    # service the application is serving from rather than through one built next to it.
    service: StorageService = cast(FastAPI, client.app).state.storage
    return service


@pytest.fixture
def seeded_client(ingest_client: TestClient) -> TestClient:
    service = service_of(ingest_client)
    ingest_raster_demo(service, raster_demo(WORLDPOP))
    ingest_vector_demo(service, vector_demo(DISTRICTS))
    ingest_vector_demo(service, vector_demo(LAKES))
    return ingest_client


def test_the_demo_ingests_the_committed_samples(ingest_client: TestClient) -> None:
    service = service_of(ingest_client)

    worldpop = ingest_raster_demo(service, raster_demo(WORLDPOP))
    districts = ingest_vector_demo(service, vector_demo(DISTRICTS))
    lakes = ingest_vector_demo(service, vector_demo(LAKES))

    assert worldpop.detail == "1 timestep, 2026-01-01 to 2026-01-01, variable population"
    assert (worldpop.published, worldpop.skipped, worldpop.failed) == (True, False, False)
    assert districts.detail == f"version 1, {DISTRICT_COUNT} features, id id"
    assert districts.published is True
    assert lakes.detail == f"version 1, {LAKE_COUNT} features, id id"
    # ne-lakes is the draft of the demo, so the catalog holds it and STAC never advertises it.
    assert lakes.published is False


def test_the_datasets_endpoint_lists_what_the_demo_seeded(seeded_client: TestClient) -> None:
    response = seeded_client.get("/api/v1/datasets")

    assert response.status_code == 200, response.text
    records = {item["dataset_identifier"]: item for item in response.json()["items"]}
    assert set(records) == {WORLDPOP, DISTRICTS, LAKES}
    assert [records[name]["item_type"] for name in (WORLDPOP, DISTRICTS, LAKES)] == ["coverage", "feature", "feature"]
    published = [records[name]["publication"]["published"] for name in (WORLDPOP, DISTRICTS, LAKES)]
    assert published == [True, True, False]


def test_the_catalog_records_hold_relative_storage_keys(seeded_client: TestClient) -> None:
    items = seeded_client.get("/api/v1/datasets").json()["items"]

    keys = {item["dataset_identifier"]: item["storage_key"] for item in items}
    assert keys == {WORLDPOP: f"raster/{WORLDPOP}", DISTRICTS: f"vector/{DISTRICTS}", LAKES: f"vector/{LAKES}"}


def test_a_worldpop_window_summarises_real_cells(seeded_client: TestClient) -> None:
    response = seeded_client.get(f"/api/v1/raster/{WORLDPOP}/query", params={"bbox": SIERRA_LEONE_BBOX})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["variable"] == "population"
    assert payload["timestep_count"] == 1
    assert payload["cell_count"] > 0
    assert payload["maximum"] is not None
    assert payload["maximum"] > 0


def test_districts_are_selected_by_a_column_the_demo_declares(seeded_client: TestClient) -> None:
    response = seeded_client.get(
        f"/api/v1/vector/{DISTRICTS}/features",
        params={"where": "level:2", "columns": "name,level"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["number_returned"] == DISTRICT_COUNT
    assert {feature["properties"]["level"] for feature in payload["features"]} == {2}
    assert "parentName" not in payload["features"][0]["properties"]


def test_a_reseeded_collection_gains_a_version_that_rolls_back(seeded_client: TestClient) -> None:
    # A second seed of the same collection is a second version, published over the first: the demo
    # never skips a dataset it already wrote, so a re-run is idempotent in identifiers, not in versions.
    second = ingest_vector_demo(service_of(seeded_client), vector_demo(DISTRICTS))
    assert second.detail == f"version 2, {DISTRICT_COUNT} features, id id"
    assert seeded_client.get(f"/api/v1/vector/{DISTRICTS}/features").json()["version"] == 2

    rollback = seeded_client.post(f"/api/v1/vector/{DISTRICTS}/publish", json={"version": 1})

    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["version"] == 1
    assert rollback.json()["previous_version"] == 2
    assert seeded_client.get(f"/api/v1/vector/{DISTRICTS}/features").json()["version"] == 1


def test_stac_advertises_only_the_published_datasets(seeded_client: TestClient) -> None:
    response = seeded_client.get("/stac/collections")

    assert response.status_code == 200, response.text
    collections = {collection["id"]: collection for collection in response.json()["collections"]}
    assert set(collections) == {WORLDPOP, DISTRICTS}
    assert collections[DISTRICTS]["table:row_count"] == DISTRICT_COUNT
    assert collections[WORLDPOP]["license"] == "CC-BY-4.0"


def test_a_missing_download_skips_rather_than_fails(storage_service: StorageService, tmp_path: Path) -> None:
    # The two downloaded samples are optional, so a machine that never ran `make samples` still
    # seeds the three committed ones and the run still exits zero.
    outcomes = run_demos(storage_service, sample_directory=tmp_path / "samples")

    assert len(outcomes) == DEMO_DATASET_COUNT
    assert all(outcome.skipped and not outcome.failed for outcome in outcomes)
    assert all(MISSING_SAMPLE_HINT in outcome.detail for outcome in outcomes)


def test_a_refused_dataset_is_reported_as_a_failure(ingest_client: TestClient) -> None:
    # What makes the seed container fail rather than bring up an empty API: a dataset the storage
    # layer refuses becomes a failed outcome, and `main` exits non-zero when it sees one.
    broken = replace(vector_demo(LAKES), identifier_property="no-such-column")

    with pytest.raises(StorageError) as raised:
        ingest_vector_demo(service_of(ingest_client), broken)

    outcome = failed_outcome(broken.dataset_identifier, "collection", raised.value)
    assert outcome.failed is True
    assert outcome_state(outcome) == "failed"


@pytest.mark.samples
def test_the_downloaded_samples_complete_the_five_datasets(seeded_client: TestClient) -> None:
    service = service_of(seeded_client)

    chirps = ingest_raster_demo(service, raster_demo(CHIRPS))
    geoboundaries = ingest_vector_demo(service, vector_demo(GEOBOUNDARIES))
    for outcome in (chirps, geoboundaries):
        if outcome.skipped:
            pytest.skip(outcome.detail)

    assert chirps.detail == f"{CHIRPS_DAY_COUNT} timesteps, 2024-01-01 to 2024-01-14, variable precipitation"
    assert geoboundaries.detail == f"version 1, {GEOBOUNDARIES_COUNT} features, id shapeID"
    listed = {item["dataset_identifier"] for item in seeded_client.get("/api/v1/datasets").json()["items"]}
    assert listed == {CHIRPS, WORLDPOP, DISTRICTS, GEOBOUNDARIES, LAKES}
    advertised = {collection["id"] for collection in seeded_client.get("/stac/collections").json()["collections"]}
    assert advertised == {CHIRPS, WORLDPOP, DISTRICTS, GEOBOUNDARIES}
