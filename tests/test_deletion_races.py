"""Tests that a deletion never sweeps a prefix it does not hold the reservation for."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import datetime

import geopandas
import pytest
import xarray

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.addresses import StorageAddress
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import PublicationConflictError
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.raster import (
    RasterRepository,
    TimeStep,
    VersionSelector,
    build_synthetic_cube,
    build_timestamps,
)
from ocs_storage_exploration.storage.raster.repository import MAXIMUM_DELETION_ATTEMPTS as RASTER_DELETION_ATTEMPTS
from ocs_storage_exploration.storage.schemas import (
    BoundingBox,
    Dataset,
    DatasetLifecycle,
    GridSpecification,
)
from ocs_storage_exploration.storage.vector.collection import MAXIMUM_DELETION_ATTEMPTS as VECTOR_DELETION_ATTEMPTS
from ocs_storage_exploration.storage.vector.collection import VectorCollectionStore

COLLECTION = "districts"
COVERAGE = "temperatures"
VARIABLE = "temperature"
START = datetime(2020, 1, 1)


class RacingCatalog(ObjectCatalog):
    """Catalog that lets a concurrent writer land just before, or just after, a deletion marks a record."""

    def __init__(
        self,
        backend: StorageBackend,
        *,
        before_mark: Callable[[], None] | None = None,
        after_mark: Callable[[], None] | None = None,
        rounds: int = 1,
    ) -> None:
        """Bind the catalog to the interferences it runs around each of the first rounds deletion marks."""
        super().__init__(backend)
        self._before_mark = before_mark
        self._after_mark = after_mark
        self._remaining = rounds

    def put(self, dataset: Dataset, *, revision: str | None = None, create: bool = False) -> None:
        """Write a record, running the interferences around a deletion mark while it still has rounds left."""
        racing = revision is not None and dataset.is_deleting and self._remaining > 0
        if racing:
            self._remaining -= 1
            if self._before_mark is not None:
                self._before_mark()
        super().put(dataset, revision=revision, create=create)
        if racing and self._after_mark is not None:
            self._after_mark()


def build_collection_store(
    storage_backend: StorageBackend,
    settings: Settings,
    catalog: ObjectCatalog,
) -> VectorCollectionStore:
    """Build a collection store with a catalog of its own, as a second process would have."""
    return VectorCollectionStore(storage_backend, catalog, settings)


def vector_keys(storage_backend: StorageBackend) -> list[str]:
    """List every object still stored below the prefix of the test collection."""
    return storage_backend.list_keys(storage_backend.address("vector", COLLECTION))


def build_grid() -> GridSpecification:
    """Build the small geographic grid the raster deletions in this module are written on."""
    return GridSpecification(
        shape=(4, 6),
        bbox=BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=12.0, maximum_y=8.0),
        crs="EPSG:4326",
    )


def build_cube(
    grid: GridSpecification,
    *,
    count: int = 2,
    start: datetime = START,
    seed: int = 0,
) -> xarray.Dataset:
    """Build a synthetic cube of count timesteps starting at start."""
    return build_synthetic_cube(
        grid,
        variable=VARIABLE,
        timestamps=build_timestamps(start, count, TimeStep.DAY),
        seed=seed,
    )


def published_coverage(storage_backend: StorageBackend, settings: Settings) -> RasterRepository:
    """Write and publish the coverage the raster deletions in this module race against."""
    writer = RasterRepository(storage_backend, ObjectCatalog(storage_backend), settings)
    writer.create(COVERAGE, build_grid(), build_cube(build_grid()))
    writer.publish(COVERAGE)
    return writer


def appending_writer(writer: RasterRepository) -> Callable[[], None]:
    """Return an interference that appends two more timesteps onto the coverage every time it runs."""
    rounds = itertools.count()

    def append_more_timesteps() -> None:
        index = next(rounds)
        writer.append(COVERAGE, build_cube(build_grid(), start=datetime(2020, 1, 3 + 2 * index), seed=index + 1))

    return append_more_timesteps


def test_a_vector_deletion_that_loses_its_mark_to_a_writer_retries_and_finishes(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    writer = build_collection_store(storage_backend, settings, ObjectCatalog(storage_backend))
    writer.write(COLLECTION, sample_features, identifier_property="id", publish=True)

    def publish_another_version() -> None:
        writer.write(COLLECTION, sample_features.iloc[:5], identifier_property="id", publish=True)

    racing = RacingCatalog(storage_backend, before_mark=publish_another_version)
    deleter = build_collection_store(storage_backend, settings, racing)

    removed = deleter.delete(COLLECTION)

    # The mark lost its compare-and-swap to version 2, was reacquired against the record that write
    # left behind, and only then swept: the record and every version go together.
    assert removed > 0
    assert ObjectCatalog(storage_backend).get(COLLECTION) is None
    assert vector_keys(storage_backend) == []


def test_a_vector_deletion_that_keeps_losing_is_refused_and_sweeps_nothing(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    writer = build_collection_store(storage_backend, settings, ObjectCatalog(storage_backend))
    writer.write(COLLECTION, sample_features, identifier_property="id", publish=True)

    def publish_another_version() -> None:
        writer.write(COLLECTION, sample_features.iloc[:5], identifier_property="id", publish=True)

    racing = RacingCatalog(storage_backend, before_mark=publish_another_version, rounds=VECTOR_DELETION_ATTEMPTS)
    deleter = build_collection_store(storage_backend, settings, racing)

    with pytest.raises(PublicationConflictError):
        deleter.delete(COLLECTION)

    reader = build_collection_store(storage_backend, settings, ObjectCatalog(storage_backend))
    record = ObjectCatalog(storage_backend).get(COLLECTION)
    assert record is not None
    assert record.is_deleting is False
    assert reader.versions(COLLECTION) == list(range(1, VECTOR_DELETION_ATTEMPTS + 2))
    assert reader.current_version(COLLECTION) == VECTOR_DELETION_ATTEMPTS + 1
    assert len(reader.read(COLLECTION, version=1).frame) == 12
    assert len(reader.read(COLLECTION).frame) == 5


def test_a_vector_deletion_that_loses_to_another_deleter_finishes_that_deletion(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    plain = ObjectCatalog(storage_backend)
    writer = build_collection_store(storage_backend, settings, plain)
    writer.write(COLLECTION, sample_features, identifier_property="id", publish=True)

    def mark_deleting() -> None:
        entry = plain.require_entry(COLLECTION)
        plain.put(entry.record.model_copy(update={"lifecycle": DatasetLifecycle.DELETING}), revision=entry.revision)

    racing = RacingCatalog(storage_backend, before_mark=mark_deleting)
    deleter = build_collection_store(storage_backend, settings, racing)

    removed = deleter.delete(COLLECTION)

    # The re-read found the record already reserved, and finishing that deletion is what this call is for.
    assert removed > 0
    assert plain.get(COLLECTION) is None
    assert vector_keys(storage_backend) == []


def test_a_vector_deletion_that_loses_to_a_finished_deleter_sweeps_nothing(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    plain = ObjectCatalog(storage_backend)
    writer = build_collection_store(storage_backend, settings, plain)
    writer.write(COLLECTION, sample_features, identifier_property="id", publish=True)

    def finish_the_deletion() -> None:
        build_collection_store(storage_backend, settings, plain).delete(COLLECTION)

    racing = RacingCatalog(storage_backend, before_mark=finish_the_deletion)
    deleter = build_collection_store(storage_backend, settings, racing)

    removed = deleter.delete(COLLECTION)

    assert removed == 0
    assert plain.get(COLLECTION) is None
    assert vector_keys(storage_backend) == []


def test_a_raster_deletion_that_loses_its_mark_to_a_writer_retries_and_finishes(
    storage_backend: StorageBackend,
    settings: Settings,
) -> None:
    writer = published_coverage(storage_backend, settings)
    racing = RacingCatalog(storage_backend, before_mark=appending_writer(writer))
    deleter = RasterRepository(storage_backend, racing, settings)

    deleter.delete(COVERAGE)

    assert ObjectCatalog(storage_backend).get(COVERAGE) is None
    assert storage_backend.list_keys(writer.repository_address(COVERAGE)) == []


def test_a_raster_deletion_that_keeps_losing_is_refused_and_sweeps_nothing(
    storage_backend: StorageBackend,
    settings: Settings,
) -> None:
    writer = published_coverage(storage_backend, settings)
    racing = RacingCatalog(storage_backend, before_mark=appending_writer(writer), rounds=RASTER_DELETION_ATTEMPTS)
    deleter = RasterRepository(storage_backend, racing, settings)

    with pytest.raises(PublicationConflictError):
        deleter.delete(COVERAGE)

    reader = RasterRepository(storage_backend, ObjectCatalog(storage_backend), settings)
    record = ObjectCatalog(storage_backend).get(COVERAGE)
    assert record is not None
    assert record.is_deleting is False
    # The published snapshot still reads, and every timestep the racing appends committed is still there.
    assert reader.query(COVERAGE).cell_count == 2 * 4 * 6
    draft = reader.describe(COVERAGE, version=VersionSelector.DRAFT)
    assert draft.timestep_count == 2 + 2 * RASTER_DELETION_ATTEMPTS


def test_a_raster_deletion_that_loses_to_another_deleter_finishes_that_deletion(
    storage_backend: StorageBackend,
    settings: Settings,
) -> None:
    plain = ObjectCatalog(storage_backend)
    writer = published_coverage(storage_backend, settings)

    def mark_deleting() -> None:
        entry = plain.require_entry(COVERAGE)
        plain.put(entry.record.model_copy(update={"lifecycle": DatasetLifecycle.DELETING}), revision=entry.revision)

    racing = RacingCatalog(storage_backend, before_mark=mark_deleting)
    deleter = RasterRepository(storage_backend, racing, settings)

    deleter.delete(COVERAGE)

    assert plain.get(COVERAGE) is None
    assert storage_backend.list_keys(writer.repository_address(COVERAGE)) == []


def test_a_raster_deletion_that_loses_to_a_finished_deleter_sweeps_nothing(
    storage_backend: StorageBackend,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = ObjectCatalog(storage_backend)
    writer = published_coverage(storage_backend, settings)
    swept: list[int] = []
    sweep = storage_backend.delete_prefix

    def counting_sweep(address: StorageAddress) -> int:
        removed = sweep(address)
        swept.append(removed)
        return removed

    def finish_the_deletion() -> None:
        RasterRepository(storage_backend, plain, settings).delete(COVERAGE)

    monkeypatch.setattr(storage_backend, "delete_prefix", counting_sweep)
    racing = RacingCatalog(storage_backend, before_mark=finish_the_deletion)

    RasterRepository(storage_backend, racing, settings).delete(COVERAGE)

    # Only the deleter that held the reservation swept; the one that lost the record swept nothing.
    assert len(swept) == 1
    assert plain.get(COVERAGE) is None
    assert storage_backend.list_keys(writer.repository_address(COVERAGE)) == []


def test_a_vector_deletion_whose_collection_is_recreated_after_its_mark_sweeps_nothing(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    plain = ObjectCatalog(storage_backend)
    writer = build_collection_store(storage_backend, settings, plain)
    writer.write(COLLECTION, sample_features, identifier_property="id", publish=True)

    def finish_the_deletion_and_write_again() -> None:
        # Another deleter sees the mark this call just wrote, finishes the deletion and drops the
        # record, which leaves a writer free to claim the same name for a collection of its own.
        other = build_collection_store(storage_backend, settings, plain)
        other.delete(COLLECTION)
        other.write(COLLECTION, sample_features.iloc[:4], identifier_property="id", publish=True)

    racing = RacingCatalog(storage_backend, after_mark=finish_the_deletion_and_write_again)
    deleter = build_collection_store(storage_backend, settings, racing)

    removed = deleter.delete(COLLECTION)

    # The record read back after the mark is live, so it is the new collection of the writer that
    # created it rather than this call's reservation, and none of its data is swept.
    assert removed == 0
    reader = build_collection_store(storage_backend, settings, ObjectCatalog(storage_backend))
    record = plain.get(COLLECTION)
    assert record is not None
    assert record.is_deleting is False
    assert reader.versions(COLLECTION) == [1]
    assert reader.current_version(COLLECTION) == 1
    assert len(reader.read(COLLECTION).frame) == 4


def test_a_raster_deletion_whose_coverage_is_recreated_after_its_mark_sweeps_nothing(
    storage_backend: StorageBackend,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = ObjectCatalog(storage_backend)
    published_coverage(storage_backend, settings)
    swept: list[int] = []
    sweep = storage_backend.delete_prefix

    def counting_sweep(address: StorageAddress) -> int:
        removed = sweep(address)
        swept.append(removed)
        return removed

    def finish_the_deletion_and_create_again() -> None:
        # Another deleter sees the mark this call just wrote, finishes the deletion and drops the
        # record, which leaves a writer free to claim the same name for a coverage of its own.
        other = RasterRepository(storage_backend, plain, settings)
        other.delete(COVERAGE)
        other.create(COVERAGE, build_grid(), build_cube(build_grid(), count=1))
        other.publish(COVERAGE)

    monkeypatch.setattr(storage_backend, "delete_prefix", counting_sweep)
    racing = RacingCatalog(storage_backend, after_mark=finish_the_deletion_and_create_again)

    RasterRepository(storage_backend, racing, settings).delete(COVERAGE)

    # Only the deletion that ran inside the interference swept; this one held no reservation on the
    # coverage that replaced it, so the recreated snapshot is still published and still readable.
    assert len(swept) == 1
    reader = RasterRepository(storage_backend, ObjectCatalog(storage_backend), settings)
    record = plain.get(COVERAGE)
    assert record is not None
    assert record.is_deleting is False
    assert reader.query(COVERAGE).cell_count == 1 * 4 * 6
    assert reader.describe(COVERAGE).timestep_count == 1
