"""Tests that a deletion sweeps and tombstones only what it still holds the reservation for."""

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
from ocs_storage_exploration.storage.errors import (
    DatasetAlreadyExistsError,
    DatasetNotFoundError,
    PublicationConflictError,
)
from ocs_storage_exploration.storage.keys import RASTER_PREFIX, VECTOR_PREFIX, validate_generation_token
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.raster import (
    RasterRepository,
    TimeStep,
    VersionSelector,
    build_synthetic_cube,
    build_timestamps,
)
from ocs_storage_exploration.storage.raster.repository import MAXIMUM_CLAIM_ATTEMPTS as RASTER_CLAIM_ATTEMPTS
from ocs_storage_exploration.storage.raster.repository import MAXIMUM_DELETION_ATTEMPTS as RASTER_DELETION_ATTEMPTS
from ocs_storage_exploration.storage.schemas import (
    BoundingBox,
    CoverageDataset,
    Dataset,
    DatasetLifecycle,
    FeatureDataset,
    GridSpecification,
    current_timestamp,
)
from ocs_storage_exploration.storage.vector.collection import MAXIMUM_CLAIM_ATTEMPTS as VECTOR_CLAIM_ATTEMPTS
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
        before_claim: Callable[[], None] | None = None,
        before_tombstone: Callable[[], None] | None = None,
        rounds: int = 1,
        claim_rounds: int = 1,
    ) -> None:
        """Bind the catalog to the interferences it runs around the record transitions it is given."""
        super().__init__(backend)
        self._before_mark = before_mark
        self._after_mark = after_mark
        self._before_claim = before_claim
        self._before_tombstone = before_tombstone
        self._remaining = rounds
        self._claim_rounds = claim_rounds

    def put(self, dataset: Dataset, *, revision: str | None = None, create: bool = False) -> None:
        """Write a record, running the interference of the transition it stands for before it lands."""
        racing = revision is not None and dataset.is_deleting and self._remaining > 0
        if racing:
            self._remaining -= 1
            if self._before_mark is not None:
                self._before_mark()
        claiming = create or (revision is not None and dataset.is_live)
        if claiming and self._claim_rounds > 0 and self._before_claim is not None:
            # Bounded by claim_rounds, one by default: a write retries a lost claim against whatever
            # the record has become, and must be free to win that retry, and to sweep what it wrote
            # when it does not, without tripping the interference again.
            self._claim_rounds -= 1
            self._before_claim()
        if revision is not None and dataset.is_tombstone and self._before_tombstone is not None:
            # Once only, for the same reason: the interference finishes this deletion itself, and the
            # tombstone below has to be free to lose its compare-and-swap to whatever it left.
            interference, self._before_tombstone = self._before_tombstone, None
            interference()
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
    return storage_backend.list_keys(storage_backend.address(VECTOR_PREFIX, COLLECTION))


def raster_keys(storage_backend: StorageBackend) -> list[str]:
    """List every object still stored below the prefix of the test coverage, in every generation."""
    return storage_backend.list_keys(storage_backend.address(RASTER_PREFIX, COVERAGE))


def live_collection_record(catalog: ObjectCatalog) -> FeatureDataset:
    """Return the record of the test collection, which is the only thing naming its live generation."""
    record = catalog.require(COLLECTION)
    assert isinstance(record, FeatureDataset)
    assert record.is_live is True
    return record


def live_coverage_record(catalog: ObjectCatalog) -> CoverageDataset:
    """Return the record of the test coverage, which is the only thing naming its live generation."""
    record = catalog.require(COVERAGE)
    assert isinstance(record, CoverageDataset)
    assert record.is_live is True
    return record


def assert_tombstoned(catalog: ObjectCatalog, identifier: str) -> None:
    """Assert a finished deletion left the tombstone of an identifier behind rather than a dataset."""
    record = catalog.require(identifier)
    assert record.is_tombstone is True
    assert [listed.dataset_identifier for listed in catalog.list_datasets()] == []


def assert_stored_below(storage_backend: StorageBackend, record: Dataset, stored: list[str]) -> None:
    """Assert every object left under the identifier belongs to the generation the record names."""
    marker = storage_backend.address(record.storage_key).key
    assert stored
    assert all(key.startswith(f"{marker}/") for key in stored)


def assert_no_stray_raster_objects(storage_backend: StorageBackend, record: Dataset) -> None:
    """Assert nothing under the raster identifier stands outside the generation the record names."""
    # The memory backend holds Icechunk repositories in an in-memory store of its own rather than as
    # objects, so its listing is empty and it is the filesystem parameter that checks the real layout.
    marker = storage_backend.address(record.storage_key).key
    assert all(key.startswith(f"{marker}/") for key in raster_keys(storage_backend))


