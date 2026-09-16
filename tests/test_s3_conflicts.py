"""Tests that compare-and-swap really loses a race on S3, through the conditional PUT rather than an emulation."""

from __future__ import annotations

from datetime import datetime

import geopandas
import icechunk
import obstore
import pytest
from obstore.exceptions import PreconditionError

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import DatasetAlreadyExistsError, PublicationConflictError
from ocs_storage_exploration.storage.keys import vector_data_key
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.raster import RasterRepository, TimeStep, build_synthetic_cube, build_timestamps
from ocs_storage_exploration.storage.schemas import (
    BoundingBox,
    CoverageDataset,
    GridSpecification,
    Publication,
    current_timestamp,
)
from ocs_storage_exploration.storage.vector.collection import VectorCollectionPointer, VectorCollectionStore

pytestmark = pytest.mark.s3

COVERAGE = "conflict-coverage"
COLLECTION = "conflict-collection"
VARIABLE = "temperature"
START = datetime(2020, 1, 1)


def build_grid() -> GridSpecification:
    return GridSpecification(
        shape=(4, 6),
        bbox=BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=12.0, maximum_y=8.0),
        crs="EPSG:4326",
    )


def build_cube(grid: GridSpecification, *, count: int = 2, start: datetime = START, seed: int = 0):
    return build_synthetic_cube(
        grid,
        variable=VARIABLE,
        timestamps=build_timestamps(start, count, TimeStep.DAY),
        seed=seed,
    )


def build_record(title: str = "Conflict coverage") -> CoverageDataset:
    now = current_timestamp()
    return CoverageDataset(
        dataset_identifier=COVERAGE,
        title=title,
        storage_key=f"raster/{COVERAGE}",
        created_at=now,
        updated_at=now,
        bbox=build_grid().bbox,
        publication=Publication(),
        grid=build_grid(),
        variables=(VARIABLE,),
        timestep_count=0,
    )


def test_the_object_store_uses_the_real_conditional_put(live_s3_backend: StorageBackend) -> None:
    store = live_s3_backend.object_store()
    key = live_s3_backend.address("catalog/datasets", "conditional.json").key
    obstore.put(store, key, b"first")

    with pytest.raises(PreconditionError):
        # A NotImplementedError here would mean the store had fallen back to the non-atomic emulation.
        obstore.put(store, key, b"second", mode={"e_tag": '"not-the-current-etag"'})


def test_two_catalogs_creating_one_record_lose_the_conditional_create(live_s3_backend: StorageBackend) -> None:
    first = ObjectCatalog(live_s3_backend)
    second = ObjectCatalog(live_s3_backend)

    first.put(build_record(), create=True)

    with pytest.raises(DatasetAlreadyExistsError):
        second.put(build_record(title="Written by the second catalog"), create=True)
    assert second.require(COVERAGE).title == "Conflict coverage"


def test_two_catalogs_racing_one_record_lose_the_compare_and_swap(live_s3_backend: StorageBackend) -> None:
    first = ObjectCatalog(live_s3_backend)
    second = ObjectCatalog(live_s3_backend)
    first.put(build_record(), create=True)
    stale = first.require_entry(COVERAGE)

    second.put(build_record(title="Written by the second catalog"), revision=second.require_entry(COVERAGE).revision)

    # A read in between must not launder the revision the first catalog still holds.
    assert first.require(COVERAGE).title == "Written by the second catalog"
    with pytest.raises(PublicationConflictError):
        first.put(build_record(title="Written by the first catalog"), revision=stale.revision)
    assert first.require(COVERAGE).title == "Written by the second catalog"


