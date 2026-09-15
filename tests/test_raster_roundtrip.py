from __future__ import annotations

from datetime import datetime

import icechunk
import numpy
import pytest
import xarray
from icechunk.xarray import to_icechunk
from pyproj import CRS

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import (
    DatasetAlreadyExistsError,
    DatasetNotFoundError,
    QuerySizeGuardError,
    RasterContractError,
)
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.raster import (
    PROJECTION_CODE_ATTRIBUTE,
    SPATIAL_BBOX_ATTRIBUTE,
    SPATIAL_REFERENCE_NAME,
    RasterRepository,
    TimeStep,
    VersionSelector,
    apply_geozarr_attributes,
    build_synthetic_cube,
    build_timestamps,
)
from ocs_storage_exploration.storage.schemas import BoundingBox, CoverageDataset, GridSpecification

IDENTIFIER = "roundtrip"
VARIABLE = "temperature"
START = datetime(2020, 1, 1)


def build_grid() -> GridSpecification:
    return GridSpecification(
        shape=(4, 6),
        bbox=BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=12.0, maximum_y=8.0),
        crs="EPSG:4326",
    )


def build_cube(
    grid: GridSpecification,
    *,
    count: int = 3,
    start: datetime = START,
    seed: int = 0,
) -> xarray.Dataset:
    timestamps = build_timestamps(start, count, TimeStep.DAY)
    return build_synthetic_cube(grid, variable=VARIABLE, timestamps=timestamps, seed=seed)


@pytest.fixture
def grid() -> GridSpecification:
    return build_grid()


@pytest.fixture
def raster_repository(
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
) -> RasterRepository:
    return RasterRepository(storage_backend, catalog, settings)


def test_create_then_read_round_trips_values_and_dimensions(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    catalog: ObjectCatalog,
):
    cube = build_cube(grid)

    result = raster_repository.create(IDENTIFIER, grid, cube, title="Round trip")

    assert result.dataset_identifier == IDENTIFIER
    assert result.timestep_count == 3
    assert result.variables == (VARIABLE,)
    assert result.snapshot_identifier
    with raster_repository.read(IDENTIFIER, version=VersionSelector.DRAFT) as handle:
        assert handle.group is None
        assert handle.snapshot_identifier == result.snapshot_identifier
        assert dict(handle.dataset.sizes) == {"t": 3, "y": 4, "x": 6}
        assert numpy.allclose(handle.dataset[VARIABLE].values, cube[VARIABLE].values)
        assert list(handle.dataset["y"].values) == [7.0, 5.0, 3.0, 1.0]
    record = catalog.require(IDENTIFIER)
    assert isinstance(record, CoverageDataset)
    assert record.title == "Round trip"
    assert record.timestep_count == 3
    assert record.temporal is not None
    assert record.temporal.start == START
    assert record.address.endswith("raster/roundtrip")


def test_read_side_carries_the_coordinate_reference_system(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))

    with raster_repository.read(IDENTIFIER, version=VersionSelector.DRAFT) as handle:
        assert SPATIAL_REFERENCE_NAME in handle.dataset.coords
        well_known_text = str(handle.dataset[SPATIAL_REFERENCE_NAME].attrs["crs_wkt"])
        assert CRS.from_wkt(well_known_text) == CRS.from_user_input(grid.crs)
        assert handle.dataset.attrs[PROJECTION_CODE_ATTRIBUTE] == "EPSG:4326"
        assert handle.dataset.attrs[SPATIAL_BBOX_ATTRIBUTE] == [0.0, 0.0, 12.0, 8.0]


def test_root_attributes_survive_the_write(raster_repository: RasterRepository, grid: GridSpecification):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))

    attributes = raster_repository.root_attributes(IDENTIFIER, version=VersionSelector.DRAFT)

    assert attributes[PROJECTION_CODE_ATTRIBUTE] == "EPSG:4326"


def test_append_grows_the_time_dimension_and_preserves_earlier_values(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    catalog: ObjectCatalog,
):
    first = build_cube(grid, count=3)
    raster_repository.create(IDENTIFIER, grid, first)
    second = build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1)

    result = raster_repository.append(IDENTIFIER, second)

    assert result.timestep_count == 5
    with raster_repository.read(IDENTIFIER, version=VersionSelector.DRAFT) as handle:
        assert dict(handle.dataset.sizes) == {"t": 5, "y": 4, "x": 6}
        assert numpy.allclose(handle.dataset[VARIABLE].values[:3], first[VARIABLE].values)
        assert numpy.allclose(handle.dataset[VARIABLE].values[3:], second[VARIABLE].values)
        assert handle.dataset.attrs[PROJECTION_CODE_ATTRIBUTE] == "EPSG:4326"
        assert SPATIAL_REFERENCE_NAME in handle.dataset.coords
    record = catalog.require(IDENTIFIER)
    assert isinstance(record, CoverageDataset)
    assert record.timestep_count == 5
    assert record.temporal is not None
    assert record.temporal.end == datetime(2020, 1, 5)