def assert_minted_generation(record: Dataset, engine_prefix: str, identifier: str) -> None:
    """Assert the storage key of a record is an engine prefix, an identifier and a generation token."""
    engine, name, generation = record.storage_key.split("/")
    assert (engine, name) == (engine_prefix, identifier)
    assert validate_generation_token(generation) == generation


def mark_deleting(catalog: ObjectCatalog, identifier: str) -> None:
    """Leave an identifier in the state a deletion that stopped before its sweep leaves behind."""
    entry = catalog.require_entry(identifier)
    catalog.put(entry.record.model_copy(update={"lifecycle": DatasetLifecycle.DELETING}), revision=entry.revision)


def bump_the_dead_record(catalog: ObjectCatalog, identifier: str) -> None:
    """Move the dead record of an identifier on without reviving it, so every claim against it loses."""
    entry = catalog.require_entry(identifier)
    assert entry.record.is_live is False
    # The bytes have to change: an S3 etag is the digest of the object, so rewriting a record
    # unchanged would leave the revision a claim is racing exactly where it was.
    catalog.put(entry.record.model_copy(update={"updated_at": current_timestamp()}), revision=entry.revision)


def sweep_after(
    storage_backend: StorageBackend,
    monkeypatch: pytest.MonkeyPatch,
    interference: Callable[[], None],
) -> list[str]:
    """Run an interference once, between a deleter taking its reservation and its sweep, recording both."""
    swept: list[str] = []
    sweep = storage_backend.delete_prefix

    def recording_sweep(address: StorageAddress) -> int:
        swept.append(address.key)
        return sweep(address)

    def interfering_sweep(address: StorageAddress) -> int:
        # The interference sweeps too, so it runs against the recording sweep rather than this one.
        monkeypatch.setattr(storage_backend, "delete_prefix", recording_sweep)
        interference()
        return recording_sweep(address)

    monkeypatch.setattr(storage_backend, "delete_prefix", interfering_sweep)
    return swept


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
    assert_tombstoned(ObjectCatalog(storage_backend), COLLECTION)
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
    assert record.is_live is True
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

    racing = RacingCatalog(storage_backend, before_mark=lambda: mark_deleting(plain, COLLECTION))
    deleter = build_collection_store(storage_backend, settings, racing)

    removed = deleter.delete(COLLECTION)

    # The re-read found the record already reserved, and finishing that deletion is what this call is for.
    assert removed > 0
    assert_tombstoned(plain, COLLECTION)
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
    assert_tombstoned(plain, COLLECTION)
    assert vector_keys(storage_backend) == []


def test_a_raster_deletion_that_loses_its_mark_to_a_writer_retries_and_finishes(
    storage_backend: StorageBackend,
    settings: Settings,
) -> None:
    writer = published_coverage(storage_backend, settings)
    racing = RacingCatalog(storage_backend, before_mark=appending_writer(writer))
    deleter = RasterRepository(storage_backend, racing, settings)

    deleter.delete(COVERAGE)

    assert_tombstoned(ObjectCatalog(storage_backend), COVERAGE)
    assert raster_keys(storage_backend) == []


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
    assert record.is_live is True
    # The published snapshot still reads, and every timestep the racing appends committed is still there.
    assert reader.query(COVERAGE).cell_count == 2 * 4 * 6
    draft = reader.describe(COVERAGE, version=VersionSelector.DRAFT)
    assert draft.timestep_count == 2 + 2 * RASTER_DELETION_ATTEMPTS


def test_a_raster_deletion_that_loses_to_another_deleter_finishes_that_deletion(
    storage_backend: StorageBackend,
    settings: Settings,
) -> None:
    plain = ObjectCatalog(storage_backend)
    published_coverage(storage_backend, settings)

    racing = RacingCatalog(storage_backend, before_mark=lambda: mark_deleting(plain, COVERAGE))
    deleter = RasterRepository(storage_backend, racing, settings)

    deleter.delete(COVERAGE)

    assert_tombstoned(plain, COVERAGE)
    assert raster_keys(storage_backend) == []


