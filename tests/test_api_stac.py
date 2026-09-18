"""Tests for the STAC router: the landing page, the listing, one collection and the draft filter."""

from __future__ import annotations

from typing import Any

import pystac
import pytest
from fastapi.testclient import TestClient

from ocs_storage_exploration.main import create_app
from ocs_storage_exploration.settings import Settings

COVERAGE = "stac-api-coverage"
COLLECTION = "stac-api-collection"
DRAFT = "stac-api-draft"
DRAFT_COLLECTION = "stac-api-draft-collection"

RASTER_BODY: dict[str, Any] = {
    "title": "Synthetic temperature",
    "license": "CC-BY-4.0",
    "attribution": "Open Climate Service",
    "shape": [4, 8],
    "timestep_count": 2,
    "publish": True,
}
VECTOR_BODY: dict[str, Any] = {
    "title": "Demo districts",
    "license": "proprietary",
    "attribution": "Statistics Norway",
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
def populated_client(client: TestClient) -> TestClient:
    assert client.post(f"/api/v1/raster/{COVERAGE}", json=RASTER_BODY).status_code == 201
    assert client.post(f"/api/v1/vector/{COLLECTION}", json=VECTOR_BODY).status_code == 201
    assert client.post(f"/api/v1/raster/{DRAFT}", json={**RASTER_BODY, "publish": False}).status_code == 201
    draft_collection = {**VECTOR_BODY, "publish": False}
    assert client.post(f"/api/v1/vector/{DRAFT_COLLECTION}", json=draft_collection).status_code == 201
    return client


def draft_collection_payload(client: TestClient, dataset_identifier: str) -> dict[str, Any]:
    response = client.get(f"/stac/collections/{dataset_identifier}", params={"published_only": False})
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def test_the_landing_page_names_the_conformance_classes(populated_client: TestClient) -> None:
    response = populated_client.get("/stac")

    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "Catalog"
    assert payload["stac_version"] == "1.1.0"
    assert payload["conformsTo"] == [
        "https://api.stacspec.org/v1.0.0/core",
        "https://api.stacspec.org/v1.0.0/collections",
    ]


def test_the_landing_page_links_to_the_collections_endpoint(populated_client: TestClient) -> None:
    links = {link["rel"]: link["href"] for link in populated_client.get("/stac").json()["links"]}

    assert links["self"] == "http://testserver/stac"
    assert links["data"] == "http://testserver/stac/collections"


def test_the_landing_page_lists_only_published_datasets(populated_client: TestClient) -> None:
    published = populated_client.get("/stac").json()["links"]
    everything = populated_client.get("/stac", params={"published_only": False}).json()["links"]

    assert [link["href"] for link in published if link["rel"] == "child"] == [
        f"http://testserver/stac/collections/{COLLECTION}",
        f"http://testserver/stac/collections/{COVERAGE}",
    ]
    assert f"http://testserver/stac/collections/{DRAFT}" in [
        link["href"] for link in everything if link["rel"] == "child"
    ]


def test_the_listing_answers_both_item_types(populated_client: TestClient) -> None:
    response = populated_client.get("/stac/collections")

    assert response.status_code == 200
    payload = response.json()
    assert [collection["id"] for collection in payload["collections"]] == [COLLECTION, COVERAGE]
    assert {collection["ocs:item_type"] for collection in payload["collections"]} == {"coverage", "feature"}
    assert {link["rel"] for link in payload["links"]} == {"self", "root", "parent"}


def test_one_collection_can_be_read_by_identifier(populated_client: TestClient) -> None:
    response = populated_client.get(f"/stac/collections/{COVERAGE}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == COVERAGE
    assert payload["license"] == "CC-BY-4.0"
    assert payload["providers"][0]["name"] == "Open Climate Service"
    assert payload["stac_extensions"] == ["https://stac-extensions.github.io/datacube/v2.2.0/schema.json"]


def test_an_unknown_collection_is_reported_as_missing(populated_client: TestClient) -> None:
    response = populated_client.get("/stac/collections/absent")

    assert response.status_code == 404
    assert response.json()["error"] == "DatasetNotFoundError"


def test_an_unpublished_collection_is_hidden_until_drafts_are_asked_for(populated_client: TestClient) -> None:
    hidden = populated_client.get(f"/stac/collections/{DRAFT}")
    shown = populated_client.get(f"/stac/collections/{DRAFT}", params={"published_only": False})

    assert hidden.status_code == 404
    assert hidden.json()["error"] == "DatasetNotFoundError"
    assert shown.status_code == 200
    assert shown.json()["id"] == DRAFT


def test_the_selectors_of_a_draft_coverage_resolve_to_the_advertised_snapshot(populated_client: TestClient) -> None:
    payload = draft_collection_payload(populated_client, DRAFT)
    icechunk = payload["assets"]["icechunk"]

    followed = populated_client.get(payload["assets"]["api"]["href"])

    # The branch a draft asset names has to be one the repository actually has, and following the
    # endpoint href has to answer the snapshot the document describes rather than 404 on a branch
    # a coverage with nothing published never created.
    assert icechunk["icechunk:branch"] == "main"
    assert icechunk["ocs:snapshot_identifier"] == payload["ocs:snapshot_identifier"]
    assert followed.status_code == 200, followed.text
    assert followed.json()["snapshot_identifier"] == payload["ocs:snapshot_identifier"]


def test_the_published_coverage_selectors_still_follow_the_published_branch(populated_client: TestClient) -> None:
    payload = populated_client.get(f"/stac/collections/{COVERAGE}").json()

    followed = populated_client.get(payload["assets"]["api"]["href"])

    assert payload["assets"]["icechunk"]["icechunk:branch"] == "published"
    assert payload["assets"]["api"]["href"] == f"http://testserver/api/v1/raster/{COVERAGE}/query"
    assert followed.status_code == 200, followed.text
    assert followed.json()["snapshot_identifier"] == payload["ocs:snapshot_identifier"]


def test_the_features_href_of_a_draft_collection_resolves_to_the_advertised_version(
    populated_client: TestClient,
) -> None:
    payload = draft_collection_payload(populated_client, DRAFT_COLLECTION)

    followed = populated_client.get(payload["assets"]["api"]["href"])

    assert payload["assets"]["api"]["href"] == (
        f"http://testserver/api/v1/vector/{DRAFT_COLLECTION}/features?version={payload['ocs:version']}"
    )
    assert followed.status_code == 200, followed.text
    assert followed.json()["version"] == payload["ocs:version"]


def test_the_base_url_follows_the_root_path(settings: Settings) -> None:
    with TestClient(create_app(settings=settings), root_path="/storage") as mounted:
        assert mounted.post(f"/api/v1/raster/{COVERAGE}", json=RASTER_BODY).status_code == 201
        links = {link["rel"]: link["href"] for link in mounted.get("/stac").json()["links"]}
        collection = mounted.get(f"/stac/collections/{COVERAGE}").json()

    assert links["self"] == "http://testserver/storage/stac"
    assert links["data"] == "http://testserver/storage/stac/collections"
    assert collection["assets"]["api"]["href"] == f"http://testserver/storage/api/v1/raster/{COVERAGE}/query"


@pytest.mark.parametrize("dataset_identifier", [COVERAGE, COLLECTION])
def test_a_collection_round_trips_through_pystac(populated_client: TestClient, dataset_identifier: str) -> None:
    payload = populated_client.get(f"/stac/collections/{dataset_identifier}").json()

    collection = pystac.Collection.from_dict(payload)

    assert collection.id == dataset_identifier
    assert collection.STAC_OBJECT_TYPE == pystac.STACObjectType.COLLECTION
    assert collection.to_dict(include_self_link=True, transform_hrefs=False)["extent"] == payload["extent"]


@pytest.mark.skip(
    reason=(
        "pystac.validation.validate_dict needs the jsonschema extra and fetches every schema over "
        "HTTP, so it cannot run in an offline suite; the structural check above stands in for it "
        "and the schemas were validated once by hand, as docs/concepts/stac-catalog.md records"
    ),
)
@pytest.mark.parametrize("dataset_identifier", [COVERAGE, COLLECTION])
def test_a_collection_validates_against_its_schemas(populated_client: TestClient, dataset_identifier: str) -> None:
    import pystac.validation

    payload = populated_client.get(f"/stac/collections/{dataset_identifier}").json()

    pystac.validation.validate_dict(payload)