def test_append_refuses_mismatched_spatial_coordinates(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))
    shifted = build_cube(grid, count=1, start=datetime(2020, 1, 4))
    shifted = shifted.assign_coords({"y": shifted["y"].values + 0.5})

    with pytest.raises(RasterContractError, match="coordinate"):
        raster_repository.append(IDENTIFIER, shifted)


def test_append_refuses_a_cube_with_the_wrong_shape(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))
    narrow = build_grid().model_copy(update={"shape": (4, 5)})

    with pytest.raises(RasterContractError):
        raster_repository.append(IDENTIFIER, build_cube(narrow, count=1, start=datetime(2020, 1, 4)))


def test_create_refuses_a_non_finite_attribute(raster_repository: RasterRepository, grid: GridSpecification):
    cube = build_cube(grid)
    cube.attrs["calibration"] = float("nan")

    with pytest.raises(RasterContractError, match="calibration"):
        raster_repository.create(IDENTIFIER, grid, cube)


def test_create_refuses_an_existing_identifier_without_overwrite(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))

    with pytest.raises(DatasetAlreadyExistsError):
        raster_repository.create(IDENTIFIER, grid, build_cube(grid))


def test_create_with_overwrite_replaces_the_values_and_keeps_the_creation_time(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    catalog: ObjectCatalog,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid), title="First")
    created_at = catalog.require(IDENTIFIER).created_at
    replacement = build_cube(grid, count=2, seed=5)

    result = raster_repository.create(IDENTIFIER, grid, replacement, overwrite=True, message="replace")

    assert result.timestep_count == 2
    with raster_repository.read(IDENTIFIER, version=VersionSelector.DRAFT) as handle:
        assert dict(handle.dataset.sizes) == {"t": 2, "y": 4, "x": 6}
        assert numpy.allclose(handle.dataset[VARIABLE].values, replacement[VARIABLE].values)
    record = catalog.require(IDENTIFIER)
    assert record.created_at == created_at
    assert record.title == "First"


def test_create_refuses_a_cube_without_data_variables(raster_repository: RasterRepository, grid: GridSpecification):
    with pytest.raises(RasterContractError, match="data variables"):
        raster_repository.create(IDENTIFIER, grid, xarray.Dataset())


def test_create_refuses_dimensions_in_the_wrong_order(raster_repository: RasterRepository, grid: GridSpecification):
    transposed = build_cube(grid).transpose("y", "x", "t")

    with pytest.raises(RasterContractError, match="dimensions"):
        raster_repository.create(IDENTIFIER, grid, transposed)


def test_delete_removes_the_record_before_the_bytes(raster_repository: RasterRepository, grid: GridSpecification):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))

    raster_repository.delete(IDENTIFIER)

    with pytest.raises(DatasetNotFoundError), raster_repository.read(IDENTIFIER, version=VersionSelector.DRAFT):
        pass


def test_read_refuses_an_unknown_dataset(raster_repository: RasterRepository):
    with pytest.raises(DatasetNotFoundError), raster_repository.read("missing", version=VersionSelector.DRAFT):
        pass


def test_rioxarray_reads_the_written_coordinate_reference_system(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    pytest.importorskip("rioxarray")
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))

    with raster_repository.read(IDENTIFIER, version=VersionSelector.DRAFT) as handle:
        assert handle.dataset.rio.crs is not None
        assert CRS.from_user_input(handle.dataset.rio.crs.to_wkt()) == CRS.from_user_input(grid.crs)


def test_read_falls_back_to_the_first_multiscale_group(
    raster_repository: RasterRepository,
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    grid: GridSpecification,
):
    identifier = "multiscale"
    cube = apply_geozarr_attributes(build_cube(grid), grid)
    address = raster_repository.repository_address(identifier)
    icechunk_repository = icechunk.Repository.open_or_create(storage_backend.icechunk_storage(address))
    session = icechunk_repository.writable_session("main")
    to_icechunk(cube, session, group="0", mode="w")
    session.commit("initial write")
    catalog.put(
        CoverageDataset(
            dataset_identifier=identifier,
            title="Multiscale",
            address=address.as_uri(),
            grid=grid,
            variables=(VARIABLE,),
            timestep_count=3,
        ),
    )

    with raster_repository.read(identifier, version=VersionSelector.DRAFT) as handle:
        assert handle.group == "0"
        assert numpy.allclose(handle.dataset[VARIABLE].values, cube[VARIABLE].values)


