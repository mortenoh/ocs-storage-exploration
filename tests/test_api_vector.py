"""Tests for the vector router: write, feature reads, publication, rollback and delete."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from httpx import Response

from ocs_storage_exploration.api.schemas import FeatureCollectionResponse
from ocs_storage_exploration.main import create_app
from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.vector.collection import VectorReadHandle

IDENTIFIER = "collection-api"


def build_feature(identifier: str, level: int, x: float, y: float) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"id": identifier, "level": level},
        "geometry": {"type": "Polygon", "coordinates": [[[x, y], [x + 1, y], [x + 1, y + 1], [x, y + 1], [x, y]]]},
    }


FEATURE_COLLECTION: dict[str, Any] = {
    "type": "FeatureCollection",
    "features": [
        build_feature("west", 1, 0.0, 0.0),
        build_feature("middle", 1, 10.0, 0.0),
        build_feature("east", 2, 20.0, 0.0),
        build_feature("far-east", 2, 30.0, 0.0),
    ],
}
CREATE_BODY: dict[str, Any] = {
    "title": "API collection",
    "identifier_property": "id",
    "crs": "EPSG:4326",
    "selectable_columns": ["level"],
    "publish": True,
    "feature_collection": FEATURE_COLLECTION,
}


def create_collection(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post(f"/api/v1/vector/{IDENTIFIER}", json={**CREATE_BODY, **overrides})
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()
    return payload


@pytest.fixture
def guarded_client(settings: Settings) -> Iterator[TestClient]:
    guarded = settings.model_copy(update={"max_unqualified_feature_count": 2})
    with TestClient(create_app(settings=guarded)) as test_client:
        yield test_client


def test_create_writes_the_first_version_and_publishes_it(client: TestClient) -> None:
    created = create_collection(client)

    assert created == {
        "dataset_identifier": IDENTIFIER,
        "version": 1,
        "feature_count": 4,
        "published": True,
    }
    record = client.get(f"/api/v1/datasets/{IDENTIFIER}").json()
    assert record["item_type"] == "feature"
    assert record["crs"] == "EPSG:4326"
    assert record["features"]["identifier_property"] == "id"
    assert record["features"]["selectable_columns"] == ["level"]


def test_an_unknown_coordinate_reference_system_is_refused(client: TestClient) -> None:
    response = client.post(f"/api/v1/vector/{IDENTIFIER}", json={**CREATE_BODY, "crs": "not-a-crs"})

    assert response.status_code == 422
    assert "coordinate reference system" in response.text


def test_features_are_answered_as_geojson_in_the_collection_frame(client: TestClient) -> None:
    create_collection(client)

    response = client.get(f"/api/v1/vector/{IDENTIFIER}/features")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["type"] == "FeatureCollection"
    assert payload["number_returned"] == 4
    assert payload["number_matched"] == 4
    assert payload["version"] == 1
    assert payload["crs"] == "EPSG:4326"
    assert payload["truncated"] is False
    assert {feature["properties"]["id"] for feature in payload["features"]} == {
        "west",
        "middle",
        "east",
        "far-east",
    }


def test_the_geojson_body_is_rendered_off_the_event_loop(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    create_collection(client)
    threads: list[str] = []
    render = FeatureCollectionResponse.body_from_handle

    def recording_body_from_handle(handle: VectorReadHandle, *, limit: int | None = None) -> bytes:
        threads.append(threading.current_thread().name)
        return render(handle, limit=limit)

    monkeypatch.setattr(FeatureCollectionResponse, "body_from_handle", recording_body_from_handle)
    response = client.get(f"/api/v1/vector/{IDENTIFIER}/features")

    assert response.status_code == 200, response.text
    assert response.json()["number_returned"] == 4
    # The conversion and the JSON encoding are both as blocking as the read, so the worker thread that
    # produced the handle hands back the finished bytes. Returning the model instead left FastAPI
    # dumping, re-validating and encoding it on the event loop, where it stalls every other request.
    assert len(threads) == 1
    assert threads[0].startswith("AnyIO worker thread")


@pytest.mark.parametrize(
    "parameters",
    [
        pytest.param({}, id="every-feature"),
        pytest.param({"limit": 2}, id="a-limited-page"),
        pytest.param({"where": "level:9"}, id="an-empty-answer"),
    ],
)
def test_the_body_is_the_bytes_fastapi_serialised_before(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, parameters: dict[str, Any]
) -> None:
    create_collection(client)
    built: list[FeatureCollectionResponse] = []
    render = FeatureCollectionResponse.from_handle

    def capturing_from_handle(handle: VectorReadHandle, *, limit: int | None = None) -> FeatureCollectionResponse:
        model = render(handle, limit=limit)
        built.append(model)
        return model

    monkeypatch.setattr(FeatureCollectionResponse, "from_handle", capturing_from_handle)
    response = client.get(f"/api/v1/vector/{IDENTIFIER}/features", params=parameters)

    assert response.status_code == 200, response.text
    # What the route answered before it rendered its own body: the model through FastAPI's encoder and
    # the JSON response class it was returned to. The bytes are identical for a full page, a limited
    # one and an empty answer, down to the compact separators and the content type.
    expected = JSONResponse(content=jsonable_encoder(built[-1])).body
    assert response.content == expected
    assert response.headers["content-type"] == "application/json"


def test_the_feature_body_is_still_documented_as_the_response_model(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    answer = schema["paths"]["/api/v1/vector/{dataset_identifier}/features"]["get"]["responses"]["200"]
    # FastAPI documents a returned Response as an empty body unless the route declares its model, and
    # a client generated from this schema would then have no feature collection to deserialise into.
    assert answer["content"]["application/json"]["schema"]["$ref"].endswith("/FeatureCollectionResponse")
    assert "FeatureCollectionResponse" in schema["components"]["schemas"]


def test_a_bbox_narrows_the_feature_read(client: TestClient) -> None:
    create_collection(client)

    response = client.get(f"/api/v1/vector/{IDENTIFIER}/features", params={"bbox": "-1,-1,2,2"})

    assert [feature["properties"]["id"] for feature in response.json()["features"]] == ["west"]


def test_a_reprojected_bbox_narrows_the_feature_read(client: TestClient) -> None:
    create_collection(client)

    response = client.get(
        f"/api/v1/vector/{IDENTIFIER}/features",
        params={"bbox": "-111319,-111319,222639,222639", "bbox-crs": "EPSG:3857"},
    )

    assert [feature["properties"]["id"] for feature in response.json()["features"]] == ["west"]


def test_a_where_clause_narrows_the_feature_read(client: TestClient) -> None:
    create_collection(client)

    response = client.get(f"/api/v1/vector/{IDENTIFIER}/features", params={"where": "level:2"})

    assert {feature["properties"]["id"] for feature in response.json()["features"]} == {"east", "far-east"}


def test_an_undeclared_where_column_is_refused(client: TestClient) -> None:
    create_collection(client)

    response = client.get(f"/api/v1/vector/{IDENTIFIER}/features", params={"where": "title:west"})

    assert response.status_code == 400
    assert response.json()["error"] == "SelectableColumnError"


def test_columns_keep_the_identifier_and_the_geometry(client: TestClient) -> None:
    create_collection(client)

    response = client.get(f"/api/v1/vector/{IDENTIFIER}/features", params={"columns": "level"})

    feature = response.json()["features"][0]
    assert set(feature["properties"]) == {"id", "level"}
    assert feature["geometry"]["type"] == "Polygon"


def test_a_limit_truncates_and_hides_the_match_count(client: TestClient) -> None:
    create_collection(client)

    response = client.get(f"/api/v1/vector/{IDENTIFIER}/features", params={"limit": 2})

    payload = response.json()
    assert payload["number_returned"] == 2
    assert payload["number_matched"] is None
    assert payload["truncated"] is True


def test_publishing_an_older_version_rolls_the_collection_back(client: TestClient) -> None:
    create_collection(client)
    smaller = {"type": "FeatureCollection", "features": FEATURE_COLLECTION["features"][:2]}
    second = create_collection(client, feature_collection=smaller, publish=True)
    assert second["version"] == 2
    assert client.get(f"/api/v1/vector/{IDENTIFIER}/features").json()["number_returned"] == 2

    rolled_back = client.post(f"/api/v1/vector/{IDENTIFIER}/publish", json={"version": 1})

    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["version"] == 1
    assert rolled_back.json()["previous_version"] == 2
    assert client.get(f"/api/v1/vector/{IDENTIFIER}/features").json()["number_returned"] == 4
    assert client.get(f"/api/v1/vector/{IDENTIFIER}/features", params={"version": 2}).json()["version"] == 2


def test_publishing_without_a_body_publishes_the_latest_version(client: TestClient) -> None:
    create_collection(client, publish=False)

    response = client.post(f"/api/v1/vector/{IDENTIFIER}/publish")

    assert response.json()["published"] is True
    assert response.json()["version"] == 1


def test_publishing_a_collection_by_snapshot_is_refused(client: TestClient) -> None:
    create_collection(client)

    response = client.post(f"/api/v1/vector/{IDENTIFIER}/publish", json={"snapshot_identifier": "SOMESNAPSHOT"})

    assert response.status_code == 422
    assert response.json()["error"] == "PublicationSelectorError"


def test_a_duplicate_identifier_names_the_offending_value(client: TestClient) -> None:
    duplicated = {
        "type": "FeatureCollection",
        "features": [*FEATURE_COLLECTION["features"], build_feature("west", 3, 40.0, 0.0)],
    }

    response = client.post(f"/api/v1/vector/{IDENTIFIER}", json={**CREATE_BODY, "feature_collection": duplicated})

    assert response.status_code == 422
    assert response.json()["error"] == "FeatureIdentityError"
    assert "west" in response.json()["detail"]


def test_an_unqualified_read_over_the_threshold_is_refused(guarded_client: TestClient) -> None:
    create_collection(guarded_client)

    refused = guarded_client.get(f"/api/v1/vector/{IDENTIFIER}/features")
    qualified = guarded_client.get(f"/api/v1/vector/{IDENTIFIER}/features", params={"where": "level:1"})

    assert refused.status_code == 413
    assert refused.json()["error"] == "FeatureCountGuardError"
    assert qualified.status_code == 200
    assert qualified.json()["number_returned"] == 2


def test_delete_removes_the_collection(client: TestClient) -> None:
    create_collection(client)

    deleted = client.delete(f"/api/v1/datasets/{IDENTIFIER}")

    assert deleted.status_code == 204
    assert client.get(f"/api/v1/datasets/{IDENTIFIER}").status_code == 404
    assert client.get(f"/api/v1/vector/{IDENTIFIER}/features").status_code == 404


def test_an_unknown_collection_is_reported_as_missing(client: TestClient) -> None:
    response = client.get("/api/v1/vector/absent/features")

    assert response.status_code == 404
    assert response.json()["error"] == "DatasetNotFoundError"


def test_the_licence_and_attribution_reach_the_collection_record(client: TestClient) -> None:
    body = {**CREATE_BODY, "license": "proprietary", "attribution": "Statistics Norway"}

    assert client.post(f"/api/v1/vector/{IDENTIFIER}", json=body).status_code == 201
    record = client.get(f"/api/v1/datasets/{IDENTIFIER}").json()

    assert record["license"] == "proprietary"
    assert record["attribution"] == "Statistics Norway"


def test_a_free_text_licence_is_refused_on_a_collection(client: TestClient) -> None:
    body = {**CREATE_BODY, "license": "Creative Commons Attribution 4.0"}

    assert client.post(f"/api/v1/vector/{IDENTIFIER}", json=body).status_code == 422


def validation_locations(response: Response) -> list[str]:
    return [".".join(str(part) for part in error["loc"]) for error in response.json()["detail"]]


def post_features(client: TestClient, features: list[Any]) -> Response:
    collection = {"type": "FeatureCollection", "features": features}
    response: Response = client.post(
        f"/api/v1/vector/{IDENTIFIER}", json={**CREATE_BODY, "feature_collection": collection}
    )
    return response


def test_a_null_feature_is_refused_with_its_position(client: TestClient) -> None:
    response = post_features(client, [FEATURE_COLLECTION["features"][0], None])

    assert response.status_code == 422
    assert "body.feature_collection.features.1" in validation_locations(response)


def test_a_feature_without_a_geometry_is_refused_with_its_position(client: TestClient) -> None:
    response = post_features(client, [{"type": "Feature", "properties": {"id": "west", "level": 1}}])

    assert response.status_code == 422
    assert "body.feature_collection.features.0.geometry" in validation_locations(response)


def test_feature_properties_that_are_not_an_object_are_refused(client: TestClient) -> None:
    broken = {"type": "Feature", "properties": "not an object", "geometry": {"type": "Point", "coordinates": [0, 0]}}

    response = post_features(client, [broken])

    assert response.status_code == 422
    locations = validation_locations(response)
    assert any(location.startswith("body.feature_collection.features.0.properties") for location in locations)


def test_a_geometry_without_coordinates_is_refused(client: TestClient) -> None:
    response = post_features(client, [{"type": "Feature", "properties": {"id": "west"}, "geometry": {"type": "Point"}}])

    assert response.status_code == 422
    assert "body.feature_collection.features.0.geometry.Point.coordinates" in validation_locations(response)


def test_a_geometry_with_unreadable_coordinates_is_refused(client: TestClient) -> None:
    broken = {"type": "Feature", "properties": {"id": "west"}, "geometry": {"type": "Point", "coordinates": "here"}}

    response = post_features(client, [broken])

    assert response.status_code == 422
    locations = validation_locations(response)
    assert any(location.startswith("body.feature_collection.features.0.geometry") for location in locations)


def test_a_null_geometry_is_refused_by_the_collection_store(client: TestClient) -> None:
    broken = {"type": "Feature", "properties": {"id": "west"}, "geometry": None}

    response = post_features(client, [broken])

    assert response.status_code == 422
    assert response.json()["error"] == "VectorInputError"
    assert "feature 0" in response.json()["detail"]


def test_an_empty_feature_collection_is_refused(client: TestClient) -> None:
    response = post_features(client, [])

    assert response.status_code == 422
    assert response.json()["error"] == "FeatureIdentityError"


def test_an_unknown_bbox_crs_is_refused(client: TestClient) -> None:
    create_collection(client)

    response = client.get(
        f"/api/v1/vector/{IDENTIFIER}/features",
        params={"bbox": "0,0,1,1", "bbox-crs": "not-a-crs"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "CrsError"
    assert "bbox-crs" in response.json()["detail"]


def test_an_out_of_range_bbox_crs_is_refused(client: TestClient) -> None:
    create_collection(client)

    response = client.get(
        f"/api/v1/vector/{IDENTIFIER}/features",
        params={"bbox": "0,0,1,1", "bbox-crs": "EPSG:999999"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "CrsError"


def test_an_out_of_range_crs_is_refused_in_the_create_body(client: TestClient) -> None:
    response = client.post(f"/api/v1/vector/{IDENTIFIER}", json={**CREATE_BODY, "crs": "EPSG:999999"})

    assert response.status_code == 422
    assert "coordinate reference system" in response.text
