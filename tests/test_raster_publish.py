from __future__ import annotations

from datetime import datetime
from typing import Any

import icechunk
import numpy
import pytest
import xarray

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import (
    DatasetAlreadyExistsError,
    NothingToPublishError,
    PublicationConflictError,
    SnapshotNotFoundError,
)
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.raster import (
    MAIN_BRANCH,
    PUBLISHED_BRANCH,
    CoverageEntry,
    CoverageWriteTarget,
    RasterRepository,
    TimeStep,
    VersionSelector,
    build_synthetic_cube,
    build_timestamps,
)
from ocs_storage_exploration.storage.raster import repository as repository_module
from ocs_storage_exploration.storage.schemas import (
    BoundingBox,
    CoverageDataset,
    Dataset,
    GridSpecification,
    ItemType,
    TemporalExtent,
)

IDENTIFIER = "publish"
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
    return build_synthetic_cube(
        grid,
        variable=VARIABLE,
        timestamps=build_timestamps(start, count, TimeStep.DAY),
        seed=seed,
    )


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


def test_publish_moves_the_branch_and_updates_the_record(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    catalog: ObjectCatalog,
    storage_backend: StorageBackend,
):
    created = raster_repository.create(IDENTIFIER, grid, build_cube(grid))

    result = raster_repository.publish(IDENTIFIER)

    assert result.item_type is ItemType.COVERAGE
    assert result.published is True
    assert result.changed is True
    assert result.snapshot_identifier == created.snapshot_identifier
    assert result.previous_snapshot_identifier is None
    record = catalog.require(IDENTIFIER)
    assert isinstance(record, CoverageDataset)
    assert record.publication.published is True
    assert record.publication.snapshot_identifier == created.snapshot_identifier
    assert record.publication.published_at is not None
    address = raster_repository.repository_address(IDENTIFIER)
    branches = icechunk.Repository.open(storage_backend.icechunk_storage(address)).list_branches()
    assert PUBLISHED_BRANCH in branches


def test_published_reader_keeps_the_published_snapshot_while_main_moves_on(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    raster_repository.publish(IDENTIFIER)

    raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))

    with raster_repository.read(IDENTIFIER, version=VersionSelector.PUBLISHED) as handle:
        assert handle.dataset.sizes["t"] == 3
    with raster_repository.read(IDENTIFIER, version=VersionSelector.DRAFT) as handle:
        assert handle.dataset.sizes["t"] == 5


def test_publishing_the_newest_snapshot_moves_the_pointer_forward(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    first = raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    raster_repository.publish(IDENTIFIER)
    second = raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))

    result = raster_repository.publish(IDENTIFIER)

    assert result.changed is True
    assert result.snapshot_identifier == second.snapshot_identifier
    assert result.previous_snapshot_identifier == first.snapshot_identifier
    with raster_repository.read(IDENTIFIER) as handle:
        assert handle.dataset.sizes["t"] == 5


def test_rollback_publishes_an_older_snapshot_again(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    catalog: ObjectCatalog,
):
    original = build_cube(grid, count=3)
    first = raster_repository.create(IDENTIFIER, grid, original)
    second = raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))
    raster_repository.publish(IDENTIFIER)

    result = raster_repository.publish(IDENTIFIER, snapshot_identifier=first.snapshot_identifier)

    assert result.changed is True
    assert result.snapshot_identifier == first.snapshot_identifier
    assert result.previous_snapshot_identifier == second.snapshot_identifier
    with raster_repository.read(IDENTIFIER) as handle:
        assert handle.dataset.sizes["t"] == 3
        assert numpy.allclose(handle.dataset[VARIABLE].values, original[VARIABLE].values)
    record = catalog.require(IDENTIFIER)
    assert isinstance(record, CoverageDataset)
    assert record.publication.snapshot_identifier == first.snapshot_identifier
    assert record.publication.previous_snapshot_identifier == second.snapshot_identifier