def test_the_licence_and_attribution_survive_an_overwrite_and_an_append(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    catalog: ObjectCatalog,
):
    raster_repository.create(
        IDENTIFIER,
        grid,
        build_cube(grid),
        license="CC-BY-4.0",
        attribution="Open Climate Service",
    )

    raster_repository.create(IDENTIFIER, grid, build_cube(grid), overwrite=True)
    overwritten = catalog.require(IDENTIFIER)
    raster_repository.append(IDENTIFIER, build_cube(grid, start=datetime(2020, 1, 4)))
    appended = catalog.require(IDENTIFIER)

    assert isinstance(overwritten, CoverageDataset)
    assert overwritten.license == "CC-BY-4.0"
    assert overwritten.attribution == "Open Climate Service"
    assert isinstance(appended, CoverageDataset)
    assert appended.license == "CC-BY-4.0"
    assert appended.attribution == "Open Climate Service"


def test_an_overwrite_can_replace_the_licence_and_attribution(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    catalog: ObjectCatalog,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid), license="CC-BY-4.0", attribution="First")

    raster_repository.create(
        IDENTIFIER,
        grid,
        build_cube(grid),
        license="CC-BY-NC-4.0",
        attribution="Second",
        overwrite=True,
    )
    record = catalog.require(IDENTIFIER)

    assert isinstance(record, CoverageDataset)
    assert record.license == "CC-BY-NC-4.0"
    assert record.attribution == "Second"


def test_append_refuses_a_variable_the_store_does_not_hold_and_stays_readable(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    continuation = build_cube(grid, count=1, start=datetime(2020, 1, 4))
    humidity = build_synthetic_cube(
        grid,
        variable="humidity",
        timestamps=build_timestamps(datetime(2020, 1, 4), 1, TimeStep.DAY),
    )

    with pytest.raises(RasterContractError, match="humidity"):
        raster_repository.append(IDENTIFIER, xarray.merge([continuation, humidity]))

    with raster_repository.read(IDENTIFIER, version=VersionSelector.DRAFT) as handle:
        assert dict(handle.dataset.sizes) == {"t": 3, "y": 4, "x": 6}
        assert sorted(str(name) for name in handle.dataset.data_vars) == [VARIABLE]


def test_append_refuses_a_different_data_type(raster_repository: RasterRepository, grid: GridSpecification):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    wider = build_cube(grid.model_copy(update={"data_type": "float64"}), count=1, start=datetime(2020, 1, 4))

    with pytest.raises(RasterContractError, match="float64"):
        raster_repository.append(IDENTIFIER, wider)

    with raster_repository.read(IDENTIFIER, version=VersionSelector.DRAFT) as handle:
        assert handle.dataset.sizes["t"] == 3


def test_create_refuses_a_cube_larger_than_the_guard(
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
    grid: GridSpecification,
):
    guarded = RasterRepository(storage_backend, catalog, settings.model_copy(update={"max_cube_cells": 10}))

    with pytest.raises(QuerySizeGuardError, match="more than the 10 allowed"):
        guarded.create(IDENTIFIER, grid, build_cube(grid))

    assert catalog.get(IDENTIFIER) is None


def test_append_refuses_a_cube_larger_than_the_guard(
    raster_repository: RasterRepository,
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    guarded = RasterRepository(storage_backend, catalog, settings.model_copy(update={"max_cube_cells": 10}))

    with pytest.raises(QuerySizeGuardError, match="more than the 10 allowed"):
        guarded.append(IDENTIFIER, build_cube(grid, count=1, start=datetime(2020, 1, 4)))


def test_recreating_a_deleted_coverage_starts_a_fresh_history(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))
    raster_repository.append(IDENTIFIER, build_cube(grid, count=1, start=datetime(2020, 1, 4)))
    raster_repository.delete(IDENTIFIER)

    raster_repository.create(IDENTIFIER, grid, build_cube(grid), message="written again")

    versions = raster_repository.versions(IDENTIFIER)
    # Only the initialisation snapshot and the write above: the deleted history must not come back.
    assert len(versions) == 2
    assert versions[0].message == "written again"
