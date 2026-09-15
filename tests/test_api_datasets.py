"""Tests for the datasets router: mixed listings, item type filtering and typed deletes."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

COVERAGE = "mixed-coverage"
COLLECTION = "mixed-collection"

RASTER_BODY: dict[str, Any] = {"title": "Mixed coverage", "shape": [4, 8], "timestep_count": 2, "publish": True}
VECTOR_BODY: dict[str, Any] = {
    "title": "Mixed collection",
    "publish": True,
    "feature_collection": {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"id": "one"},
                "geometry": {"type": "Point", "coordinates": [10.7, 59.9]},
            },
        ],
    },
}


@pytest.fixture
def populated_client(client: TestClient) -> TestClient:
    assert client.post(f"/api/v1/raster/{COVERAGE}", json=RASTER_BODY).status_code == 201
    assert client.post(f"/api/v1/vector/{COLLECTION}", json=VECTOR_BODY).status_code == 201
    return client


def test_the_listing_carries_both_item_types(populated_client: TestClient) -> None:
    response = populated_client.get("/api/v1/datasets")

    assert response.status_code == 200
    items = response.json()["items"]
    assert {item["dataset_identifier"] for item in items} == {COVERAGE, COLLECTION}
    assert {item["item_type"] for item in items} == {"coverage", "feature"}
    assert {item["storage_format"] for item in items} == {"icechunk", "geoparquet"}


def test_the_listing_can_be_filtered_by_item_type(populated_client: TestClient) -> None:
    coverages = populated_client.get("/api/v1/datasets", params={"item_type": "coverage"}).json()["items"]
    features = populated_client.get("/api/v1/datasets", params={"item_type": "feature"}).json()["items"]

    assert [item["dataset_identifier"] for item in coverages] == [COVERAGE]
    assert [item["dataset_identifier"] for item in features] == [COLLECTION]


def test_an_unknown_item_type_is_refused(populated_client: TestClient) -> None:
    response = populated_client.get("/api/v1/datasets", params={"item_type": "raster"})

    assert response.status_code == 422


def test_one_record_can_be_read_by_identifier(populated_client: TestClient) -> None:
    response = populated_client.get(f"/api/v1/datasets/{COLLECTION}")

    assert response.status_code == 200
    assert response.json()["title"] == "Mixed collection"
    assert response.json()["features"]["feature_count"] == 1


def test_an_unknown_identifier_is_reported_as_missing(populated_client: TestClient) -> None:
    response = populated_client.get("/api/v1/datasets/absent")

    assert response.status_code == 404
    assert response.json()["error"] == "DatasetNotFoundError"


def test_deleting_the_coverage_leaves_the_collection_readable(populated_client: TestClient) -> None:
    deleted = populated_client.delete(f"/api/v1/datasets/{COVERAGE}")

    assert deleted.status_code == 204
    assert populated_client.get(f"/api/v1/raster/{COVERAGE}/query").status_code == 404
    assert populated_client.get(f"/api/v1/vector/{COLLECTION}/features").status_code == 200
    assert [item["dataset_identifier"] for item in populated_client.get("/api/v1/datasets").json()["items"]] == [
        COLLECTION
    ]


def test_deleting_the_collection_leaves_the_coverage_queryable(populated_client: TestClient) -> None:
    deleted = populated_client.delete(f"/api/v1/datasets/{COLLECTION}")

    assert deleted.status_code == 204
    assert populated_client.get(f"/api/v1/vector/{COLLECTION}/features").status_code == 404
    assert populated_client.get(f"/api/v1/raster/{COVERAGE}/query").status_code == 200
    assert [item["dataset_identifier"] for item in populated_client.get("/api/v1/datasets").json()["items"]] == [
        COVERAGE
    ]


def test_deleting_an_unknown_dataset_is_reported_as_missing(populated_client: TestClient) -> None:
    response = populated_client.delete("/api/v1/datasets/absent")

    assert response.status_code == 404
    assert response.json()["error"] == "DatasetNotFoundError"
