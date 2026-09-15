"""Tests of ingesting the committed sample vector files into a collection, through the engine and the API."""

from __future__ import annotations

import geopandas
import pytest
from fastapi.testclient import TestClient

from ocs_storage_exploration.storage.errors import FeatureIdentityError, VectorInputError
from ocs_storage_exploration.storage.service import StorageService
from ocs_storage_exploration.storage.vector.ingest import (
    build_vector_ingest_plan,
    ingest_vector_file,
    open_vector_file,
)
from tests.ingest_helpers import DISTRICTS_FILE, LAKES_FILE, SAMPLES_DIRECTORY

DISTRICT_COUNT = 13
LAKE_COUNT = 25


def test_a_geojson_without_a_crs_is_read_as_wgs84() -> None:
    frame = open_vector_file(DISTRICTS_FILE)
    assert frame.crs is not None
    assert frame.crs.to_epsg() == 4326
    assert len(frame) == DISTRICT_COUNT


def test_a_column_that_is_empty_on_every_row_is_dropped() -> None:
    # Every DHIS2 organisation unit carries `dimensions: {}`, which Parquet has no type for.
    assert "dimensions" in geopandas.read_file(DISTRICTS_FILE).columns
    assert "dimensions" not in open_vector_file(DISTRICTS_FILE).columns
    assert "parentName" in open_vector_file(DISTRICTS_FILE).columns


def test_districts_are_ingested_published_and_read_back(storage_service: StorageService) -> None:
    plan = build_vector_ingest_plan(
        path=str(DISTRICTS_FILE),
        roots=[SAMPLES_DIRECTORY],
        identifier_property="id",
        selectable_columns=("level", "name"),
        title="Sierra Leone districts",
        license="BSD-3-Clause",
        attribution="DHIS2 demo database",
        publish=True,
    )
    result = ingest_vector_file(storage_service.vector, "sle-districts", plan)
    assert result.feature_count == DISTRICT_COUNT
    assert result.published is True

    record = storage_service.require_collection("sle-districts")
    assert record.features.identifier_property == "id"
    assert record.features.selectable_columns == ("level", "name")
    assert record.bbox is not None
    assert record.bbox.minimum_x == pytest.approx(-13.3, abs=0.2)
    assert record.bbox.maximum_y == pytest.approx(10.0, abs=0.2)

    handle = storage_service.vector.read("sle-districts")
    assert len(handle.frame) == DISTRICT_COUNT


def test_an_identifier_property_the_file_lacks_is_refused(storage_service: StorageService) -> None:
    plan = build_vector_ingest_plan(
        path=str(DISTRICTS_FILE),
        roots=[SAMPLES_DIRECTORY],
        identifier_property="shapeID",
    )
    with pytest.raises(FeatureIdentityError, match="shapeID"):
        ingest_vector_file(storage_service.vector, "sle-districts", plan)


def test_a_file_of_an_unreadable_suffix_is_refused(storage_service: StorageService) -> None:
    plan = build_vector_ingest_plan(
        path=str(SAMPLES_DIRECTORY / "README.md"),
        roots=[SAMPLES_DIRECTORY],
        identifier_property="id",
    )
    with pytest.raises(VectorInputError, match="readable vector suffix"):
        ingest_vector_file(storage_service.vector, "readme", plan)


def test_districts_are_ingested_through_the_api_and_reach_stac(ingest_client: TestClient) -> None:
    response = ingest_client.post(
        "/api/v1/vector/sle-districts/ingest",
        json={
            "path": str(DISTRICTS_FILE),
            "identifier_property": "id",
            "selectable_columns": ["level", "name"],
            "title": "Sierra Leone districts",
            "license": "BSD-3-Clause",
            "attribution": "DHIS2 demo database",
            "publish": True,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json() == {
        "dataset_identifier": "sle-districts",
        "version": 1,
        "feature_count": DISTRICT_COUNT,
        "published": True,
    }

    features = ingest_client.get("/api/v1/vector/sle-districts/features", params={"where": "level:2"})
    assert features.status_code == 200, features.text
    assert features.json()["number_returned"] == DISTRICT_COUNT

    collection = ingest_client.get("/stac/collections/sle-districts")
    assert collection.status_code == 200, collection.text
    payload = collection.json()
    assert payload["table:row_count"] == DISTRICT_COUNT
    assert payload["license"] == "BSD-3-Clause"
    assert {column["name"] for column in payload["table:columns"]} >= {"id", "level", "name"}


def test_lakes_are_ingested_as_a_draft(ingest_client: TestClient) -> None:
    response = ingest_client.post(
        "/api/v1/vector/ne-lakes/ingest",
        json={"path": str(LAKES_FILE), "identifier_property": "id", "selectable_columns": ["name"]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["feature_count"] == LAKE_COUNT
    assert response.json()["published"] is False

    record = ingest_client.get("/api/v1/datasets/ne-lakes")
    assert record.status_code == 200, record.text
    assert record.json()["publication"]["published"] is False
    # A collection with nothing published still reads back its newest written version.
    features = ingest_client.get("/api/v1/vector/ne-lakes/features", params={"limit": 5})
    assert features.status_code == 200, features.text
    assert features.json()["version"] == 1