def test_republishing_the_same_snapshot_reports_no_change(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    created = raster_repository.create(IDENTIFIER, grid, build_cube(grid))
    raster_repository.publish(IDENTIFIER)

    result = raster_repository.publish(IDENTIFIER)

    assert result.changed is False
    assert result.published is True
    assert result.snapshot_identifier == created.snapshot_identifier
    assert result.previous_snapshot_identifier == created.snapshot_identifier


def test_publish_refuses_a_snapshot_outside_the_ancestry(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))

    with pytest.raises(SnapshotNotFoundError):
        raster_repository.publish(IDENTIFIER, snapshot_identifier="ZZZZZZZZZZZZZZZZZZZZ")


def test_read_refuses_a_snapshot_outside_the_ancestry(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))

    with pytest.raises(SnapshotNotFoundError), raster_repository.read(IDENTIFIER, snapshot_identifier="ZZZZZZZZZZZZ"):
        pass


def test_reading_an_explicit_snapshot_ignores_the_published_pointer(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    first = raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))
    raster_repository.publish(IDENTIFIER)

    with raster_repository.read(IDENTIFIER, snapshot_identifier=first.snapshot_identifier) as handle:
        assert handle.snapshot_identifier == first.snapshot_identifier
        assert handle.dataset.sizes["t"] == 3


def test_a_read_handle_opened_before_a_publish_still_reads_its_snapshot(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    original = build_cube(grid, count=3)
    raster_repository.create(IDENTIFIER, grid, original)
    raster_repository.publish(IDENTIFIER)

    with raster_repository.read(IDENTIFIER) as handle:
        raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))
        raster_repository.publish(IDENTIFIER)

        assert handle.dataset.sizes["t"] == 3
        assert numpy.allclose(handle.dataset[VARIABLE].values, original[VARIABLE].values)


def test_a_published_read_without_a_published_branch_is_refused(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))

    with pytest.raises(SnapshotNotFoundError, match="no published version"), raster_repository.read(IDENTIFIER):
        pass


def test_a_failed_record_write_does_not_expose_the_next_draft(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    catalog: ObjectCatalog,
    monkeypatch: pytest.MonkeyPatch,
):
    created = raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    written = catalog.put
    failing = {"active": True}

    def put(dataset: Dataset, **keywords: Any) -> None:
        if failing["active"]:
            raise RuntimeError("the catalog write failed after the branch had moved")
        written(dataset, **keywords)

    monkeypatch.setattr(catalog, "put", put)
    with pytest.raises(RuntimeError):
        raster_repository.publish(IDENTIFIER)
    failing["active"] = False
    stale = catalog.require(IDENTIFIER)
    assert isinstance(stale, CoverageDataset)
    assert stale.publication.published is False

    raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))

    with raster_repository.read(IDENTIFIER, version=VersionSelector.PUBLISHED) as handle:
        assert handle.snapshot_identifier == created.snapshot_identifier
        assert handle.dataset.sizes["t"] == 3
    published = [version for version in raster_repository.versions(IDENTIFIER) if version.is_published]
    assert [version.snapshot_identifier for version in published] == [created.snapshot_identifier]
    reconciled = catalog.require(IDENTIFIER)
    assert isinstance(reconciled, CoverageDataset)
    assert reconciled.publication.published is True
    assert reconciled.publication.snapshot_identifier == created.snapshot_identifier


