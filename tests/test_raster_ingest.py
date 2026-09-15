"""Tests of the raster ingest normalisation rules and of ingesting the committed sample rasters."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy
import pytest
from fastapi.testclient import TestClient

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.errors import RasterContractError
from ocs_storage_exploration.storage.raster import NODATA_ATTRIBUTE, PROJECTION_CODE_ATTRIBUTE, SPATIAL_REFERENCE_NAME
from ocs_storage_exploration.storage.raster.ingest import (
    build_raster_ingest_plan,
    grid_from_dataset,
    ingest_raster_files,
    open_raster_file,
    timestamp_from_filename,
)
from ocs_storage_exploration.storage.service import StorageService
from tests.ingest_helpers import CHIRPS_DIRECTORY, SAMPLES_DIRECTORY, WORLDPOP_FILE, ramp, write_geotiff

WORLDPOP_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)


def test_band_is_squeezed_and_dimensions_are_named(tmp_path: Path) -> None:
    path = write_geotiff(
        tmp_path / "block.tif",
        values=ramp(4, 5),
        y_values=numpy.linspace(9.5, 6.5, 4),
        x_values=numpy.linspace(-13.5, -9.5, 5),
    )
    dataset = open_raster_file(path, variable="rain", timestamp=datetime(2024, 1, 1, tzinfo=UTC))
    assert tuple(str(name) for name in dataset["rain"].dims) == ("t", "y", "x")
    assert "band" not in dataset.dims
    assert dataset.sizes["t"] == 1
    assert dataset["t"].values[0] == numpy.datetime64("2024-01-01T00:00:00", "ns")


def test_ascending_y_is_reversed_with_its_rows(tmp_path: Path) -> None:
    values = ramp(3, 4)
    path = write_geotiff(
        tmp_path / "south-up.tif",
        values=values,
        y_values=numpy.array([1.5, 2.5, 3.5]),
        x_values=numpy.array([10.5, 11.5, 12.5, 13.5]),
    )
    dataset = open_raster_file(path, variable="rain", timestamp=datetime(2024, 1, 1, tzinfo=UTC))
    y_values = numpy.asarray(dataset["y"].values, dtype="float64")
    assert list(y_values) == [3.5, 2.5, 1.5]
    # The rows travelled with the coordinate, so the value at a latitude did not move.
    assert list(numpy.asarray(dataset["rain"].values[0, 0, :])) == list(values[2, :])


def test_longitudes_beyond_180_are_wrapped_and_sorted(tmp_path: Path) -> None:
    values = ramp(2, 4)
    path = write_geotiff(
        tmp_path / "zero-to-360.tif",
        values=values,
        y_values=numpy.array([45.0, -45.0]),
        x_values=numpy.array([45.0, 135.0, 225.0, 315.0]),
    )
    dataset = open_raster_file(path, variable="rain", timestamp=datetime(2024, 1, 1, tzinfo=UTC))
    x_values = numpy.asarray(dataset["x"].values, dtype="float64")
    assert list(x_values) == [-135.0, -45.0, 45.0, 135.0]
    # The columns travelled with the coordinate: the cell that was at 225 is now at minus 135.
    assert dataset["rain"].values[0, 0, 0] == pytest.approx(values[0, 2])
    grid = grid_from_dataset(dataset, variable="rain")
    assert grid.bbox.minimum_x == pytest.approx(-180.0)
    assert grid.bbox.maximum_x == pytest.approx(180.0)


def test_nodata_is_masked_and_recorded_as_a_finite_attribute(tmp_path: Path) -> None:
    values = ramp(2, 3)
    values[0, 0] = -9999.0
    path = write_geotiff(
        tmp_path / "with-nodata.tif",
        values=values,
        y_values=numpy.array([2.5, 1.5]),
        x_values=numpy.array([10.5, 11.5, 12.5]),
        nodata_value=-9999.0,
    )
    dataset = open_raster_file(path, variable="rain", timestamp=datetime(2024, 1, 1, tzinfo=UTC))
    assert bool(numpy.isnan(dataset["rain"].values[0, 0, 0]))
    assert dataset["rain"].attrs[NODATA_ATTRIBUTE] == pytest.approx(-9999.0)
    assert "_FillValue" not in dataset["rain"].attrs
    assert dataset["rain"].encoding == {}


def test_projection_is_written_as_spatial_ref_and_projection_code(tmp_path: Path) -> None:
    path = write_geotiff(
        tmp_path / "projected.tif",
        values=ramp(2, 2),
        y_values=numpy.array([1_000_500.0, 999_500.0]),
        x_values=numpy.array([500_500.0, 501_500.0]),
        crs="EPSG:32633",
    )
    dataset = open_raster_file(path, variable="rain", timestamp=datetime(2024, 1, 1, tzinfo=UTC))
    assert dataset.attrs[PROJECTION_CODE_ATTRIBUTE] == "EPSG:32633"
    assert "crs_wkt" in dataset[SPATIAL_REFERENCE_NAME].attrs
    assert grid_from_dataset(dataset, variable="rain").crs == "EPSG:32633"


def test_a_raster_without_a_time_axis_needs_a_timestamp(tmp_path: Path) -> None:
    path = write_geotiff(
        tmp_path / "undated.tif",
        values=ramp(2, 2),
        y_values=numpy.array([2.5, 1.5]),
        x_values=numpy.array([10.5, 11.5]),
    )
    with pytest.raises(RasterContractError, match="timestamp"):
        open_raster_file(path, variable="rain")


def test_timestamp_is_read_from_the_file_name() -> None:
    assert timestamp_from_filename(Path("chirps3-2024-01-07.tif")) == datetime(2024, 1, 7)
    assert timestamp_from_filename(
        Path("chirps.2024.01.07.cog"),
        pattern=r"(\d{4})\.(\d{2})\.(\d{2})",
    ) == datetime(2024, 1, 7)
    with pytest.raises(RasterContractError, match="carries no timestamp"):
        timestamp_from_filename(Path("undated.tif"))


def test_a_glob_is_expanded_in_timestamp_order(tmp_path: Path) -> None:
    for day in ("2024-01-03", "2024-01-01", "2024-01-02"):
        write_geotiff(
            tmp_path / "block" / f"rain-{day}.tif",
            values=ramp(2, 2),
            y_values=numpy.array([2.5, 1.5]),
            x_values=numpy.array([10.5, 11.5]),
        )
    plan = build_raster_ingest_plan(
        files=["block/*.tif"],
        variable="rain",
        roots=[tmp_path],
        working_directory=tmp_path,
    )
    assert [path.name for path in plan.files] == [
        "rain-2024-01-01.tif",
        "rain-2024-01-02.tif",
        "rain-2024-01-03.tif",
    ]
    assert plan.timestamps == (datetime(2024, 1, 1), datetime(2024, 1, 2), datetime(2024, 1, 3))


def test_explicit_timestamps_must_match_the_file_count(tmp_path: Path) -> None:
    write_geotiff(
        tmp_path / "rain-2024-01-01.tif",
        values=ramp(2, 2),
        y_values=numpy.array([2.5, 1.5]),
        x_values=numpy.array([10.5, 11.5]),
    )
    with pytest.raises(RasterContractError, match="match one to one"):
        build_raster_ingest_plan(
            files=["rain-2024-01-01.tif"],
            variable="rain",
            roots=[tmp_path],
            timestamps=[datetime(2024, 1, 1), datetime(2024, 1, 2)],
            working_directory=tmp_path,
        )


def test_worldpop_is_ingested_as_one_published_timestep(storage_service: StorageService) -> None:
    plan = build_raster_ingest_plan(
        files=[str(WORLDPOP_FILE)],
        variable="population",
        roots=[SAMPLES_DIRECTORY],
        timestamp=WORLDPOP_TIMESTAMP,
        title="WorldPop Sierra Leone 2026",
        license="CC-BY-4.0",
        attribution="WorldPop",
        publish=True,
    )
    result = ingest_raster_files(storage_service.raster, "worldpop-sle-2026", plan)

    assert result.timestep_count == 1
    assert result.variables == ("population",)
    assert result.published is True
    assert result.timestamps == (datetime(2026, 1, 1),)

    description = storage_service.raster.describe("worldpop-sle-2026")
    assert description.crs == "EPSG:4326"
    assert description.shape == (370, 364)
    assert description.temporal_start == datetime(2026, 1, 1)

    summary = storage_service.raster.query("worldpop-sle-2026", variable="population")
    assert summary.cell_count == 370 * 364
    assert summary.maximum is not None and summary.maximum > 0.0
    # The nodata sentinel was masked rather than summarised as a value.
    assert summary.minimum is not None and summary.minimum >= 0.0

    record = storage_service.require_coverage("worldpop-sle-2026")
    assert record.license == "CC-BY-4.0"
    assert record.attribution == "WorldPop"


def test_worldpop_is_ingested_through_the_api(ingest_client: TestClient) -> None:
    response = ingest_client.post(
        "/api/v1/raster/worldpop-sle-2026/ingest",
        json={
            "files": [str(WORLDPOP_FILE)],
            "variable": "population",
            "timestamp": "2026-01-01T00:00:00Z",
            "title": "WorldPop Sierra Leone 2026",
            "license": "CC-BY-4.0",
            "attribution": "WorldPop",
            "publish": True,
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["timestep_count"] == 1
    assert payload["published"] is True
    assert payload["timestamps"] == ["2026-01-01T00:00:00"]

    query = ingest_client.get("/api/v1/raster/worldpop-sle-2026/query")
    assert query.status_code == 200, query.text
    assert query.json()["crs"] == "EPSG:4326"


def test_an_ingest_of_an_unreadable_suffix_is_refused(ingest_client: TestClient) -> None:
    response = ingest_client.post(
        "/api/v1/raster/districts-as-raster/ingest",
        json={
            "files": [str(SAMPLES_DIRECTORY / "sierra_leone_districts.geojson")],
            "variable": "population",
            "timestamp": "2026-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 422, response.text
    assert "readable raster suffix" in response.json()["detail"]


@pytest.mark.samples
def test_the_chirps_days_are_ingested_as_one_time_axis(storage_service: StorageService) -> None:
    if not CHIRPS_DIRECTORY.is_dir() or not any(CHIRPS_DIRECTORY.glob("chirps3-*.tif")):
        pytest.skip("run `make samples` while online to fetch the CHIRPS days")
    plan = build_raster_ingest_plan(
        files=[str(CHIRPS_DIRECTORY / "chirps3-*.tif")],
        variable="precipitation",
        roots=[SAMPLES_DIRECTORY],
        publish=True,
    )
    result = ingest_raster_files(storage_service.raster, "chirps3-sle-daily", plan)
    assert result.timestep_count == len(plan.files)
    assert result.published is True

    description = storage_service.raster.describe("chirps3-sle-daily")
    assert description.temporal_start == plan.timestamps[0]
    assert description.temporal_end == plan.timestamps[-1]


def test_ingest_settings_default_to_the_samples_and_data_directories() -> None:
    settings = Settings(data_directory=Path("/tmp/ocs-data"))
    assert settings.ingest_roots == [Path("samples"), Path("/tmp/ocs-data")]
