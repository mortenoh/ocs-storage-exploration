"""Tests for the STAC projection: extents, extension fields, assets, licence and draft exclusion."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import geopandas
import pytest

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.addresses import StorageScheme
from ocs_storage_exploration.storage.errors import SnapshotNotFoundError
from ocs_storage_exploration.storage.raster import TimeStep, build_synthetic_cube, build_timestamps
from ocs_storage_exploration.storage.schemas import BoundingBox, GridSpecification
from ocs_storage_exploration.storage.service import StorageService
from ocs_storage_exploration.storage.stac import (
    CATALOG_IDENTIFIER,
    CONFORMANCE_CLASSES,
    DATACUBE_EXTENSION,
    DEFAULT_LICENSE,
    PARQUET_MEDIA_TYPE,
    STAC_VERSION,
    TABLE_EXTENSION,
    ZARR_V3_MEDIA_TYPE,
    build_catalog,
    build_collection,
    wgs84_bounds,
)

BASE_URL = "https://example.test"
COVERAGE = "stac-coverage"
COLLECTION = "stac-collection"
VARIABLE = "temperature"
START = datetime(2020, 1, 1)
# A one degree box around Oslo, projected into Web Mercator by hand so the reprojection has
# something to undo rather than a bbox that is the same in both frames.
MERCATOR_BBOX = BoundingBox(
    minimum_x=1_113_194.907_932_735_7,
    minimum_y=8_289_249.926_586_546,
    maximum_x=1_224_514.398_726_009_3,
    maximum_y=8_399_737.889_818_357,
)


def build_grid(crs: str = "EPSG:4326", bbox: BoundingBox | None = None) -> GridSpecification:
    return GridSpecification(
        shape=(4, 6),
        bbox=bbox if bbox is not None else BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=12.0, maximum_y=8.0),
        crs=crs,
    )


def write_coverage(
    storage_service: StorageService,
    *,
    grid: GridSpecification | None = None,
    license: str | None = "CC-BY-4.0",
    attribution: str | None = "Open Climate Service",
    publish: bool = True,
) -> None:
    resolved = grid if grid is not None else build_grid()
    cube = build_synthetic_cube(
        resolved,
        variable=VARIABLE,
        timestamps=build_timestamps(START, 3, TimeStep.DAY),
        seed=0,
    )
    result = storage_service.raster.create(
        COVERAGE,
        resolved,
        cube,
        title="Synthetic temperature",
        license=license,
        attribution=attribution,
    )
    if publish:
        storage_service.raster.publish(COVERAGE, snapshot_identifier=result.snapshot_identifier)


def write_collection(
    storage_service: StorageService,
    frame: geopandas.GeoDataFrame,
    *,
    license: str | None = "proprietary",
    attribution: str | None = "Statistics Norway",
    publish: bool = True,
) -> None:
    storage_service.vector.write(
        COLLECTION,
        frame,
        identifier_property="id",
        title="Demo districts",
        license=license,
        attribution=attribution,
        selectable_columns=("level",),
        publish=publish,
    )


def project(storage_service: StorageService, dataset_identifier: str) -> dict[str, Any]:
    record = storage_service.get_dataset(dataset_identifier)
    return build_collection(record, base_url=BASE_URL, service=storage_service)


@pytest.fixture
def coverage_collection(storage_service: StorageService) -> dict[str, Any]:
    write_coverage(storage_service)
    return project(storage_service, COVERAGE)


@pytest.fixture
def feature_collection(storage_service: StorageService, sample_features: geopandas.GeoDataFrame) -> dict[str, Any]:
    write_collection(storage_service, sample_features)
    return project(storage_service, COLLECTION)


def test_the_extension_versions_are_the_ones_the_design_pins() -> None:
    assert DATACUBE_EXTENSION == "https://stac-extensions.github.io/datacube/v2.2.0/schema.json"
    assert TABLE_EXTENSION == "https://stac-extensions.github.io/table/v1.2.0/schema.json"


def test_a_coverage_projects_onto_a_datacube_collection(coverage_collection: dict[str, Any]) -> None:
    assert coverage_collection["type"] == "Collection"
    assert coverage_collection["stac_version"] == STAC_VERSION
    assert coverage_collection["id"] == COVERAGE
    assert coverage_collection["title"] == "Synthetic temperature"
    assert coverage_collection["stac_extensions"] == [DATACUBE_EXTENSION]
    assert coverage_collection["ocs:item_type"] == "coverage"
    assert coverage_collection["ocs:snapshot_identifier"]


def test_the_coverage_extent_carries_the_grid_and_the_time_axis(coverage_collection: dict[str, Any]) -> None:
    assert coverage_collection["extent"]["spatial"]["bbox"] == [[0.0, 0.0, 12.0, 8.0]]
    assert coverage_collection["extent"]["temporal"]["interval"] == [
        ["2020-01-01T00:00:00Z", "2020-01-03T00:00:00Z"],
    ]


def test_the_cube_dimensions_name_the_three_axes(coverage_collection: dict[str, Any]) -> None:
    dimensions = coverage_collection["cube:dimensions"]

    assert set(dimensions) == {"x", "y", "t"}
    assert dimensions["x"] == {"type": "spatial", "axis": "x", "extent": [0.0, 12.0], "reference_system": 4326}
    assert dimensions["y"] == {"type": "spatial", "axis": "y", "extent": [0.0, 8.0], "reference_system": 4326}
    assert dimensions["t"] == {"type": "temporal", "extent": ["2020-01-01T00:00:00Z", "2020-01-03T00:00:00Z"]}


def test_every_variable_becomes_a_cube_variable(coverage_collection: dict[str, Any]) -> None:
    assert coverage_collection["cube:variables"] == {VARIABLE: {"dimensions": ["t", "y", "x"], "type": "data"}}


def test_the_coverage_assets_are_the_repository_and_the_query_endpoint(coverage_collection: dict[str, Any]) -> None:
    icechunk = coverage_collection["assets"]["icechunk"]
    api = coverage_collection["assets"]["api"]

    assert icechunk["type"] == ZARR_V3_MEDIA_TYPE == "application/vnd.zarr; version=3"
    assert icechunk["roles"] == ["data"]
    assert icechunk["icechunk:branch"] == "published"
    assert icechunk["href"].endswith(f"raster/{COVERAGE}")
    assert api["href"] == f"{BASE_URL}/api/v1/raster/{COVERAGE}/query"
    assert api["type"] == "application/json"
    assert api["roles"] == ["metadata"]


def test_the_coverage_links_point_back_at_the_catalog(coverage_collection: dict[str, Any]) -> None:
    links = {link["rel"]: link["href"] for link in coverage_collection["links"]}

    assert links["self"] == f"{BASE_URL}/stac/collections/{COVERAGE}"
    assert links["root"] == f"{BASE_URL}/stac"
    assert links["parent"] == f"{BASE_URL}/stac"


def test_the_licence_and_the_attribution_reach_the_collection(coverage_collection: dict[str, Any]) -> None:
    assert coverage_collection["license"] == "CC-BY-4.0"
    assert coverage_collection["providers"] == [
        {"name": "Open Climate Service", "roles": ["producer", "licensor"]},
    ]


def test_a_record_without_a_licence_falls_back_to_other(storage_service: StorageService) -> None:
    write_coverage(storage_service, license=None, attribution=None)

    payload = project(storage_service, COVERAGE)

    assert payload["license"] == DEFAULT_LICENSE == "other"
    assert "providers" not in payload


def test_a_projected_grid_is_advertised_in_wgs84(storage_service: StorageService) -> None:
    write_coverage(storage_service, grid=build_grid(crs="EPSG:3857", bbox=MERCATOR_BBOX))

    payload = project(storage_service, COVERAGE)
    west, south, east, north = payload["extent"]["spatial"]["bbox"][0]

    assert west == pytest.approx(10.0, abs=0.001)
    assert east == pytest.approx(11.0, abs=0.001)
    assert south == pytest.approx(59.5, abs=0.001)
    assert north == pytest.approx(60.0, abs=0.001)
    # The cube dimensions stay in the frame the cube was written on, which reference_system names.
    assert payload["cube:dimensions"]["x"]["reference_system"] == 3857
    # The extent is measured on the coordinates of the opened store, so it lands within a cell
    # rounding of the envelope the grid was written from rather than on the identical float.
    assert payload["cube:dimensions"]["x"]["extent"] == pytest.approx(
        [MERCATOR_BBOX.minimum_x, MERCATOR_BBOX.maximum_x],
    )


def test_wgs84_bounds_leaves_a_geographic_envelope_alone() -> None:
    bbox = BoundingBox(minimum_x=-180.0, minimum_y=-90.0, maximum_x=180.0, maximum_y=90.0)

    assert wgs84_bounds(bbox, "EPSG:4326") == (-180.0, -90.0, 180.0, 90.0)


def test_a_collection_projects_onto_a_table_collection(feature_collection: dict[str, Any]) -> None:
    assert feature_collection["type"] == "Collection"
    assert feature_collection["stac_version"] == STAC_VERSION
    assert feature_collection["id"] == COLLECTION
    assert feature_collection["stac_extensions"] == [TABLE_EXTENSION]
    assert feature_collection["ocs:item_type"] == "feature"
    assert feature_collection["ocs:version"] == 1


def test_the_table_fields_describe_the_published_parquet(feature_collection: dict[str, Any]) -> None:
    columns = {column["name"]: column["type"] for column in feature_collection["table:columns"]}

    assert feature_collection["table:row_count"] == 12
    assert feature_collection["table:primary_geometry"] == "geometry"
    assert columns["id"] == "string"
    assert columns["level"] == "int64"
    assert columns["geometry"] == "binary"


def test_the_feature_collection_has_no_time_axis(feature_collection: dict[str, Any]) -> None:
    assert feature_collection["extent"]["temporal"]["interval"] == [[None, None]]
    assert feature_collection["extent"]["spatial"]["bbox"] == [[0.0, 0.0, 21.5, 21.5]]


def test_the_feature_assets_are_the_parquet_and_the_features_endpoint(feature_collection: dict[str, Any]) -> None:
    data = feature_collection["assets"]["data"]
    api = feature_collection["assets"]["api"]

    assert data["type"] == PARQUET_MEDIA_TYPE == "application/x-parquet"
    assert data["roles"] == ["data"]
    assert data["href"].endswith(f"vector/{COLLECTION}/versions/v00001/data.parquet")
    assert api["href"] == f"{BASE_URL}/api/v1/vector/{COLLECTION}/features"
    assert api["roles"] == ["metadata"]


def test_the_published_version_is_the_one_advertised(
    storage_service: StorageService,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    write_collection(storage_service, sample_features)
    storage_service.vector.write(COLLECTION, sample_features.iloc[:4], identifier_property="id")

    payload = project(storage_service, COLLECTION)

    assert payload["ocs:version"] == 1
    assert payload["assets"]["data"]["href"].endswith("versions/v00001/data.parquet")


def test_the_feature_licence_and_attribution_reach_the_collection(feature_collection: dict[str, Any]) -> None:
    assert feature_collection["license"] == "proprietary"
    assert feature_collection["providers"] == [
        {"name": "Statistics Norway", "roles": ["producer", "licensor"]},
    ]


def test_the_catalog_links_to_the_collections_endpoint_and_every_record(
    storage_service: StorageService,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    write_coverage(storage_service)
    write_collection(storage_service, sample_features)

    payload = build_catalog(storage_service.list_datasets(), base_url=BASE_URL)
    links = [(link["rel"], link["href"]) for link in payload["links"]]

    assert payload["type"] == "Catalog"
    assert payload["id"] == CATALOG_IDENTIFIER
    assert payload["stac_version"] == STAC_VERSION
    assert payload["conformsTo"] == list(CONFORMANCE_CLASSES)
    assert ("self", f"{BASE_URL}/stac") in links
    assert ("root", f"{BASE_URL}/stac") in links
    assert ("data", f"{BASE_URL}/stac/collections") in links
    assert ("child", f"{BASE_URL}/stac/collections/{COVERAGE}") in links
    assert ("child", f"{BASE_URL}/stac/collections/{COLLECTION}") in links


def test_a_draft_only_catalog_advertises_nothing(
    storage_service: StorageService,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    write_coverage(storage_service, publish=False)
    write_collection(storage_service, sample_features, publish=False)

    published = [record for record in storage_service.list_datasets() if record.publication.published]
    payload = build_catalog(published, base_url=BASE_URL)

    assert [link for link in payload["links"] if link["rel"] == "child"] == []


def test_a_draft_collection_still_projects_with_its_newest_written_version(
    storage_service: StorageService,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    write_collection(storage_service, sample_features, publish=False)
    storage_service.vector.write(COLLECTION, sample_features.iloc[:4], identifier_property="id")

    payload = project(storage_service, COLLECTION)

    assert payload["ocs:version"] == 2
    assert payload["assets"]["data"]["href"].endswith("versions/v00002/data.parquet")


def test_the_row_count_follows_the_published_version_rather_than_the_newest_write(
    storage_service: StorageService,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    write_collection(storage_service, sample_features)
    storage_service.vector.write(COLLECTION, sample_features.iloc[:4], identifier_property="id")

    # The record's feature detail now says four, because it tracks the newest write; the
    # published version still holds twelve, and that is what the collection has to advertise.
    record = storage_service.require_collection(COLLECTION)
    payload = project(storage_service, COLLECTION)

    assert record.features.feature_count == 4
    assert payload["ocs:version"] == 1
    assert payload["table:row_count"] == 12


def test_an_unpublished_append_leaves_the_published_temporal_extent_alone(
    storage_service: StorageService,
) -> None:
    grid = build_grid()
    write_coverage(storage_service, grid=grid)
    published = project(storage_service, COVERAGE)["extent"]["temporal"]["interval"]

    storage_service.raster.append(
        COVERAGE,
        build_synthetic_cube(
            grid,
            variable=VARIABLE,
            timestamps=build_timestamps(datetime(2020, 1, 4), 2, TimeStep.DAY),
            seed=1,
        ),
    )

    payload = project(storage_service, COVERAGE)
    record = storage_service.require_coverage(COVERAGE)

    # The record tracks the newest write; the collection describes the snapshot the published
    # branch points at, which the append did not move.
    assert record.timestep_count == 5
    assert payload["extent"]["temporal"]["interval"] == published == [["2020-01-01T00:00:00Z", "2020-01-03T00:00:00Z"]]
    assert payload["cube:dimensions"]["t"]["extent"] == ["2020-01-01T00:00:00Z", "2020-01-03T00:00:00Z"]


def test_a_draft_written_in_another_frame_leaves_the_published_collection_alone(
    storage_service: StorageService,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    write_collection(storage_service, sample_features)

    storage_service.vector.write(
        COLLECTION,
        sample_features.iloc[:4].to_crs("EPSG:3857"),
        identifier_property="id",
    )

    payload = project(storage_service, COLLECTION)
    record = storage_service.require_collection(COLLECTION)

    assert record.crs == "EPSG:3857"
    assert payload["ocs:version"] == 1
    assert payload["table:row_count"] == 12
    assert payload["extent"]["spatial"]["bbox"] == [[0.0, 0.0, 21.5, 21.5]]
    assert "EPSG:4326" in payload["description"]


def test_a_coverage_whose_store_cannot_be_read_falls_back_to_its_record(
    storage_service: StorageService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_coverage(storage_service)

    def refuse(*arguments: Any, **keywords: Any) -> None:
        raise SnapshotNotFoundError("the store cannot be opened")

    monkeypatch.setattr(storage_service.raster, "describe", refuse)
    payload = project(storage_service, COVERAGE)

    assert payload["extent"]["spatial"]["bbox"] == [[0.0, 0.0, 12.0, 8.0]]
    assert payload["extent"]["temporal"]["interval"] == [["2020-01-01T00:00:00Z", "2020-01-03T00:00:00Z"]]
    assert payload["cube:variables"] == {VARIABLE: {"dimensions": ["t", "y", "x"], "type": "data"}}
    assert payload["ocs:snapshot_identifier"]
    # With no snapshot to read, the record is the documented fallback for the licence too.
    assert payload["license"] == "CC-BY-4.0"
    assert payload["providers"] == [{"name": "Open Climate Service", "roles": ["producer", "licensor"]}]


def test_a_collection_whose_parquet_cannot_be_read_falls_back_to_its_record(
    storage_service: StorageService,
    sample_features: geopandas.GeoDataFrame,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_collection(storage_service, sample_features)

    def refuse(*arguments: Any, **keywords: Any) -> None:
        raise SnapshotNotFoundError("the parquet cannot be opened")

    monkeypatch.setattr(storage_service.vector, "table_schema", refuse)
    payload = project(storage_service, COLLECTION)

    assert payload["table:row_count"] == 12
    assert payload["table:columns"] == []
    assert payload["table:primary_geometry"] == "geometry"
    assert "data" not in payload["assets"]
    assert "ocs:version" not in payload
    # With no version to read, the record is the documented fallback for the licence too.
    assert payload["license"] == "proprietary"
    assert payload["providers"] == [{"name": "Statistics Norway", "roles": ["producer", "licensor"]}]


def test_the_collection_licence_follows_the_published_version_not_the_newest_write(
    storage_service: StorageService,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    write_collection(
        storage_service,
        sample_features,
        license="CC-BY-4.0",
        attribution="Open Climate Service",
    )
    storage_service.vector.write(
        COLLECTION,
        sample_features,
        identifier_property="id",
        license="proprietary",
        attribution="Statistics Norway",
    )

    payload = project(storage_service, COLLECTION)
    record = storage_service.require_collection(COLLECTION)

    # The record tracks the newest write; the collection advertises the terms the published bytes
    # were written under, which is what a client that downloads them is bound by.
    assert record.license == "proprietary"
    assert record.attribution == "Statistics Norway"
    assert payload["ocs:version"] == 1
    assert payload["license"] == "CC-BY-4.0"
    assert payload["providers"] == [{"name": "Open Climate Service", "roles": ["producer", "licensor"]}]


def test_the_coverage_licence_follows_the_published_snapshot_not_the_newest_write(
    storage_service: StorageService,
) -> None:
    write_coverage(storage_service, license="CC-BY-4.0", attribution="Open Climate Service")
    grid = build_grid()
    storage_service.raster.create(
        COVERAGE,
        grid,
        build_synthetic_cube(grid, variable=VARIABLE, timestamps=build_timestamps(START, 3, TimeStep.DAY), seed=1),
        overwrite=True,
        license="proprietary",
        attribution="Statistics Norway",
    )

    payload = project(storage_service, COVERAGE)
    record = storage_service.require_coverage(COVERAGE)

    # The record tracks the newest write; the collection advertises the terms the published snapshot
    # was committed under, which is what a client that opens that branch is bound by.
    assert record.license == "proprietary"
    assert record.attribution == "Statistics Norway"
    assert payload["license"] == "CC-BY-4.0"
    assert payload["providers"] == [{"name": "Open Climate Service", "roles": ["producer", "licensor"]}]


def test_a_data_directory_that_moved_serves_asset_hrefs_from_its_new_root(
    tmp_path: Path,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    first = tmp_path / "first"
    written = StorageService.from_settings(
        Settings(backend=StorageScheme.FILE, data_directory=first, base_prefix="ocs"),
    )
    write_coverage(written)
    write_collection(written, sample_features)
    second = tmp_path / "second"
    shutil.copytree(first, second)

    moved = StorageService.from_settings(
        Settings(backend=StorageScheme.FILE, data_directory=second, base_prefix="ocs"),
    )
    coverage = build_collection(moved.get_dataset(COVERAGE), base_url=BASE_URL, service=moved)
    collection = build_collection(moved.get_dataset(COLLECTION), base_url=BASE_URL, service=moved)

    # A record holds a key below the backend and never a root, so the same bytes served from another
    # directory, or from inside a container, advertise where they actually are.
    root = f"file://{second.resolve()}/ocs/"
    assert coverage["assets"]["icechunk"]["href"].startswith(root)
    assert collection["assets"]["data"]["href"].startswith(root)