def test_a_raster_deletion_that_loses_to_a_finished_deleter_sweeps_nothing(
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

    def finish_the_deletion() -> None:
        RasterRepository(storage_backend, plain, settings).delete(COVERAGE)

    monkeypatch.setattr(storage_backend, "delete_prefix", counting_sweep)
    racing = RacingCatalog(storage_backend, before_mark=finish_the_deletion)

    RasterRepository(storage_backend, racing, settings).delete(COVERAGE)

    # Only the deleter that held the reservation swept; the one that lost the record swept nothing.
    assert len(swept) == 1
    assert_tombstoned(plain, COVERAGE)
    assert raster_keys(storage_backend) == []


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
    assert record.is_live is True
    assert reader.versions(COLLECTION) == [1]
    assert reader.current_version(COLLECTION) == 1
    assert len(reader.read(COLLECTION).frame) == 4


def test_a_vector_deletion_that_resumes_after_a_recreation_never_touches_the_new_generation(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = ObjectCatalog(storage_backend)
    writer = build_collection_store(storage_backend, settings, plain)
    writer.write(COLLECTION, sample_features, identifier_property="id", publish=True)
    doomed = live_collection_record(plain).storage_key

    def finish_the_deletion_and_write_again() -> None:
        # The deleter holds its reservation and has not swept yet. A writer that arrives now sees the
        # marked record, finishes that deletion, and creates the collection again with a generation of
        # its own, which the deleter never reserved and must never sweep.
        other = build_collection_store(storage_backend, settings, plain)
        other.write(COLLECTION, sample_features.iloc[:4], identifier_property="id", publish=True)

    swept = sweep_after(storage_backend, monkeypatch, finish_the_deletion_and_write_again)
    deleter = build_collection_store(storage_backend, settings, plain)

    removed = deleter.delete(COLLECTION)

    # Both sweeps emptied the generation of the record the deletion was reserved on, and the deleter
    # resumed into a prefix the writer had already emptied, so it removed nothing.
    assert swept == [storage_backend.address(doomed).key] * 2
    assert removed == 0
    record = live_collection_record(plain)
    assert record.storage_key != doomed
    # The recreated collection is live, complete and fully readable: record, versions, pointer and data.
    reader = build_collection_store(storage_backend, settings, ObjectCatalog(storage_backend))
    assert reader.versions(COLLECTION) == [1]
    assert reader.current_version(COLLECTION) == 1
    assert len(reader.read(COLLECTION).frame) == 4
    assert reader.version_metadata(COLLECTION, 1).feature_count == 4
    assert_stored_below(storage_backend, record, vector_keys(storage_backend))


def test_a_vector_write_that_loses_the_conditional_create_sweeps_the_generation_it_minted(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    plain = ObjectCatalog(storage_backend)
    winner = build_collection_store(storage_backend, settings, plain)

    def claim_the_identifier_first() -> None:
        winner.write(COLLECTION, sample_features.iloc[:4], identifier_property="id", publish=True)

    racing = RacingCatalog(storage_backend, before_claim=claim_the_identifier_first)
    loser = build_collection_store(storage_backend, settings, racing)

    with pytest.raises(DatasetAlreadyExistsError):
        loser.write(COLLECTION, sample_features, identifier_property="id")

    # Nobody else knew the token of the generation the loser minted, so it swept it rather than
    # leaving a version nothing names under the identifier forever.
    record = live_collection_record(plain)
    assert record.features.feature_count == 4
    assert_stored_below(storage_backend, record, vector_keys(storage_backend))


def test_a_vector_collection_written_again_after_a_deletion_lands_in_a_new_generation(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    plain = ObjectCatalog(storage_backend)
    store = build_collection_store(storage_backend, settings, plain)
    store.write(COLLECTION, sample_features, identifier_property="id", publish=True)
    first = live_collection_record(plain).storage_key

    store.delete(COLLECTION)
    store.write(COLLECTION, sample_features.iloc[:4], identifier_property="id", publish=True)

    record = live_collection_record(plain)
    assert record.storage_key != first
    assert_minted_generation(record, "vector", COLLECTION)
    # The record is the only thing that says where the live data is, and it says it truthfully.
    assert_stored_below(storage_backend, record, vector_keys(storage_backend))
    assert store.read(COLLECTION).version == 1
    assert len(store.read(COLLECTION).frame) == 4


def test_two_vector_writers_taking_over_one_deletion_leave_the_winner_whole(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = ObjectCatalog(storage_backend)
    first_writer = build_collection_store(storage_backend, settings, plain)
    first_writer.write(COLLECTION, sample_features, identifier_property="id", publish=True)
    doomed = live_collection_record(plain).storage_key
    mark_deleting(plain, COLLECTION)

    def take_the_deletion_over_first() -> None:
        # The winner sees the same marked record, finishes the same deletion, and creates the
        # collection again in a generation of its own while the loser is still inside its own sweep.
        build_collection_store(storage_backend, settings, plain).write(
            COLLECTION,
            sample_features.iloc[:4],
            identifier_property="id",
            publish=True,
        )

    swept = sweep_after(storage_backend, monkeypatch, take_the_deletion_over_first)
    loser = build_collection_store(storage_backend, settings, plain)

    with pytest.raises(DatasetAlreadyExistsError):
        loser.write(COLLECTION, sample_features.iloc[:3], identifier_property="id")

    # Both takeovers swept the dead generation, the loser left the winner's record alone because the
    # revision no longer matched, and then swept the generation it had minted for itself.
    winner = live_collection_record(plain)
    doomed_key = storage_backend.address(doomed).key
    assert swept[:2] == [doomed_key, doomed_key]
    assert len(swept) == 3
    assert swept[2] not in (doomed_key, storage_backend.address(winner.storage_key).key)
    assert winner.features.feature_count == 4
    assert_stored_below(storage_backend, winner, vector_keys(storage_backend))
    reader = build_collection_store(storage_backend, settings, ObjectCatalog(storage_backend))
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
    assert record.is_live is True
    assert reader.query(COVERAGE).cell_count == 1 * 4 * 6
    assert reader.describe(COVERAGE).timestep_count == 1


def test_a_raster_deletion_that_resumes_after_a_recreation_never_touches_the_new_generation(
    storage_backend: StorageBackend,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = ObjectCatalog(storage_backend)
    published_coverage(storage_backend, settings)
    doomed = live_coverage_record(plain).storage_key

    def finish_the_deletion_and_create_again() -> None:
        # The deleter holds its reservation and has not swept yet. A writer that arrives now sees the
        # marked record, finishes that deletion, and creates the coverage again in a generation of its
        # own, which the deleter never reserved and must never sweep.
        other = RasterRepository(storage_backend, plain, settings)
        other.create(COVERAGE, build_grid(), build_cube(build_grid(), count=1))
        other.publish(COVERAGE)

    swept = sweep_after(storage_backend, monkeypatch, finish_the_deletion_and_create_again)

    RasterRepository(storage_backend, plain, settings).delete(COVERAGE)

    # Both sweeps emptied the repository of the record the deletion was reserved on, and the deleter
    # resumed into a prefix the writer had already emptied.
    assert swept == [storage_backend.address(doomed).key] * 2
    record = live_coverage_record(plain)
    assert record.storage_key != doomed
    # The reviewer reproduced this as a successful write answering SnapshotNotFoundError afterwards
    # with a live record standing over an empty prefix. The recreated coverage reads instead.
    reader = RasterRepository(storage_backend, ObjectCatalog(storage_backend), settings)
    assert reader.query(COVERAGE).cell_count == 1 * 4 * 6
    assert reader.describe(COVERAGE).timestep_count == 1
    assert_no_stray_raster_objects(storage_backend, record)


def test_a_raster_create_that_loses_the_conditional_create_sweeps_the_generation_it_minted(
    storage_backend: StorageBackend,
    settings: Settings,
) -> None:
    plain = ObjectCatalog(storage_backend)
    winner = RasterRepository(storage_backend, plain, settings)

    def claim_the_identifier_first() -> None:
        winner.create(COVERAGE, build_grid(), build_cube(build_grid(), count=1))
        winner.publish(COVERAGE)

    racing = RacingCatalog(storage_backend, before_claim=claim_the_identifier_first)
    loser = RasterRepository(storage_backend, racing, settings)

    with pytest.raises(DatasetAlreadyExistsError):
        loser.create(COVERAGE, build_grid(), build_cube(build_grid()))

    # Nobody else knew the token of the generation the loser minted, so it swept the repository it
    # had created there rather than leaving one nothing names under the identifier forever.
    record = live_coverage_record(plain)
    assert record.timestep_count == 1
    assert_no_stray_raster_objects(storage_backend, record)
    reader = RasterRepository(storage_backend, ObjectCatalog(storage_backend), settings)
    assert reader.query(COVERAGE).cell_count == 1 * 4 * 6


def test_a_raster_coverage_created_again_after_a_deletion_lands_in_a_new_generation(
    storage_backend: StorageBackend,
    settings: Settings,
) -> None:
    plain = ObjectCatalog(storage_backend)
    repository = published_coverage(storage_backend, settings)
    first = live_coverage_record(plain).storage_key

    repository.delete(COVERAGE)
    repository.create(COVERAGE, build_grid(), build_cube(build_grid(), count=1))
    repository.publish(COVERAGE)

    record = live_coverage_record(plain)
    assert record.storage_key != first
    assert_minted_generation(record, "raster", COVERAGE)
    # The record is the only thing that says where the live repository is, and it says it truthfully.
    assert_no_stray_raster_objects(storage_backend, record)
    assert repository.describe(COVERAGE).timestep_count == 1


def test_an_overwrite_of_a_live_coverage_stays_in_the_generation_of_its_record(
    storage_backend: StorageBackend,
    settings: Settings,
) -> None:
    plain = ObjectCatalog(storage_backend)
    repository = published_coverage(storage_backend, settings)
    created = live_coverage_record(plain).storage_key

    repository.create(COVERAGE, build_grid(), build_cube(build_grid(), count=1), overwrite=True)

    # An overwrite replaces the record it read, so it writes into the repository that record names
    # rather than minting a generation the publication block of the old record would not describe.
    assert live_coverage_record(plain).storage_key == created
    assert repository.describe(COVERAGE, version=VersionSelector.DRAFT).timestep_count == 1


def test_two_raster_writers_taking_over_one_deletion_leave_the_winner_whole(
    storage_backend: StorageBackend,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = ObjectCatalog(storage_backend)
    published_coverage(storage_backend, settings)
    doomed = live_coverage_record(plain).storage_key
    mark_deleting(plain, COVERAGE)

    def take_the_deletion_over_first() -> None:
        # The winner sees the same marked record, finishes the same deletion, and creates the coverage
        # again in a generation of its own while the loser is still inside its own sweep.
        other = RasterRepository(storage_backend, plain, settings)
        other.create(COVERAGE, build_grid(), build_cube(build_grid(), count=1))
        other.publish(COVERAGE)

    swept = sweep_after(storage_backend, monkeypatch, take_the_deletion_over_first)
    loser = RasterRepository(storage_backend, plain, settings)

    with pytest.raises(DatasetAlreadyExistsError):
        loser.create(COVERAGE, build_grid(), build_cube(build_grid()))

    # Both takeovers swept the dead generation, the loser left the winner's record alone because the
    # revision no longer matched, and then swept the generation it had minted for itself.
    winner = live_coverage_record(plain)
    doomed_key = storage_backend.address(doomed).key
    assert swept[:2] == [doomed_key, doomed_key]
    assert len(swept) == 3
    assert swept[2] not in (doomed_key, storage_backend.address(winner.storage_key).key)
    assert_no_stray_raster_objects(storage_backend, winner)
    reader = RasterRepository(storage_backend, ObjectCatalog(storage_backend), settings)
    assert reader.query(COVERAGE).cell_count == 1 * 4 * 6
    assert reader.describe(COVERAGE).timestep_count == 1


def test_a_vector_deletion_that_loses_its_tombstone_leaves_the_recreated_collection_whole(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    plain = ObjectCatalog(storage_backend)
    build_collection_store(storage_backend, settings, plain).write(
        COLLECTION,
        sample_features,
        identifier_property="id",
        publish=True,
    )

    def finish_the_deletion_and_write_again() -> None:
        # The window the unconditional record delete left open: between the last read of this deleter
        # and its last write, another actor finishes the same deletion and a writer claims the name
        # again. The delete that followed removed that writer's record, and the collection answered
        # 404 over objects nothing named any more.
        other = build_collection_store(storage_backend, settings, plain)
        other.delete(COLLECTION)
        other.write(COLLECTION, sample_features.iloc[:4], identifier_property="id", publish=True)

    racing = RacingCatalog(storage_backend, before_tombstone=finish_the_deletion_and_write_again)

    removed = build_collection_store(storage_backend, settings, racing).delete(COLLECTION)

    # The deleter emptied the generation it reserved, which was its own and already dead, and then
    # lost its tombstone to the record that writer created: the recreated collection is live, listed
    # and readable rather than answering 404 over objects nothing names.
    assert removed > 0
    record = live_collection_record(plain)
    assert [listed.dataset_identifier for listed in plain.list_datasets()] == [COLLECTION]
    reader = build_collection_store(storage_backend, settings, ObjectCatalog(storage_backend))
    assert reader.versions(COLLECTION) == [1]
    assert reader.current_version(COLLECTION) == 1
    assert len(reader.read(COLLECTION).frame) == 4
    assert_stored_below(storage_backend, record, vector_keys(storage_backend))


def test_a_raster_deletion_that_loses_its_tombstone_leaves_the_recreated_coverage_whole(
    storage_backend: StorageBackend,
    settings: Settings,
) -> None:
    plain = ObjectCatalog(storage_backend)
    published_coverage(storage_backend, settings)

    def finish_the_deletion_and_create_again() -> None:
        # The same window as the vector case: another actor finishes this deletion and a writer
        # creates the coverage again, between the last read of this deleter and its last write.
        other = RasterRepository(storage_backend, plain, settings)
        other.delete(COVERAGE)
        other.create(COVERAGE, build_grid(), build_cube(build_grid(), count=1))
        other.publish(COVERAGE)

    racing = RacingCatalog(storage_backend, before_tombstone=finish_the_deletion_and_create_again)

    RasterRepository(storage_backend, racing, settings).delete(COVERAGE)

    record = live_coverage_record(plain)
    assert [listed.dataset_identifier for listed in plain.list_datasets()] == [COVERAGE]
    reader = RasterRepository(storage_backend, ObjectCatalog(storage_backend), settings)
    assert reader.query(COVERAGE).cell_count == 1 * 4 * 6
    assert reader.describe(COVERAGE).timestep_count == 1
    assert_no_stray_raster_objects(storage_backend, record)


def test_a_vector_collection_written_over_a_tombstone_inherits_nothing_from_the_dead_one(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    plain = ObjectCatalog(storage_backend)
    store = build_collection_store(storage_backend, settings, plain)
    store.write(
        COLLECTION,
        sample_features,
        identifier_property="id",
        title="Before the deletion",
        license="CC-BY-4.0",
        attribution="Statistics Norway",
        publish=True,
    )
    first = live_collection_record(plain)

    store.delete(COLLECTION)
    store.write(COLLECTION, sample_features.iloc[:4], identifier_property="id")

    # A write over a tombstone is a first write in every respect but the compare-and-swap it claims
    # the identifier with: a generation of its own, and nothing carried over from the dead collection.
    record = live_collection_record(plain)
    assert record.storage_key != first.storage_key
    assert_minted_generation(record, "vector", COLLECTION)
    assert record.title == COLLECTION
    assert record.license is None
    assert record.attribution is None
    assert record.publication.published is False
    assert record.created_at == record.updated_at
    assert record.created_at != first.created_at
    assert store.versions(COLLECTION) == [1]


def test_a_raster_coverage_created_over_a_tombstone_inherits_nothing_from_the_dead_one(
    storage_backend: StorageBackend,
    settings: Settings,
) -> None:
    plain = ObjectCatalog(storage_backend)
    repository = RasterRepository(storage_backend, plain, settings)
    repository.create(
        COVERAGE,
        build_grid(),
        build_cube(build_grid()),
        title="Before the deletion",
        license="CC-BY-4.0",
        attribution="Statistics Norway",
    )
    repository.publish(COVERAGE)
    first = live_coverage_record(plain)

    repository.delete(COVERAGE)
    # No overwrite flag: a tombstone is replaced by a first write rather than overwritten by one.
    repository.create(COVERAGE, build_grid(), build_cube(build_grid(), count=1))

    record = live_coverage_record(plain)
    assert record.storage_key != first.storage_key
    assert_minted_generation(record, "raster", COVERAGE)
    assert record.title == COVERAGE
    assert record.license is None
    assert record.attribution is None
    assert record.publication.published is False
    assert record.created_at == record.updated_at
    assert record.created_at != first.created_at


def test_a_coverage_is_created_over_the_tombstone_of_a_collection(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    plain = ObjectCatalog(storage_backend)
    store = build_collection_store(storage_backend, settings, plain)
    store.write(COLLECTION, sample_features, identifier_property="id", publish=True)
    store.delete(COLLECTION)

    # The tombstone is checked before the item type is, so the name is free for either engine again.
    repository = RasterRepository(storage_backend, plain, settings)
    repository.create(COLLECTION, build_grid(), build_cube(build_grid(), count=1))
    repository.publish(COLLECTION)

    record = plain.require(COLLECTION)
    assert isinstance(record, CoverageDataset)
    assert record.is_live is True
    assert_minted_generation(record, "raster", COLLECTION)
    assert repository.query(COLLECTION).cell_count == 1 * 4 * 6


def test_a_collection_is_written_over_the_tombstone_of_a_coverage(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    plain = ObjectCatalog(storage_backend)
    RasterRepository(storage_backend, plain, settings).create(COVERAGE, build_grid(), build_cube(build_grid()))
    RasterRepository(storage_backend, plain, settings).delete(COVERAGE)

    store = build_collection_store(storage_backend, settings, plain)
    store.write(COVERAGE, sample_features.iloc[:4], identifier_property="id", publish=True)

    record = plain.require(COVERAGE)
    assert isinstance(record, FeatureDataset)
    assert record.is_live is True
    assert_minted_generation(record, "vector", COVERAGE)
    assert len(store.read(COVERAGE).frame) == 4


def test_two_vector_writers_over_one_tombstone_leave_the_winner_whole(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    plain = ObjectCatalog(storage_backend)
    store = build_collection_store(storage_backend, settings, plain)
    store.write(COLLECTION, sample_features, identifier_property="id", publish=True)
    store.delete(COLLECTION)

    def claim_the_tombstone_first() -> None:
        build_collection_store(storage_backend, settings, plain).write(
            COLLECTION,
            sample_features.iloc[:4],
            identifier_property="id",
            publish=True,
        )

    racing = RacingCatalog(storage_backend, before_claim=claim_the_tombstone_first)
    loser = build_collection_store(storage_backend, settings, racing)

    with pytest.raises(DatasetAlreadyExistsError):
        loser.write(COLLECTION, sample_features.iloc[:3], identifier_property="id")

    # Both writers replaced the same tombstone under compare-and-swap, so exactly one holds the
    # identifier, and the loser swept the generation it had minted rather than leaving it behind.
    winner = live_collection_record(plain)
    assert winner.features.feature_count == 4
    assert_stored_below(storage_backend, winner, vector_keys(storage_backend))
    reader = build_collection_store(storage_backend, settings, ObjectCatalog(storage_backend))
    assert reader.versions(COLLECTION) == [1]
    assert len(reader.read(COLLECTION).frame) == 4


def test_two_raster_creates_over_one_tombstone_leave_the_winner_whole(
    storage_backend: StorageBackend,
    settings: Settings,
) -> None:
    plain = ObjectCatalog(storage_backend)
    published_coverage(storage_backend, settings)
    RasterRepository(storage_backend, plain, settings).delete(COVERAGE)

    def claim_the_tombstone_first() -> None:
        other = RasterRepository(storage_backend, plain, settings)
        other.create(COVERAGE, build_grid(), build_cube(build_grid(), count=1))
        other.publish(COVERAGE)

    racing = RacingCatalog(storage_backend, before_claim=claim_the_tombstone_first)
    loser = RasterRepository(storage_backend, racing, settings)

    with pytest.raises(DatasetAlreadyExistsError):
        loser.create(COVERAGE, build_grid(), build_cube(build_grid()))

    winner = live_coverage_record(plain)
    assert_no_stray_raster_objects(storage_backend, winner)
    reader = RasterRepository(storage_backend, ObjectCatalog(storage_backend), settings)
    assert reader.query(COVERAGE).cell_count == 1 * 4 * 6
    assert reader.describe(COVERAGE).timestep_count == 1


def test_a_vector_collection_can_be_deleted_written_again_and_deleted_again(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    plain = ObjectCatalog(storage_backend)
    store = build_collection_store(storage_backend, settings, plain)
    store.write(COLLECTION, sample_features, identifier_property="id", publish=True)

    assert store.delete(COLLECTION) > 0
    store.write(COLLECTION, sample_features.iloc[:4], identifier_property="id", publish=True)
    assert store.delete(COLLECTION) > 0

    # The second deletion reserved the tombstone of the first, marked it and tombstoned it again.
    assert_tombstoned(plain, COLLECTION)
    assert vector_keys(storage_backend) == []


def test_a_raster_coverage_can_be_deleted_created_again_and_deleted_again(
    storage_backend: StorageBackend,
    settings: Settings,
) -> None:
    plain = ObjectCatalog(storage_backend)
    repository = RasterRepository(storage_backend, plain, settings)
    repository.create(COVERAGE, build_grid(), build_cube(build_grid()))
    repository.publish(COVERAGE)

    repository.delete(COVERAGE)
    repository.create(COVERAGE, build_grid(), build_cube(build_grid(), count=1))
    repository.publish(COVERAGE)
    repository.delete(COVERAGE)

    assert_tombstoned(plain, COVERAGE)
    assert raster_keys(storage_backend) == []


def test_deleting_a_tombstoned_dataset_answers_as_an_absent_one(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    plain = ObjectCatalog(storage_backend)
    store = build_collection_store(storage_backend, settings, plain)
    store.write(COLLECTION, sample_features, identifier_property="id", publish=True)
    store.delete(COLLECTION)
    repository = RasterRepository(storage_backend, plain, settings)
    repository.create(COVERAGE, build_grid(), build_cube(build_grid()))
    repository.delete(COVERAGE)

    with pytest.raises(DatasetNotFoundError):
        store.delete(COLLECTION)
    with pytest.raises(DatasetNotFoundError):
        repository.delete(COVERAGE)
    with pytest.raises(DatasetNotFoundError):
        store.read(COLLECTION)
    with pytest.raises(DatasetNotFoundError):
        repository.describe(COVERAGE)
    # A tombstone of the other item type is refused as absent too, rather than as a type mismatch.
    with pytest.raises(DatasetNotFoundError):
        store.delete(COVERAGE)
    with pytest.raises(DatasetNotFoundError):
        repository.delete(COLLECTION)


def test_a_vector_write_taking_over_a_running_deletion_wins_when_the_deleter_tombstones_first(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    plain = ObjectCatalog(storage_backend)
    build_collection_store(storage_backend, settings, plain).write(
        COLLECTION,
        sample_features,
        identifier_property="id",
        publish=True,
    )
    doomed = live_collection_record(plain).storage_key
    mark_deleting(plain, COLLECTION)

    def let_the_deleter_finish() -> None:
        # The ordinary case rather than a rare race: the deletion this write took over is still
        # running, and it swaps its mark for a tombstone while the write is putting its bytes down.
        build_collection_store(storage_backend, settings, plain).delete(COLLECTION)

    racing = RacingCatalog(storage_backend, before_claim=let_the_deleter_finish)
    writer = build_collection_store(storage_backend, settings, racing)

    result = writer.write(COLLECTION, sample_features.iloc[:4], identifier_property="id", publish=True)

    # The claim lost to the tombstone and was retried against it. Nobody owned the name, so the write
    # keeps the generation it had already filled instead of throwing it away and answering 409.
    assert result.version == 1
    record = live_collection_record(plain)
    assert record.storage_key != doomed
    assert_minted_generation(record, "vector", COLLECTION)
    reader = build_collection_store(storage_backend, settings, ObjectCatalog(storage_backend))
    assert reader.versions(COLLECTION) == [1]
    assert reader.current_version(COLLECTION) == 1
    assert len(reader.read(COLLECTION).frame) == 4
    assert_stored_below(storage_backend, record, vector_keys(storage_backend))


def test_a_raster_create_taking_over_a_running_deletion_wins_when_the_deleter_tombstones_first(
    storage_backend: StorageBackend,
    settings: Settings,
) -> None:
    plain = ObjectCatalog(storage_backend)
    published_coverage(storage_backend, settings)
    doomed = live_coverage_record(plain).storage_key
    mark_deleting(plain, COVERAGE)

    def let_the_deleter_finish() -> None:
        RasterRepository(storage_backend, plain, settings).delete(COVERAGE)

    racing = RacingCatalog(storage_backend, before_claim=let_the_deleter_finish)
    writer = RasterRepository(storage_backend, racing, settings)

    writer.create(COVERAGE, build_grid(), build_cube(build_grid(), count=1))

    record = live_coverage_record(plain)
    assert record.storage_key != doomed
    assert_minted_generation(record, "raster", COVERAGE)
    reader = RasterRepository(storage_backend, ObjectCatalog(storage_backend), settings)
    assert reader.describe(COVERAGE, version=VersionSelector.DRAFT).timestep_count == 1
    assert_no_stray_raster_objects(storage_backend, record)


def test_a_vector_claim_that_keeps_losing_to_a_moving_dead_record_is_refused_and_leaves_nothing(
    storage_backend: StorageBackend,
    settings: Settings,
    sample_features: geopandas.GeoDataFrame,
) -> None:
    plain = ObjectCatalog(storage_backend)
    build_collection_store(storage_backend, settings, plain).write(
        COLLECTION,
        sample_features,
        identifier_property="id",
        publish=True,
    )
    mark_deleting(plain, COLLECTION)

    racing = RacingCatalog(
        storage_backend,
        before_claim=lambda: bump_the_dead_record(plain, COLLECTION),
        claim_rounds=VECTOR_CLAIM_ATTEMPTS,
    )
    writer = build_collection_store(storage_backend, settings, racing)

    with pytest.raises(PublicationConflictError):
        writer.write(COLLECTION, sample_features.iloc[:4], identifier_property="id")

    # Every attempt raced a record that moved again, so the write gave up and swept the generation it
    # had minted: nothing of it survives, and the dead record is still dead.
    assert plain.require(COLLECTION).is_live is False
    assert vector_keys(storage_backend) == []


def test_a_raster_claim_that_keeps_losing_to_a_moving_dead_record_is_refused_and_leaves_nothing(
    storage_backend: StorageBackend,
    settings: Settings,
) -> None:
    plain = ObjectCatalog(storage_backend)
    published_coverage(storage_backend, settings)
    mark_deleting(plain, COVERAGE)

    racing = RacingCatalog(
        storage_backend,
        before_claim=lambda: bump_the_dead_record(plain, COVERAGE),
        claim_rounds=RASTER_CLAIM_ATTEMPTS,
    )
    writer = RasterRepository(storage_backend, racing, settings)

    with pytest.raises(PublicationConflictError):
        writer.create(COVERAGE, build_grid(), build_cube(build_grid(), count=1))

    assert plain.require(COVERAGE).is_live is False
    assert raster_keys(storage_backend) == []
