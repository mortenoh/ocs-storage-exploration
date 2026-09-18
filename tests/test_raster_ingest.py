"""Tests of the raster ingest normalisation rules and of ingesting the committed sample rasters."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy
import pytest
import xarray
import xarray.backends
from fastapi.testclient import TestClient

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.errors import (
    BackendNotSupportedError,
    QuerySizeGuardError,
    RasterContractError,
)
from ocs_storage_exploration.storage.raster import (
    NODATA_ATTRIBUTE,
    PROJECTION_CODE_ATTRIBUTE,
    SPATIAL_REFERENCE_NAME,
    VersionSelector,
)
from ocs_storage_exploration.storage.raster.ingest import (
    NETCDF_ENGINE,
    build_raster_ingest_plan,
    grid_from_dataset,
    ingest_raster_files,
    open_raster_file,
    timestamp_from_filename,
)
from ocs_storage_exploration.storage.schemas import BoundingBox
from ocs_storage_exploration.storage.service import StorageService
from tests.ingest_helpers import (
    CHIRPS_DIRECTORY,
    SAMPLES_DIRECTORY,
    WORLDPOP_FILE,
    ramp,
    write_geotiff,
    write_netcdf,
    write_scaled_geotiff,
    write_zarr_store,
)

WORLDPOP_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)
# An 8 by 8 source over one timestep holds 64 cells, so a limit of 16 refuses it and a clip saves it.
GUARDED_ROW_COUNT = 8
GUARDED_COLUMN_COUNT = 8
GUARDED_CELL_LIMIT = 16


def write_guarded_netcdf(path: Path, moment: datetime) -> Path:
    regular = ramp(GUARDED_ROW_COUNT, GUARDED_COLUMN_COUNT).reshape(1, GUARDED_ROW_COUNT, GUARDED_COLUMN_COUNT)
    return write_netcdf(
        path,
        values=regular,
        y_values=numpy.linspace(4.5, -2.5, GUARDED_ROW_COUNT),
        x_values=numpy.linspace(10.5, 17.5, GUARDED_COLUMN_COUNT),
        timestamps=[moment],
    )


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


def test_a_scaled_geotiff_is_decoded_before_it_is_stored(tmp_path: Path) -> None:
    path = write_scaled_geotiff(
        tmp_path / "scaled.tif",
        values=numpy.full((2, 3), 100, dtype="int16"),
        y_values=numpy.array([2.5, 1.5]),
        x_values=numpy.array([10.5, 11.5, 12.5]),
        scale_factor=0.1,
        add_offset=5.0,
    )
    dataset = open_raster_file(path, variable="rain", timestamp=datetime(2024, 1, 1, tzinfo=UTC))
    # 100 raw is 100 * 0.1 + 5 in the units the file declares, and that is what a reader has to see.
    assert dataset["rain"].values[0, 0, 0] == pytest.approx(15.0)
    assert numpy.issubdtype(numpy.dtype(dataset["rain"].dtype), numpy.floating)
    assert grid_from_dataset(dataset, variable="rain").data_type.startswith("float")


def test_a_scaled_nodata_sentinel_is_recorded_in_decoded_units(tmp_path: Path) -> None:
    values = numpy.full((2, 3), 100, dtype="int16")
    values[0, 0] = -1
    path = write_scaled_geotiff(
        tmp_path / "scaled-nodata.tif",
        values=values,
        y_values=numpy.array([2.5, 1.5]),
        x_values=numpy.array([10.5, 11.5, 12.5]),
        scale_factor=0.1,
        add_offset=5.0,
        nodata_value=-1,
    )
    dataset = open_raster_file(path, variable="rain", timestamp=datetime(2024, 1, 1, tzinfo=UTC))
    assert bool(numpy.isnan(dataset["rain"].values[0, 0, 0]))
    # The sentinel is recorded in the same units as the values it stood among, not in raw counts.
    assert dataset["rain"].attrs[NODATA_ATTRIBUTE] == pytest.approx(-1 * 0.1 + 5.0)
    assert dataset["rain"].values[0, 0, 1] == pytest.approx(15.0)


def test_a_netcdf_file_round_trips_through_ingest(tmp_path: Path) -> None:
    values = ramp(2, 3).reshape(1, 2, 3)
    path = write_netcdf(
        tmp_path / "rain-2024-01-01.nc",
        values=values,
        y_values=numpy.array([2.5, 1.5]),
        x_values=numpy.array([10.5, 11.5, 12.5]),
        timestamps=[datetime(2024, 1, 1)],
    )
    dataset = open_raster_file(path, variable="rain")
    assert tuple(str(name) for name in dataset["rain"].dims) == ("t", "y", "x")
    assert dataset.sizes["t"] == 1
    assert dataset["t"].values[0] == numpy.datetime64("2024-01-01T00:00:00", "ns")
    assert list(numpy.asarray(dataset["rain"].values[0, 0, :])) == list(values[0, 0, :])
    assert grid_from_dataset(dataset, variable="rain").crs == "EPSG:4326"


def test_a_netcdf_file_without_its_engine_is_reported_as_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_netcdf(
        tmp_path / "rain-2024-01-01.nc",
        values=ramp(2, 3).reshape(1, 2, 3),
        y_values=numpy.array([2.5, 1.5]),
        x_values=numpy.array([10.5, 11.5, 12.5]),
        timestamps=[datetime(2024, 1, 1)],
    )
    monkeypatch.setattr(xarray.backends, "list_engines", lambda: {"rasterio": None, "store": None, "zarr": None})
    with pytest.raises(BackendNotSupportedError, match=NETCDF_ENGINE):
        open_raster_file(path, variable="rain")


def test_the_netcdf_engine_is_installed() -> None:
    assert NETCDF_ENGINE in xarray.backends.list_engines()


def test_an_unreadable_netcdf_file_is_a_contract_error(tmp_path: Path) -> None:
    path = tmp_path / "rain-2024-01-01.nc"
    path.write_bytes(b"not a netcdf file at all")
    with pytest.raises(RasterContractError, match="NetCDF"):
        open_raster_file(path, variable="rain")


def test_a_zarr_directory_store_is_ingested_end_to_end(storage_service: StorageService, tmp_path: Path) -> None:
    store = write_zarr_store(
        tmp_path / "cube-2024-03-05.zarr",
        values=ramp(2, 6).reshape(2, 2, 3),
        y_values=numpy.array([2.5, 1.5]),
        x_values=numpy.array([10.5, 11.5, 12.5]),
        timestamps=[datetime(2024, 3, 5), datetime(2024, 3, 6)],
    )
    plan = build_raster_ingest_plan(
        files=[str(store)],
        variable="rain",
        roots=[tmp_path],
        working_directory=tmp_path,
        publish=True,
    )
    assert plan.files == (store.resolve(),)

    result = ingest_raster_files(storage_service.raster, "cube-from-zarr", plan)
    assert result.timestep_count == 2
    assert result.variables == ("rain",)
    assert result.published is True

    description = storage_service.raster.describe("cube-from-zarr")
    assert description.shape == (2, 3)
    assert description.temporal_start == datetime(2024, 3, 5)
    assert description.temporal_end == datetime(2024, 3, 6)


def test_an_oversized_raster_is_refused_before_its_cells_are_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_guarded_netcdf(tmp_path / "rain-2024-01-01.nc", datetime(2024, 1, 1))

    def refuse_load(dataset: xarray.Dataset, **keywords: object) -> xarray.Dataset:
        raise AssertionError("the cube guard has to fire before the values are read into memory")

    monkeypatch.setattr(xarray.Dataset, "load", refuse_load)
    with pytest.raises(QuerySizeGuardError, match=r"rain-2024-01-01\.nc' holds 64 cells, more than the 16 allowed"):
        open_raster_file(path, variable="rain", max_cube_cells=GUARDED_CELL_LIMIT)


def test_a_bbox_clip_brings_an_oversized_raster_under_the_guard(tmp_path: Path) -> None:
    path = write_guarded_netcdf(tmp_path / "rain-2024-01-02.nc", datetime(2024, 1, 2))
    clipped = open_raster_file(
        path,
        variable="rain",
        bbox=BoundingBox(minimum_x=11.4, minimum_y=1.6, maximum_x=13.6, maximum_y=3.4),
        max_cube_cells=GUARDED_CELL_LIMIT,
    )
    # The guard counts the cells the clip left, not the ones the file holds, so a window of a large
    # source is ingested while the whole source is refused.
    assert (clipped.sizes["y"], clipped.sizes["x"]) == (3, 3)
    with pytest.raises(QuerySizeGuardError, match="more than the 16 allowed"):
        open_raster_file(path, variable="rain", max_cube_cells=GUARDED_CELL_LIMIT)


def test_an_ingest_plan_carries_the_cube_guard_onto_every_file(
    storage_service: StorageService,
    tmp_path: Path,
) -> None:
    write_guarded_netcdf(tmp_path / "rain-2024-01-01.nc", datetime(2024, 1, 1))
    plan = build_raster_ingest_plan(
        files=["rain-2024-01-01.nc"],
        variable="rain",
        roots=[tmp_path],
        working_directory=tmp_path,
        max_cube_cells=GUARDED_CELL_LIMIT,
    )
    assert plan.max_cube_cells == GUARDED_CELL_LIMIT

    # The file is named in the failure, which is the guard of the reader rather than the one the
    # engine runs on a cube it has already been handed.
    with pytest.raises(QuerySizeGuardError, match="rain-2024-01-01"):
        ingest_raster_files(storage_service.raster, "guarded-cube", plan)


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


def test_an_ingest_of_duplicate_timestamps_is_refused(storage_service: StorageService, tmp_path: Path) -> None:
    for name in ("first.tif", "second.tif"):
        write_geotiff(
            tmp_path / name,
            values=ramp(2, 2),
            y_values=numpy.array([2.5, 1.5]),
            x_values=numpy.array([10.5, 11.5]),
        )
    plan = build_raster_ingest_plan(
        files=["first.tif", "second.tif"],
        variable="rain",
        roots=[tmp_path],
        working_directory=tmp_path,
        timestamps=[datetime(2024, 1, 1), datetime(2024, 1, 1)],
    )

    with pytest.raises(RasterContractError, match="does not extend the committed time axis"):
        ingest_raster_files(storage_service.raster, "duplicate-timestamps", plan)

    # The create landed and the append that repeated its timestamp did not, so the store holds the
    # first file alone rather than a time axis a label window cannot be read on.
    description = storage_service.raster.describe("duplicate-timestamps", version=VersionSelector.DRAFT)
    assert description.timestep_count == 1
    assert description.temporal_end == datetime(2024, 1, 1)


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
