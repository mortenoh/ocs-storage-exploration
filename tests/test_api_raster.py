"""Tests for the raster router: create, append, query, publish, versions, rollback and delete."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ocs_storage_exploration.api.schemas import TIME_STEP_ATTRIBUTE, AppendRasterRequest
from ocs_storage_exploration.storage.errors import RasterContractError
from ocs_storage_exploration.storage.models import (
    BoundingBox,
    CoverageDataset,
    GridSpecification,
    Publication,
    TemporalExtent,
)

IDENTIFIER = "coverage-api"
VARIABLE = "temperature"
CREATE_BODY: dict[str, Any] = {
    "title": "API coverage",
    "shape": [8, 16],
    "variable": VARIABLE,
    "timestep_count": 3,
    "start_time": "2020-01-01T00:00:00Z",
    "step": "month",
    "publish": False,
}


def create_coverage(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post(f"/api/v1/raster/{IDENTIFIER}", json={**CREATE_BODY, **overrides})
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()
    return payload


def test_create_writes_a_coverage_and_reports_it_as_a_draft(client: TestClient) -> None:
    created = create_coverage(client)

    assert created["dataset_identifier"] == IDENTIFIER
    assert created["timestep_count"] == 3
    assert created["variables"] == [VARIABLE]
    assert created["published"] is False

    record = client.get(f"/api/v1/datasets/{IDENTIFIER}").json()
    assert record["item_type"] == "coverage"
    assert record["title"] == "API coverage"
    assert record["grid"]["shape"] == [8, 16]
    assert record["grid"]["attributes"][TIME_STEP_ATTRIBUTE] == "month"


def test_create_can_publish_in_the_same_request(client: TestClient) -> None:
    created = create_coverage(client, publish=True)

    assert created["published"] is True
    record = client.get(f"/api/v1/datasets/{IDENTIFIER}").json()
    assert record["publication"]["snapshot_identifier"] == created["snapshot_identifier"]


def test_create_refuses_an_existing_identifier_without_overwrite(client: TestClient) -> None:
    create_coverage(client)

    response = client.post(f"/api/v1/raster/{IDENTIFIER}", json=CREATE_BODY)

    assert response.status_code == 409
    assert response.json()["error"] == "DatasetAlreadyExistsError"


def test_an_explicit_coordinate_reference_system_is_accepted(client: TestClient) -> None:
    created = create_coverage(client, crs="EPSG:4326")

    assert created["timestep_count"] == 3


def test_an_unknown_coordinate_reference_system_is_refused(client: TestClient) -> None:
    response = client.post(f"/api/v1/raster/{IDENTIFIER}", json={**CREATE_BODY, "crs": "EPSG:999999"})

    assert response.status_code == 422
    assert "coordinate reference system" in response.text


def test_append_continues_the_time_axis_with_the_same_step(client: TestClient) -> None:
    create_coverage(client)

    response = client.post(f"/api/v1/raster/{IDENTIFIER}/append", json={"timestep_count": 2, "seed": 7})

    assert response.status_code == 200, response.text
    assert response.json()["timestep_count"] == 5
    record = client.get(f"/api/v1/datasets/{IDENTIFIER}").json()
    assert record["temporal"]["start"].startswith("2020-01-01")
    assert record["temporal"]["end"].startswith("2020-05-01")


def test_append_can_publish_the_snapshot_it_wrote(client: TestClient) -> None:
    create_coverage(client)

    response = client.post(f"/api/v1/raster/{IDENTIFIER}/append", json={"publish": True})

    assert response.json()["published"] is True
    record = client.get(f"/api/v1/datasets/{IDENTIFIER}").json()
    assert record["publication"]["snapshot_identifier"] == response.json()["snapshot_identifier"]


def test_query_summarises_the_whole_cube(client: TestClient) -> None:
    create_coverage(client)

    response = client.get(f"/api/v1/raster/{IDENTIFIER}/query", params={"variable": VARIABLE})

    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["dataset_identifier"] == IDENTIFIER
    assert summary["variable"] == VARIABLE
    assert summary["crs"] == "EPSG:4326"
    assert summary["timestep_count"] == 3
    assert summary["cell_count"] == 3 * 8 * 16
    assert summary["minimum"] <= summary["mean"] <= summary["maximum"]


def test_a_bbox_query_reads_fewer_cells_than_the_whole_cube(client: TestClient) -> None:
    create_coverage(client)

    whole = client.get(f"/api/v1/raster/{IDENTIFIER}/query").json()
    window = client.get(f"/api/v1/raster/{IDENTIFIER}/query", params={"bbox": "0,0,45,45"}).json()

    assert window["cell_count"] < whole["cell_count"]
    assert window["bbox"]["minimum_x"] >= 0.0
    assert window["bbox"]["maximum_y"] <= 90.0
    assert window["mean"] != whole["mean"]


def test_a_time_window_narrows_the_query(client: TestClient) -> None:
    create_coverage(client)

    response = client.get(
        f"/api/v1/raster/{IDENTIFIER}/query",
        params={"start": "2020-02-01T00:00:00", "end": "2020-03-01T00:00:00"},
    )

    assert response.json()["timestep_count"] == 2


def test_a_malformed_bbox_is_refused(client: TestClient) -> None:
    create_coverage(client)

    response = client.get(f"/api/v1/raster/{IDENTIFIER}/query", params={"bbox": "0,0,45"})

    assert response.status_code == 400
    assert response.json()["error"] == "StorageAddressError"


def test_an_unknown_variable_is_refused(client: TestClient) -> None:
    create_coverage(client)

    response = client.get(f"/api/v1/raster/{IDENTIFIER}/query", params={"variable": "humidity"})

    assert response.status_code == 422
    assert response.json()["error"] == "RasterContractError"
    assert "humidity" in response.json()["detail"]


def test_publish_then_republish_reports_no_change(client: TestClient) -> None:
    create_coverage(client)

    published = client.post(f"/api/v1/raster/{IDENTIFIER}/publish")
    republished = client.post(f"/api/v1/raster/{IDENTIFIER}/publish")

    assert published.status_code == 200, published.text
    assert published.json()["published"] is True
    assert published.json()["changed"] is True
    assert republished.json()["changed"] is False


def test_versions_mark_the_published_snapshot(client: TestClient) -> None:
    created = create_coverage(client)
    client.post(f"/api/v1/raster/{IDENTIFIER}/append", json={"timestep_count": 1})
    client.post(f"/api/v1/raster/{IDENTIFIER}/publish")

    response = client.get(f"/api/v1/raster/{IDENTIFIER}/versions")

    items = response.json()["items"]
    assert [item["message"] for item in items[:2]] == ["append", "initial write"]
    assert items[0]["is_published"] is True
    assert items[1]["snapshot_identifier"] == created["snapshot_identifier"]
    assert len(client.get(f"/api/v1/raster/{IDENTIFIER}/versions", params={"limit": 1}).json()["items"]) == 1


def test_publishing_an_older_snapshot_rolls_the_coverage_back(client: TestClient) -> None:
    created = create_coverage(client)
    client.post(f"/api/v1/raster/{IDENTIFIER}/append", json={"timestep_count": 2})
    client.post(f"/api/v1/raster/{IDENTIFIER}/publish")

    rolled_back = client.post(
        f"/api/v1/raster/{IDENTIFIER}/publish",
        json={"snapshot_identifier": created["snapshot_identifier"]},
    )

    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["snapshot_identifier"] == created["snapshot_identifier"]
    published = client.get(f"/api/v1/raster/{IDENTIFIER}/query", params={"version": "published"}).json()
    draft = client.get(f"/api/v1/raster/{IDENTIFIER}/query", params={"version": "draft"}).json()
    assert published["timestep_count"] == 3
    assert draft["timestep_count"] == 5


def test_publishing_an_unknown_snapshot_is_refused(client: TestClient) -> None:
    create_coverage(client)

    response = client.post(f"/api/v1/raster/{IDENTIFIER}/publish", json={"snapshot_identifier": "ABSENTSNAPSHOT"})

    assert response.status_code == 404
    assert response.json()["error"] == "SnapshotNotFoundError"


def test_publishing_a_coverage_by_version_is_refused(client: TestClient) -> None:
    create_coverage(client)

    response = client.post(f"/api/v1/raster/{IDENTIFIER}/publish", json={"version": 1})

    assert response.status_code == 422
    assert response.json()["error"] == "PublicationSelectorError"


def test_naming_both_selectors_is_refused_by_the_request_model(client: TestClient) -> None:
    create_coverage(client)

    response = client.post(
        f"/api/v1/raster/{IDENTIFIER}/publish",
        json={"snapshot_identifier": "SOMESNAPSHOT", "version": 1},
    )

    assert response.status_code == 422


def test_delete_removes_the_coverage(client: TestClient) -> None:
    create_coverage(client)

    deleted = client.delete(f"/api/v1/datasets/{IDENTIFIER}")

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get(f"/api/v1/datasets/{IDENTIFIER}").status_code == 404
    assert client.get(f"/api/v1/raster/{IDENTIFIER}/query").status_code == 404


def test_an_unknown_coverage_is_reported_as_missing(client: TestClient) -> None:
    response = client.post("/api/v1/raster/absent/append", json={"timestep_count": 1})

    assert response.status_code == 404
    assert response.json()["error"] == "DatasetNotFoundError"


def test_an_append_request_falls_back_to_a_daily_step_for_an_unreadable_one() -> None:
    record = CoverageDataset(
        dataset_identifier=IDENTIFIER,
        title=IDENTIFIER,
        address="memory://memory/ocs/raster/coverage-api",
        publication=Publication(),
        grid=GridSpecification(
            shape=(2, 2),
            bbox=BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=2.0, maximum_y=2.0),
            crs="EPSG:4326",
            attributes={TIME_STEP_ATTRIBUTE: "fortnight"},
        ),
        variables=(VARIABLE,),
        temporal=TemporalExtent(start=datetime(2020, 1, 1), end=datetime(2020, 1, 2)),
        timestep_count=2,
    )

    cube = AppendRasterRequest(timestep_count=1).to_cube(record)

    assert [str(value)[:10] for value in cube[record.grid.time_dimension].values] == ["2020-01-03"]


def test_an_append_request_refuses_a_coverage_without_a_time_axis() -> None:
    record = CoverageDataset(
        dataset_identifier=IDENTIFIER,
        title=IDENTIFIER,
        address="memory://memory/ocs/raster/coverage-api",
        grid=GridSpecification(
            shape=(2, 2),
            bbox=BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=2.0, maximum_y=2.0),
            crs="EPSG:4326",
        ),
    )

    with pytest.raises(RasterContractError, match="no time axis"):
        AppendRasterRequest().to_cube(record)


def test_the_licence_and_attribution_reach_the_record(client: TestClient) -> None:
    body = {**CREATE_BODY, "license": "CC-BY-4.0", "attribution": "Open Climate Service"}

    assert client.post(f"/api/v1/raster/{IDENTIFIER}", json=body).status_code == 201
    record = client.get(f"/api/v1/datasets/{IDENTIFIER}").json()

    assert record["license"] == "CC-BY-4.0"
    assert record["attribution"] == "Open Climate Service"


def test_a_free_text_licence_is_refused(client: TestClient) -> None:
    body = {**CREATE_BODY, "license": "Creative Commons Attribution 4.0"}

    assert client.post(f"/api/v1/raster/{IDENTIFIER}", json=body).status_code == 422