def test_publishing_the_initialisation_snapshot_is_refused(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    raster_repository.publish(IDENTIFIER)
    initialisation = raster_repository.versions(IDENTIFIER)[-1]

    with pytest.raises(NothingToPublishError, match=initialisation.snapshot_identifier):
        raster_repository.publish(IDENTIFIER, snapshot_identifier=initialisation.snapshot_identifier)

    assert raster_repository.query(IDENTIFIER).timestep_count == 3


def test_two_raster_creates_racing_one_record_lose_the_conditional_create(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    racing = RasterRepository(storage_backend, catalog, settings)
    raster_repository.create(IDENTIFIER, grid, build_cube(grid))
    # Freeze the empty catalog the racing writer saw before the first create landed.
    monkeypatch.setattr(racing, "_write_target", lambda dataset_identifier, *, overwrite: CoverageWriteTarget())

    winning = build_cube(grid)

    with pytest.raises(DatasetAlreadyExistsError):
        racing.create(IDENTIFIER, grid, build_cube(grid, seed=1))

    record = catalog.require(IDENTIFIER)
    assert isinstance(record, CoverageDataset)
    assert record.title == IDENTIFIER
    # The losing writer staged its cube on a scratch branch, so the draft still holds the winner's.
    with raster_repository.read(IDENTIFIER, version=VersionSelector.DRAFT) as handle:
        assert numpy.allclose(handle.dataset[VARIABLE].values, winning[VARIABLE].values)


def test_a_stale_raster_record_loses_the_compare_and_swap(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    racing = RasterRepository(storage_backend, catalog, settings)
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    read = catalog.require_entry(IDENTIFIER)
    assert isinstance(read.record, CoverageDataset)
    stale = CoverageEntry(record=read.record, revision=read.revision)

    racing.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))

    # Freeze the record the losing writer read before the racing writer moved it on.
    monkeypatch.setattr(raster_repository, "_require_coverage_entry", lambda dataset_identifier: stale)
    with pytest.raises(PublicationConflictError):
        raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 6), seed=2))

    record = catalog.require(IDENTIFIER)
    assert isinstance(record, CoverageDataset)
    assert record.timestep_count == 5
    with raster_repository.read(IDENTIFIER, version=VersionSelector.DRAFT) as handle:
        assert handle.dataset.sizes["t"] == 5


def test_a_commit_racing_the_draft_branch_is_reported_as_a_conflict(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    storage_backend: StorageBackend,
    monkeypatch: pytest.MonkeyPatch,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    address = raster_repository.repository_address(IDENTIFIER)
    written = repository_module.to_icechunk
    raced = {"done": False}

    def to_icechunk_after_a_racing_commit(dataset: xarray.Dataset, session: Any, **keywords: Any) -> None:
        if not raced["done"]:
            raced["done"] = True
            racing = icechunk.Repository.open(storage_backend.icechunk_storage(address))
            racing.writable_session("main").commit("a racing writer moved the draft branch", allow_empty=True)
        written(dataset, session, **keywords)

    monkeypatch.setattr(repository_module, "to_icechunk", to_icechunk_after_a_racing_commit)

    with pytest.raises(PublicationConflictError):
        raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))


def test_reconcile_rebuilds_the_extents_a_record_disagrees_with(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    catalog: ObjectCatalog,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    entry = catalog.require_entry(IDENTIFIER)
    assert isinstance(entry.record, CoverageDataset)
    catalog.put(
        entry.record.model_copy(
            update={
                "timestep_count": 99,
                "variables": ("humidity",),
                "temporal": TemporalExtent(start=datetime(1999, 1, 1), end=datetime(1999, 1, 2)),
                "title": "A title no write ever committed",
            },
        ),
        revision=entry.revision,
    )

    reconciled = raster_repository.reconcile_publication(IDENTIFIER)

    assert reconciled.timestep_count == 3
    assert reconciled.variables == (VARIABLE,)
    assert reconciled.temporal is not None
    assert reconciled.temporal.start == START
    assert reconciled.temporal.end == datetime(2020, 1, 3)
    # The title travels with the commit, so it is read back from the store like everything else.
    assert reconciled.title == IDENTIFIER
    stored = catalog.require(IDENTIFIER)
    assert isinstance(stored, CoverageDataset)
    assert stored.timestep_count == 3
    assert stored.variables == (VARIABLE,)
    assert stored.title == IDENTIFIER


def build_wider_grid() -> GridSpecification:
    """Build a grid of a different shape from build_grid, so a record describing it is visibly stale."""
    return GridSpecification(
        shape=(6, 9),
        bbox=BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=18.0, maximum_y=12.0),
        crs="EPSG:4326",
    )