def test_two_vector_writers_racing_one_version_reserve_different_numbers(
    live_s3_backend: StorageBackend,
    live_s3_settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    writer = VectorCollectionStore(live_s3_backend, ObjectCatalog(live_s3_backend), live_s3_settings)
    racing = VectorCollectionStore(live_s3_backend, ObjectCatalog(live_s3_backend), live_s3_settings)
    writer.write(COLLECTION, sample_features, identifier_property="id", publish=True)
    published = bytes(
        obstore.get(
            live_s3_backend.object_store(), live_s3_backend.address(vector_data_key(COLLECTION, 1)).key
        ).bytes(),
    )
    # Freeze the racing writer on the empty listing it saw before the first writer created version 1.
    racing.versions = lambda collection_identifier: []  # type: ignore[method-assign]

    result = racing.write(COLLECTION, sample_features.iloc[:5], identifier_property="id")

    assert result.version == 2
    replayed = bytes(
        obstore.get(
            live_s3_backend.object_store(), live_s3_backend.address(vector_data_key(COLLECTION, 1)).key
        ).bytes(),
    )
    assert replayed == published


def test_two_raster_publishes_with_a_stale_snapshot_lose_the_compare_and_swap(
    live_s3_backend: StorageBackend,
    live_s3_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grid = build_grid()
    writer = RasterRepository(live_s3_backend, ObjectCatalog(live_s3_backend), live_s3_settings)
    first = writer.create(COVERAGE, grid, build_cube(grid))
    second = writer.append(COVERAGE, build_cube(grid, start=datetime(2020, 1, 3), seed=1))
    third = writer.append(COVERAGE, build_cube(grid, start=datetime(2020, 1, 5), seed=2))
    writer.publish(COVERAGE, snapshot_identifier=first.snapshot_identifier)
    stale = RasterRepository(live_s3_backend, ObjectCatalog(live_s3_backend), live_s3_settings)
    # Freeze what the stale publisher saw before the writer moved the branch, which is the whole race.
    monkeypatch.setattr(stale, "_published_snapshot", lambda repository: first.snapshot_identifier)

    writer.publish(COVERAGE, snapshot_identifier=second.snapshot_identifier)

    with pytest.raises(PublicationConflictError):
        stale.publish(COVERAGE, snapshot_identifier=third.snapshot_identifier)
    published = icechunk.Repository.open(
        live_s3_backend.icechunk_storage(writer.repository_address(COVERAGE)),
    ).lookup_branch("published")
    assert published == second.snapshot_identifier


def test_a_second_vector_publisher_cannot_create_the_pointer_twice(
    live_s3_backend: StorageBackend,
    live_s3_settings: Settings,
    sample_features: geopandas.GeoDataFrame,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = VectorCollectionStore(live_s3_backend, ObjectCatalog(live_s3_backend), live_s3_settings)
    writer.write(COLLECTION, sample_features, identifier_property="id")
    racing = VectorCollectionStore(live_s3_backend, ObjectCatalog(live_s3_backend), live_s3_settings)
    # Freeze the racing publisher on the absent pointer it saw before the writer created one.
    monkeypatch.setattr(racing, "_read_pointer", lambda identifier: None)

    writer.publish(COLLECTION)

    with pytest.raises(PublicationConflictError):
        racing.publish(COLLECTION)


def test_a_stale_vector_pointer_loses_the_compare_and_swap(
    live_s3_backend: StorageBackend,
    live_s3_settings: Settings,
    sample_features: geopandas.GeoDataFrame,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = VectorCollectionStore(live_s3_backend, ObjectCatalog(live_s3_backend), live_s3_settings)
    writer.write(COLLECTION, sample_features, identifier_property="id")
    writer.write(COLLECTION, sample_features.iloc[:5], identifier_property="id")
    writer.publish(COLLECTION, version=1)
    stale = VectorCollectionStore(live_s3_backend, ObjectCatalog(live_s3_backend), live_s3_settings)
    frozen: VectorCollectionPointer | None = stale._read_pointer(COLLECTION)
    assert frozen is not None and frozen.version == 1
    # Freeze the pointer the stale publisher read, and the etag it remembered with it.
    monkeypatch.setattr(stale, "_read_pointer", lambda identifier: frozen)

    writer.publish(COLLECTION, version=2)

    with pytest.raises(PublicationConflictError):
        stale.publish(COLLECTION, version=2)
    assert writer.current_version(COLLECTION) == 2