def move_the_draft_branch_under_the_next_write(
    raster_repository: RasterRepository,
    storage_backend: StorageBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make the next staged write find the draft branch moved on, so its commit loses the compare-and-swap."""
    address = raster_repository.repository_address(IDENTIFIER)
    written = repository_module.to_icechunk
    raced = {"done": False}

    def to_icechunk_after_a_racing_commit(dataset: xarray.Dataset, session: Any, **keywords: Any) -> None:
        if not raced["done"]:
            raced["done"] = True
            racing = icechunk.Repository.open(storage_backend.icechunk_storage(address))
            tip = racing.lookup_branch(MAIN_BRANCH)
            # A racing writer commits through the engine, so its commit carries the title and the terms
            # of the snapshot it extends rather than none at all.
            carried = next(iter(racing.ancestry(snapshot_id=tip))).metadata
            racing.writable_session(MAIN_BRANCH).commit(
                "a racing writer moved the draft branch",
                carried,
                allow_empty=True,
            )
        written(dataset, session, **keywords)

    monkeypatch.setattr(repository_module, "to_icechunk", to_icechunk_after_a_racing_commit)


def test_a_rejected_overwrite_puts_the_record_it_replaced_back(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    monkeypatch: pytest.MonkeyPatch,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    wider = build_wider_grid()
    move_the_draft_branch_under_the_next_write(raster_repository, storage_backend, monkeypatch)

    with pytest.raises(PublicationConflictError):
        raster_repository.create(IDENTIFIER, wider, build_cube(wider, count=1), overwrite=True, title="Rejected")

    record = catalog.require(IDENTIFIER)
    assert isinstance(record, CoverageDataset)
    assert record.grid.shape == grid.shape
    assert record.bbox == grid.bbox
    assert record.timestep_count == 3
    assert record.title == IDENTIFIER


def test_reconcile_restores_the_grid_a_rejected_overwrite_left_behind(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
    monkeypatch: pytest.MonkeyPatch,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    wider = build_wider_grid()
    move_the_draft_branch_under_the_next_write(raster_repository, storage_backend, monkeypatch)
    # A crash between the catalog claim and the record being put back is exactly what reconciliation is
    # for, so the write is stopped after the claim and before it takes the replaced record back.
    monkeypatch.setattr(raster_repository, "_restore_replaced_record", lambda rejected, replaced: None)

    with pytest.raises(PublicationConflictError):
        raster_repository.create(IDENTIFIER, wider, build_cube(wider, count=1), overwrite=True, title="Rejected")

    stale = catalog.require(IDENTIFIER)
    assert isinstance(stale, CoverageDataset)
    assert stale.grid.shape == wider.shape
    assert stale.title == "Rejected"

    reconciled = raster_repository.reconcile_publication(IDENTIFIER)

    assert reconciled.grid.shape == grid.shape
    assert reconciled.grid.bbox == grid.bbox
    assert reconciled.bbox == grid.bbox
    assert reconciled.timestep_count == 3
    assert reconciled.variables == (VARIABLE,)
    # The title travels with the commit too, so the one the rejected overwrite wrote is put back.
    assert reconciled.title == IDENTIFIER
    # The grid an append is checked against is the record's, so a repaired one lets the coverage grow.
    raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))
    summary = raster_repository.query(IDENTIFIER, version=VersionSelector.DRAFT)
    assert summary.timestep_count == 5
    assert summary.cell_count == 5 * 4 * 6
    assert summary.bbox.as_tuple() == grid.bbox.as_tuple()


def test_reconcile_rewrites_nothing_when_the_record_already_describes_the_store(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    catalog: ObjectCatalog,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    written = catalog.require_entry(IDENTIFIER)

    raster_repository.reconcile_publication(IDENTIFIER)

    # The grid is measured back from coordinates written as floats, so a reconciliation comparing it
    # exactly would find the record stale every time and rewrite it on every read of the versions.
    assert catalog.require_entry(IDENTIFIER).revision == written.revision


def test_reconcile_leaves_a_record_alone_when_the_store_holds_no_data_yet(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    storage_backend: StorageBackend,
    catalog: ObjectCatalog,
):
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3))
    # Everything below the prefix is gone, so the next open finds only an initialisation snapshot.
    storage_backend.delete_prefix(raster_repository.repository_address(IDENTIFIER))

    reconciled = raster_repository.reconcile_publication(IDENTIFIER)

    assert reconciled.timestep_count == 3
    assert reconciled.variables == (VARIABLE,)
    assert catalog.get(IDENTIFIER) is not None


def test_the_commit_metadata_carries_the_licence_of_the_snapshot_it_was_written_with(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    created = raster_repository.create(
        IDENTIFIER,
        grid,
        build_cube(grid, count=3),
        license="CC-BY-4.0",
        attribution="Open Climate Service",
    )
    raster_repository.publish(IDENTIFIER, snapshot_identifier=created.snapshot_identifier)

    raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))

    # The terms live on the commit, and an append that declares none carries forward the ones it extends.
    published = raster_repository.describe(IDENTIFIER, version=VersionSelector.PUBLISHED)
    draft = raster_repository.describe(IDENTIFIER, version=VersionSelector.DRAFT)
    assert published.license == "CC-BY-4.0"
    assert published.attribution == "Open Climate Service"
    assert draft.license == "CC-BY-4.0"
    assert draft.attribution == "Open Climate Service"


def test_a_draft_written_under_other_terms_leaves_the_published_snapshot_alone(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    created = raster_repository.create(
        IDENTIFIER,
        grid,
        build_cube(grid),
        license="CC-BY-4.0",
        attribution="Open Climate Service",
    )
    raster_repository.publish(IDENTIFIER, snapshot_identifier=created.snapshot_identifier)

    raster_repository.create(
        IDENTIFIER,
        grid,
        build_cube(grid, seed=1),
        overwrite=True,
        license="proprietary",
        attribution="Statistics Norway",
    )

    # The record tracks the newest write; each snapshot keeps the terms its own bytes were written under.
    assert raster_repository.describe(IDENTIFIER, version=VersionSelector.PUBLISHED).license == "CC-BY-4.0"
    assert raster_repository.describe(IDENTIFIER, version=VersionSelector.DRAFT).license == "proprietary"


def test_reconcile_leaves_the_title_alone_when_the_snapshot_carries_none(
    raster_repository: RasterRepository,
    grid: GridSpecification,
    catalog: ObjectCatalog,
    monkeypatch: pytest.MonkeyPatch,
):
    # A snapshot committed before a write carried its title: the commit metadata holds only the terms.
    written = repository_module._commit_metadata

    def metadata_without_a_title(title: str | None, license: str | None, attribution: str | None) -> dict[str, Any]:
        return written(None, license, attribution)

    monkeypatch.setattr(repository_module, "_commit_metadata", metadata_without_a_title)
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3), title="Written before titles travelled")

    reconciled = raster_repository.reconcile_publication(IDENTIFIER)

    # Nothing can be read back, so the record keeps the title it has rather than being blanked.
    assert raster_repository.describe(IDENTIFIER, version=VersionSelector.DRAFT).title is None
    assert reconciled.title == "Written before titles travelled"
    stored = catalog.require(IDENTIFIER)
    assert isinstance(stored, CoverageDataset)
    assert stored.title == "Written before titles travelled"


def test_the_commit_metadata_carries_the_title_of_the_snapshot_it_was_written_with(
    raster_repository: RasterRepository,
    grid: GridSpecification,
):
    created = raster_repository.create(IDENTIFIER, grid, build_cube(grid, count=3), title="Daily temperature")
    raster_repository.publish(IDENTIFIER, snapshot_identifier=created.snapshot_identifier)

    raster_repository.append(IDENTIFIER, build_cube(grid, count=2, start=datetime(2020, 1, 4), seed=1))
    raster_repository.create(IDENTIFIER, grid, build_cube(grid, seed=2), overwrite=True, title="Renamed")

    # The title lives on the commit like the terms do: an append carries forward the one it extends,
    # and an overwrite leaves the published snapshot advertising the title it was written under.
    assert raster_repository.describe(IDENTIFIER, version=VersionSelector.PUBLISHED).title == "Daily temperature"
    assert raster_repository.describe(IDENTIFIER, version=VersionSelector.DRAFT).title == "Renamed"
