"""Raster engine creating, reading, querying and publishing Icechunk-backed GeoZarr coverages."""

from __future__ import annotations

import math
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

import icechunk
import numpy
import xarray
from icechunk.xarray import to_icechunk
from numpy.typing import NDArray
from pyproj import CRS
from zarr.errors import GroupNotFoundError

from ocs_storage_exploration.storage.addresses import StorageAddress
from ocs_storage_exploration.storage.errors import (
    DatasetAlreadyExistsError,
    DatasetNotFoundError,
    ItemTypeMismatchError,
    NothingToPublishError,
    PublicationConflictError,
    QuerySizeGuardError,
    RasterContractError,
    SnapshotNotFoundError,
)
from ocs_storage_exploration.storage.failures import backend_transport_failures
from ocs_storage_exploration.storage.keys import raster_prefix, validate_dataset_identifier
from ocs_storage_exploration.storage.protocols import Catalog, StorageBackend
from ocs_storage_exploration.storage.raster.grid import (
    CRS_WELL_KNOWN_TEXT_ATTRIBUTE,
    LONGITUDE_SPAN,
    MAXIMUM_LONGITUDE,
    NODATA_ATTRIBUTE,
    PROJECTION_CODE_ATTRIBUTE,
    SPATIAL_BBOX_ATTRIBUTE,
    SPATIAL_REFERENCE_NAME,
    apply_geozarr_attributes,
    assert_finite_attributes,
    build_cell_sizes,
    build_coordinates,
    projection_code,
    to_naive_utc,
    wrap_longitudes,
)
from ocs_storage_exploration.storage.schemas import (
    BoundingBox,
    CoverageDataset,
    DatasetLifecycle,
    GridSpecification,
    ItemType,
    Publication,
    PublicationResult,
    RasterQuerySummary,
    RasterVersion,
    RasterWriteResult,
    TemporalExtent,
    current_timestamp,
)

if TYPE_CHECKING:
    from ocs_storage_exploration.settings import Settings

MAIN_BRANCH: Final[str] = "main"
PUBLISHED_BRANCH: Final[str] = "published"
SCRATCH_BRANCH_PREFIX: Final[str] = "write-"
FALLBACK_GROUP: Final[str] = "0"
DEFAULT_VERSION_LIMIT: Final[int] = 100
DEFAULT_CREATE_MESSAGE: Final[str] = "initial write"
DEFAULT_APPEND_MESSAGE: Final[str] = "append"
CUBE_DIMENSION_COUNT: Final[int] = 3
BBOX_VALUE_COUNT: Final[int] = 4
LICENSE_METADATA_KEY: Final[str] = "license"
ATTRIBUTION_METADATA_KEY: Final[str] = "attribution"

FloatArray = NDArray[numpy.float64]
IndexArray = NDArray[numpy.intp]


@contextmanager
def _publication_conflicts(message: str) -> Generator[None]:
    """Report an Icechunk branch compare-and-swap that lost as the conflict the API answers with 409."""
    try:
        yield
    except icechunk.ConflictError as error:
        raise PublicationConflictError(message) from error


class VersionSelector(StrEnum):
    """Pointer a raster read follows when no explicit snapshot is requested."""

    PUBLISHED = "published"
    DRAFT = "draft"


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    """The catalog record of a coverage together with the revision it was read at."""

    record: CoverageDataset
    revision: str


@dataclass(frozen=True, slots=True)
class RasterReadHandle:
    """Open dataset of one raster snapshot together with the snapshot and group it was read from."""

    dataset: xarray.Dataset
    snapshot_identifier: str
    group: str | None = None


@dataclass(frozen=True, slots=True)
class RasterStoreDescription:
    """Grid, variables and extents of one coverage snapshot as the opened store reports them."""

    snapshot_identifier: str
    variables: tuple[str, ...]
    crs: str
    bbox: BoundingBox
    temporal_start: datetime | None
    temporal_end: datetime | None
    timestep_count: int
    time_dimension: str
    y_dimension: str
    x_dimension: str
    shape: tuple[int, int]
    # The terms the bytes of this snapshot were written under, taken from the commit that wrote them
    # rather than from the record, which describes the newest write and not the snapshot being read.
    license: str | None = None
    attribution: str | None = None


@dataclass(frozen=True, slots=True)
class _StoreGrid:
    """Dimension names, projection and cell sizes read back from a store rather than from a record."""

    time_dimension: str
    y_dimension: str
    x_dimension: str
    crs: str
    geographic: bool
    y_cell_size: float
    x_cell_size: float


class RasterRepository:
    """Stores one coverage per Icechunk repository and publishes it by moving a branch."""

    def __init__(self, backend: StorageBackend, catalog: Catalog, settings: Settings) -> None:
        """Bind the engine to one backend, one catalog and the service settings."""
        self._backend = backend
        self._catalog = catalog
        self._settings = settings

    @property
    def backend(self) -> StorageBackend:
        """Backend holding the Icechunk repositories."""
        return self._backend

    @property
    def catalog(self) -> Catalog:
        """Catalog holding the record that makes a coverage exist."""
        return self._catalog

    def repository_address(self, dataset_identifier: str) -> StorageAddress:
        """Return the address of the Icechunk repository of one coverage."""
        return self._backend.address(raster_prefix(dataset_identifier))

    def create(
        self,
        dataset_identifier: str,
        grid: GridSpecification,
        dataset: xarray.Dataset,
        *,
        title: str | None = None,
        license: str | None = None,
        attribution: str | None = None,
        overwrite: bool = False,
        message: str = DEFAULT_CREATE_MESSAGE,
    ) -> RasterWriteResult:
        """Write a coverage onto the main branch and record it in the catalog."""
        identifier = validate_dataset_identifier(dataset_identifier)
        self._assert_cube_size(identifier, grid.shape, _incoming_timestep_count(dataset, grid.time_dimension))
        existing = self._existing_entry(identifier, overwrite=overwrite)
        prepared = self._prepare(grid, dataset)
        repository = self._open_repository(identifier)
        record = self._build_record(
            identifier,
            grid,
            prepared,
            title=title,
            license=license,
            attribution=attribution,
            existing=None if existing is None else existing.record,
        )
        snapshot_identifier = self._staged_commit(
            repository,
            identifier,
            previous=repository.lookup_branch(MAIN_BRANCH),
            message=message,
            apply=lambda session: to_icechunk(prepared, session, mode="w"),
            record=record,
            # A first write claims the record, an overwrite replaces the one it read: either way a
            # concurrent writer that got there first is refused rather than overwritten.
            revision=None if existing is None else existing.revision,
            # The terms travel with the commit, so a published snapshot keeps advertising the ones it
            # was written under even after a draft replaces the record with other ones.
            metadata=_commit_metadata(record.license, record.attribution),
        )
        return self._write_result(record, snapshot_identifier)

    def append(
        self,
        dataset_identifier: str,
        dataset: xarray.Dataset,
        *,
        message: str = DEFAULT_APPEND_MESSAGE,
    ) -> RasterWriteResult:
        """Append timesteps to a coverage after checking that it matches what is already committed."""
        identifier = validate_dataset_identifier(dataset_identifier)
        entry = self._require_coverage_entry(identifier)
        record = entry.record
        grid = record.grid
        self._assert_cube_size(identifier, grid.shape, _incoming_timestep_count(dataset, grid.time_dimension))
        prepared = self._prepare(grid, dataset)
        repository = self._open_repository(identifier)
        # The snapshot the append is checked against is the snapshot it is written onto, so a
        # concurrent write cannot slip between the check and the commit.
        previous = repository.lookup_branch(MAIN_BRANCH)
        self._assert_appendable(repository, record, prepared, base=previous)
        updated = self._extend_record(record, prepared)
        snapshot_identifier = self._staged_commit(
            repository,
            identifier,
            previous=previous,
            message=message,
            apply=lambda session: to_icechunk(prepared, session, append_dim=grid.time_dimension),
            record=updated,
            revision=entry.revision,
            # An append declares no terms of its own, so it extends the snapshot it grows under the
            # terms that snapshot already carries.
            metadata=self._snapshot_metadata(repository, previous),
        )
        return self._write_result(updated, snapshot_identifier)

    def _staged_commit(
        self,
        repository: icechunk.Repository,
        dataset_identifier: str,
        *,
        previous: str,
        message: str,
        apply: Callable[[icechunk.Session], None],
        record: CoverageDataset,
        revision: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Commit a write on a scratch branch, claim the record, then fast-forward the draft branch onto it."""
        branch = f"{SCRATCH_BRANCH_PREFIX}{uuid4().hex}"
        repository.create_branch(branch, previous)
        try:
            session = repository.writable_session(branch)
            apply(session)
            snapshot_identifier = session.commit(message, metadata)
            # The catalog claim is the arbiter of the race, and it runs before anything a reader can
            # see moves: a writer that loses it leaves the draft branch exactly where it found it.
            if revision is None:
                self._catalog.put(record, create=True)
            else:
                self._catalog.put(record, revision=revision)
            with _publication_conflicts(
                f"draft branch of {dataset_identifier!r} moved while the write was being staged",
            ):
                repository.reset_branch(MAIN_BRANCH, snapshot_identifier, from_snapshot_id=previous)
        finally:
            repository.delete_branch(branch)
        return snapshot_identifier

    @contextmanager
    def read(
        self,
        dataset_identifier: str,
        *,
        version: VersionSelector = VersionSelector.PUBLISHED,
        snapshot_identifier: str | None = None,
    ) -> Generator[RasterReadHandle]:
        """Open a readonly session on the requested snapshot and yield the dataset it holds."""
        identifier = validate_dataset_identifier(dataset_identifier)
        record = self._require_coverage(identifier)
        repository = self._open_repository(identifier)
        with self._read_opened(repository, record, version, snapshot_identifier) as handle:
            yield handle

    @contextmanager
    def _read_opened(
        self,
        repository: icechunk.Repository,
        record: CoverageDataset,
        version: VersionSelector,
        snapshot_identifier: str | None,
    ) -> Generator[RasterReadHandle]:
        """Open a readonly session of an already opened repository and yield the dataset it holds."""
        session = self._readonly_session(repository, record, version, snapshot_identifier)
        dataset, group = self._open_dataset(session)
        try:
            yield RasterReadHandle(dataset=dataset, snapshot_identifier=session.snapshot_id, group=group)
        finally:
            dataset.close()

    def describe(
        self,
        dataset_identifier: str,
        *,
        version: VersionSelector = VersionSelector.PUBLISHED,
        snapshot_identifier: str | None = None,
    ) -> RasterStoreDescription:
        """Describe one coverage snapshot from the store it was written to rather than from its record."""
        identifier = validate_dataset_identifier(dataset_identifier)
        record = self._require_coverage(identifier)
        repository = self._open_repository(identifier)
        with self._read_opened(repository, record, version, snapshot_identifier) as handle:
            metadata = self._snapshot_metadata(repository, handle.snapshot_identifier)
            return self._describe_handle(handle, record, metadata)

    def query(
        self,
        dataset_identifier: str,
        *,
        variable: str | None = None,
        bbox: BoundingBox | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        version: VersionSelector = VersionSelector.PUBLISHED,
        snapshot_identifier: str | None = None,
    ) -> RasterQuerySummary:
        """Summarise one variable over a spatial and temporal window, guarding the window size."""
        identifier = validate_dataset_identifier(dataset_identifier)
        record = self._require_coverage(identifier)
        with self.read(identifier, version=version, snapshot_identifier=snapshot_identifier) as handle:
            # Every grid fact comes from the snapshot that was opened: the record describes the newest
            # write, which is not the snapshot a published read follows.
            store_grid = self._store_grid(handle.dataset, record)
            name = self._select_variable(handle.dataset, variable)
            array = handle.dataset[name]
            window = self._apply_spatial_window(
                self._apply_time_window(array, store_grid, start, end), store_grid, bbox
            )
            cell_count = int(window.size)
            if cell_count == 0:
                raise QuerySizeGuardError(f"query window of {identifier!r} selects no cells")
            if cell_count > self._settings.max_query_cell_count:
                raise QuerySizeGuardError(
                    f"query window of {identifier!r} reads {cell_count} cells, "
                    f"more than the {self._settings.max_query_cell_count} allowed",
                )
            minimum, maximum, mean = self._summarise(window.values, self._nodata_value(array))
            return RasterQuerySummary(
                dataset_identifier=identifier,
                variable=name,
                bbox=self._envelope(store_grid, window),
                crs=store_grid.crs,
                snapshot_identifier=handle.snapshot_identifier,
                timestep_count=int(window.sizes.get(store_grid.time_dimension, 1)),
                cell_count=cell_count,
                minimum=minimum,
                maximum=maximum,
                mean=mean,
            )

    def publish(self, dataset_identifier: str, *, snapshot_identifier: str | None = None) -> PublicationResult:
        """Move the published branch onto a snapshot of the main branch, creating it on first publication."""
        identifier = validate_dataset_identifier(dataset_identifier)
        entry = self._reconcile_entry(identifier)
        record = entry.record
        repository = self._open_repository(identifier)
        ancestry = {information.id for information in repository.ancestry(branch=MAIN_BRANCH)}
        target = snapshot_identifier if snapshot_identifier is not None else repository.lookup_branch(MAIN_BRANCH)
        if target not in ancestry:
            raise SnapshotNotFoundError(f"snapshot {target!r} is not in the history of {identifier!r}")
        self._assert_publishable(repository, identifier, target)
        previous = self._published_snapshot(repository)
        changed = previous != target
        if previous is None:
            repository.create_branch(PUBLISHED_BRANCH, target)
        elif changed:
            self._reset_published_branch(repository, identifier, target, previous)
        if changed:
            self._catalog.put(self._publish_record(record, target, previous), revision=entry.revision)
        return PublicationResult(
            dataset_identifier=identifier,
            item_type=ItemType.COVERAGE,
            published=True,
            changed=changed,
            snapshot_identifier=target,
            previous_snapshot_identifier=previous,
        )

    def reconcile_publication(self, dataset_identifier: str) -> CoverageDataset:
        """Rewrite the publication block and the extents of a record from the store, which is the only truth."""
        return self._reconcile_entry(dataset_identifier).record

    def _reconcile_entry(self, dataset_identifier: str) -> CoverageEntry:
        """Reconcile a record against the store and return the entry the caller may write against."""
        identifier = validate_dataset_identifier(dataset_identifier)
        entry = self._require_coverage_entry(identifier)
        record = entry.record
        published = self._published_snapshot(self._open_repository(identifier))
        update: dict[str, Any] = {}
        publication = self._reconciled_publication(record.publication, published)
        if publication is not None:
            update["publication"] = publication
        update.update(self._reconciled_extents(record))
        if not update:
            return entry
        updated = record.model_copy(update={**update, "updated_at": current_timestamp()})
        self._catalog.put(updated, revision=entry.revision)
        # The write moved the record on, so the revision the caller writes against is the new one.
        return self._require_coverage_entry(identifier)

    def _reconciled_publication(self, publication: Publication, published: str | None) -> Publication | None:
        """Return the publication block the published branch implies, or None when the record already matches."""
        if published is None:
            if not publication.published and publication.snapshot_identifier is None:
                return None
            return Publication(previous_snapshot_identifier=publication.snapshot_identifier)
        if publication.published and publication.snapshot_identifier == published:
            return None
        return Publication(
            published=True,
            published_at=current_timestamp(),
            snapshot_identifier=published,
            previous_snapshot_identifier=publication.snapshot_identifier,
        )

    def _reconciled_extents(self, record: CoverageDataset) -> dict[str, Any]:
        """Return the extent fields the draft branch disagrees with, which a rejected write can leave stale."""
        description = self._draft_description(record.dataset_identifier)
        if description is None:
            return {}
        temporal = (
            TemporalExtent(start=description.temporal_start, end=description.temporal_end)
            if description.temporal_start is not None and description.temporal_end is not None
            else None
        )
        facts: dict[str, Any] = {
            "timestep_count": description.timestep_count,
            "variables": description.variables,
            "temporal": temporal,
        }
        return {name: value for name, value in facts.items() if getattr(record, name) != value}

    def _draft_description(self, dataset_identifier: str) -> RasterStoreDescription | None:
        """Describe the draft branch of a coverage, or None when it holds nothing to describe yet."""
        try:
            return self.describe(dataset_identifier, version=VersionSelector.DRAFT)
        except (RasterContractError, SnapshotNotFoundError, FileNotFoundError, KeyError):
            # A repository that only holds its initialisation snapshot has no extents to read back.
            return None

    def versions(self, dataset_identifier: str, *, limit: int = DEFAULT_VERSION_LIMIT) -> list[RasterVersion]:
        """List the snapshots of the main branch newest first, marking the published one."""
        identifier = validate_dataset_identifier(dataset_identifier)
        self.reconcile_publication(identifier)
        repository = self._open_repository(identifier)
        published = self._published_snapshot(repository)
        versions: list[RasterVersion] = []
        for information in repository.ancestry(branch=MAIN_BRANCH):
            if len(versions) >= limit:
                break
            versions.append(
                RasterVersion(
                    snapshot_identifier=information.id,
                    message=information.message,
                    written_at=information.written_at,
                    is_published=information.id == published,
                ),
            )
        return versions

    def root_attributes(
        self,
        dataset_identifier: str,
        *,
        version: VersionSelector = VersionSelector.PUBLISHED,
    ) -> dict[str, Any]:
        """Return the root attributes of a coverage as they were written."""
        with self.read(dataset_identifier, version=version) as handle:
            return {str(key): value for key, value in handle.dataset.attrs.items()}

    def delete(self, dataset_identifier: str) -> None:
        """Mark the record of a coverage as deleting, sweep every object of its repository, then drop it.

        The record goes last rather than first: while it is there and marked, a concurrent writer is
        told a deletion is running instead of finding no record at all and writing into a prefix that
        is about to be emptied. A deletion that stops half way leaves the marked record behind, and
        deleting again, or creating the coverage again, finishes it.
        """
        identifier = validate_dataset_identifier(dataset_identifier)
        # Read raw: a record already marked as deleting is exactly what this call is here to finish.
        entry = self._coverage_entry(identifier)
        reserved = self._reserve_deletion(entry)
        self._sweep(identifier)
        self._drop_reserved_record(identifier, reserved)

    def _open_repository(self, dataset_identifier: str) -> icechunk.Repository:
        """Open the Icechunk repository of a coverage, creating it when it does not exist."""
        address = self.repository_address(dataset_identifier)
        with backend_transport_failures(f"opening the repository of {dataset_identifier!r}"):
            return icechunk.Repository.open_or_create(
                self._backend.icechunk_storage(address),
                config=self._backend.repository_config(),
            )

    def _existing_entry(self, dataset_identifier: str, *, overwrite: bool) -> CoverageEntry | None:
        """Return the entry being overwritten, refusing an existing dataset unless overwrite is set."""
        entry = self._catalog.get_entry(dataset_identifier)
        if entry is None:
            return None
        if not isinstance(entry.record, CoverageDataset):
            raise ItemTypeMismatchError(f"dataset {dataset_identifier!r} is not a coverage")
        existing = CoverageEntry(record=entry.record, revision=entry.revision)
        if existing.record.is_deleting:
            # The prefix this write is about to fill is being emptied. Finishing that deletion first is
            # the only way to write into a prefix nobody else is sweeping; the write then creates the
            # record conditionally, so a second writer doing the same thing loses at the create.
            self._complete_deletion(existing)
            return None
        if not overwrite:
            raise DatasetAlreadyExistsError(f"dataset {dataset_identifier!r} already exists")
        return existing

    def _coverage_entry(self, dataset_identifier: str) -> CoverageEntry:
        """Read the coverage entry of a dataset, including one whose deletion is under way."""
        entry = self._catalog.require_entry(dataset_identifier)
        if not isinstance(entry.record, CoverageDataset):
            raise ItemTypeMismatchError(f"dataset {dataset_identifier!r} is not a coverage")
        return CoverageEntry(record=entry.record, revision=entry.revision)

    def _require_coverage_entry(self, dataset_identifier: str) -> CoverageEntry:
        """Read the entry of a live coverage, refusing another item type or a deletion under way."""
        entry = self._coverage_entry(dataset_identifier)
        if entry.record.is_deleting:
            raise DatasetNotFoundError(f"coverage {dataset_identifier!r} is being deleted")
        return entry

    def _sweep(self, dataset_identifier: str) -> None:
        """Delete every object below the prefix holding the Icechunk repository of a coverage."""
        self._backend.delete_prefix(self.repository_address(dataset_identifier))

    def _reserve_deletion(self, entry: CoverageEntry) -> str | None:
        """Mark a record as deleting under compare-and-swap, or report that another deleter got there first."""
        identifier = entry.record.dataset_identifier
        if entry.record.is_deleting:
            return entry.revision
        marked = entry.record.model_copy(
            update={"lifecycle": DatasetLifecycle.DELETING, "updated_at": current_timestamp()},
        )
        try:
            self._catalog.put(marked, revision=entry.revision)
        except PublicationConflictError:
            # Another deleter marked it first, or a writer changed it. Sweeping is still correct and
            # idempotent, but dropping a record this call never reserved is not, so it is left alone.
            return None
        reserved = self._catalog.get_entry(identifier)
        return None if reserved is None else reserved.revision

    def _drop_reserved_record(self, dataset_identifier: str, reserved: str | None) -> None:
        """Delete the record of a swept coverage, leaving alone one that was reclaimed in the meantime."""
        if reserved is None:
            return
        entry = self._catalog.get_entry(dataset_identifier)
        # Object stores offer no conditional delete, so this is a read followed by a delete rather than
        # one compare-and-swap. It refuses the outcome that matters: erasing the record of a coverage
        # that was written again under the same identifier while this deletion was sweeping.
        if entry is None or entry.revision != reserved:
            return
        self._catalog.delete(dataset_identifier)

    def _complete_deletion(self, entry: CoverageEntry) -> None:
        """Finish a deletion that stopped half way, so the caller can start from an empty prefix."""
        identifier = entry.record.dataset_identifier
        self._sweep(identifier)
        self._drop_reserved_record(identifier, entry.revision)

    def _require_coverage(self, dataset_identifier: str) -> CoverageDataset:
        """Read the coverage record of a dataset or raise."""
        return self._require_coverage_entry(dataset_identifier).record

    def _assert_cube_size(self, dataset_identifier: str, shape: tuple[int, int], timestep_count: int) -> None:
        """Refuse a cube larger than the configured guard, before anything allocates it."""
        rows, columns = shape
        cell_count = rows * columns * max(timestep_count, 1)
        allowed = self._settings.max_cube_cells
        if cell_count > allowed:
            raise QuerySizeGuardError(
                f"cube of {dataset_identifier!r} holds {cell_count} cells, more than the {allowed} allowed",
            )

    def _prepare(self, grid: GridSpecification, dataset: xarray.Dataset) -> xarray.Dataset:
        """Validate a cube against its grid and attach the GeoZarr attributes it is written with."""
        if not dataset.data_vars:
            raise RasterContractError("dataset has no data variables")
        dimensions = (grid.time_dimension, grid.y_dimension, grid.x_dimension)
        for name, array in dataset.data_vars.items():
            if tuple(str(dimension) for dimension in array.dims) != dimensions:
                raise RasterContractError(f"variable {str(name)!r} must have dimensions {dimensions}")
        rows, columns = grid.shape
        if dataset.sizes.get(grid.y_dimension) != rows or dataset.sizes.get(grid.x_dimension) != columns:
            raise RasterContractError(f"cube shape does not match the grid shape {(rows, columns)}")
        aligned = self._align_spatial_coordinates(grid, dataset)
        prepared = apply_geozarr_attributes(aligned, grid)
        assert_finite_attributes(prepared)
        return prepared

    def _align_spatial_coordinates(self, grid: GridSpecification, dataset: xarray.Dataset) -> xarray.Dataset:
        """Assign the grid coordinates when they are missing and refuse coordinates that disagree."""
        expected = build_coordinates(grid)
        missing = {name: values for name, values in expected.items() if name not in dataset.coords}
        aligned = dataset.assign_coords(missing) if missing else dataset
        for name, values in expected.items():
            present = _float_values(aligned[name])
            if present.size != values.size or not bool(numpy.allclose(present, values)):
                raise RasterContractError(f"coordinate {name!r} does not match the grid of the dataset")
        return aligned

    def _assert_appendable(
        self,
        repository: icechunk.Repository,
        record: CoverageDataset,
        dataset: xarray.Dataset,
        *,
        base: str,
    ) -> None:
        """Refuse an append whose coordinates, variables or timestamps differ from the ones already committed."""
        committed, _ = self._open_dataset(repository.readonly_session(snapshot_id=base))
        try:
            for name in (record.grid.y_dimension, record.grid.x_dimension):
                if name not in committed.coords:
                    continue
                stored = _float_values(committed[name])
                incoming = _float_values(dataset[name])
                if stored.size != incoming.size or not bool(numpy.allclose(stored, incoming)):
                    raise RasterContractError(
                        f"coordinate {name!r} does not match the coordinate committed for "
                        f"{record.dataset_identifier!r}",
                    )
            self._assert_committed_variables(record.dataset_identifier, committed, dataset)
            self._assert_time_axis_extends(record, committed, dataset)
        finally:
            committed.close()

    def _assert_time_axis_extends(
        self,
        record: CoverageDataset,
        committed: xarray.Dataset,
        dataset: xarray.Dataset,
    ) -> None:
        """Refuse an append that repeats or precedes committed timestamps, which leaves the time axis unusable."""
        time_dimension = record.grid.time_dimension
        incoming = self._timestamps(dataset, time_dimension)
        if not incoming:
            return
        if any(later <= earlier for earlier, later in zip(incoming, incoming[1:], strict=False)):
            raise RasterContractError(
                f"append of {record.dataset_identifier!r} carries timestamps that do not strictly increase",
            )
        stored = self._timestamps(committed, time_dimension)
        if not stored:
            return
        # A label window over an unsorted axis reads the wrong cells or none at all, so an axis that
        # only ever grows forwards is part of the contract rather than something a query works around.
        newest = max(stored)
        if incoming[0] <= newest:
            raise RasterContractError(
                f"append of {record.dataset_identifier!r} starts at {incoming[0].isoformat()} and does not "
                f"extend the committed time axis, which already reaches {newest.isoformat()}",
            )

    def _assert_committed_variables(
        self,
        dataset_identifier: str,
        committed: xarray.Dataset,
        dataset: xarray.Dataset,
    ) -> None:
        """Refuse an append that drops, adds or redefines a variable, which would leave the store unreadable."""
        stored = {str(name): committed[name] for name in committed.data_vars}
        incoming = {str(name): dataset[name] for name in dataset.data_vars}
        missing = sorted(set(stored) - set(incoming))
        if missing:
            raise RasterContractError(f"append is missing the committed variables {missing}")
        # A variable the store does not hold would gain only the appended timesteps, leaving the
        # cube ragged and every later read of the committed variables broken.
        extra = sorted(set(incoming) - set(stored))
        if extra:
            raise RasterContractError(
                f"append of {dataset_identifier!r} adds the variables {extra}, "
                f"which are not committed for this coverage",
            )
        for name, array in incoming.items():
            self._assert_same_layout(dataset_identifier, name, stored[name], array)

    def _assert_same_layout(
        self,
        dataset_identifier: str,
        variable: str,
        committed: xarray.DataArray,
        incoming: xarray.DataArray,
    ) -> None:
        """Refuse an append that changes the data type or the dimension order of a committed variable."""
        if numpy.dtype(incoming.dtype) != numpy.dtype(committed.dtype):
            raise RasterContractError(
                f"variable {variable!r} of {dataset_identifier!r} is written as {incoming.dtype} "
                f"but is committed as {committed.dtype}",
            )
        stored_dimensions = tuple(str(dimension) for dimension in committed.dims)
        incoming_dimensions = tuple(str(dimension) for dimension in incoming.dims)
        if incoming_dimensions != stored_dimensions:
            raise RasterContractError(
                f"variable {variable!r} of {dataset_identifier!r} has dimensions {incoming_dimensions} "
                f"but is committed with {stored_dimensions}",
            )

    def _readonly_session(
        self,
        repository: icechunk.Repository,
        record: CoverageDataset,
        version: VersionSelector,
        snapshot_identifier: str | None,
    ) -> icechunk.Session:
        """Open the readonly session the requested version or snapshot points at."""
        if snapshot_identifier is not None:
            known = {information.id for information in repository.ancestry(branch=MAIN_BRANCH)}
            if snapshot_identifier not in known:
                raise SnapshotNotFoundError(
                    f"snapshot {snapshot_identifier!r} is not in the history of {record.dataset_identifier!r}",
                )
            return repository.readonly_session(snapshot_id=snapshot_identifier)
        if version is VersionSelector.PUBLISHED:
            # The branch is the publication truth. A record that disagrees with it is stale, and
            # falling back to main would serve a draft as if it had been published.
            if PUBLISHED_BRANCH not in repository.list_branches():
                raise SnapshotNotFoundError(f"dataset {record.dataset_identifier!r} has no published version")
            return repository.readonly_session(PUBLISHED_BRANCH)
        return repository.readonly_session(MAIN_BRANCH)

    def _open_dataset(self, session: icechunk.Session) -> tuple[xarray.Dataset, str | None]:
        """Open the Zarr store of a session, falling back to the first multiscale group."""
        dataset = xarray.open_zarr(session.store, consolidated=False, zarr_format=3, decode_coords="all")
        if dataset.data_vars:
            return dataset, None
        try:
            nested = xarray.open_zarr(
                session.store,
                group=FALLBACK_GROUP,
                consolidated=False,
                zarr_format=3,
                decode_coords="all",
            )
        except (GroupNotFoundError, FileNotFoundError, KeyError):
            return dataset, None
        dataset.close()
        return nested, FALLBACK_GROUP

    def _select_variable(self, dataset: xarray.Dataset, variable: str | None) -> str:
        """Return the variable to summarise, defaulting to the only or first one written."""
        names = sorted(str(name) for name in dataset.data_vars)
        if not names:
            raise RasterContractError("snapshot has no data variables")
        if variable is None:
            return names[0]
        if variable not in names:
            raise RasterContractError(f"variable {variable!r} is not one of {names}")
        return variable

    def _snapshot_metadata(self, repository: icechunk.Repository, snapshot_identifier: str) -> dict[str, Any] | None:
        """Return the commit metadata of one snapshot, or None when its ancestry cannot be read."""
        information = next(iter(repository.ancestry(snapshot_id=snapshot_identifier)), None)
        return None if information is None else information.metadata

    def _describe_handle(
        self,
        handle: RasterReadHandle,
        record: CoverageDataset,
        metadata: dict[str, Any] | None,
    ) -> RasterStoreDescription:
        """Read the grid, variables, extents and terms of one opened snapshot."""
        dataset = handle.dataset
        store_grid = self._store_grid(dataset, record)
        timestamps = self._timestamps(dataset, store_grid.time_dimension)
        return RasterStoreDescription(
            snapshot_identifier=handle.snapshot_identifier,
            variables=tuple(sorted(str(name) for name in dataset.data_vars)),
            crs=store_grid.crs,
            bbox=self._envelope(store_grid, dataset),
            # The extent is the span the axis covers, which is not its first and last cell when a
            # store written before the append guard existed holds an unsorted one.
            temporal_start=min(timestamps) if timestamps else None,
            temporal_end=max(timestamps) if timestamps else None,
            timestep_count=int(dataset.sizes.get(store_grid.time_dimension, 0)),
            time_dimension=store_grid.time_dimension,
            y_dimension=store_grid.y_dimension,
            x_dimension=store_grid.x_dimension,
            shape=(
                int(dataset.sizes.get(store_grid.y_dimension, 0)),
                int(dataset.sizes.get(store_grid.x_dimension, 0)),
            ),
            license=_metadata_text(metadata, LICENSE_METADATA_KEY),
            attribution=_metadata_text(metadata, ATTRIBUTION_METADATA_KEY),
        )

    def _store_grid(self, dataset: xarray.Dataset, record: CoverageDataset) -> _StoreGrid:
        """Read the dimension names, projection and cell sizes an opened store carries."""
        time_dimension, y_dimension, x_dimension = self._store_dimensions(dataset)
        crs = self._store_crs(dataset, record.grid.crs)
        geographic = bool(CRS.from_user_input(crs).is_geographic)
        y_cell_size, x_cell_size = self._store_cell_sizes(
            dataset,
            record.grid,
            y_dimension,
            x_dimension,
            geographic=geographic,
        )
        return _StoreGrid(
            time_dimension=time_dimension,
            y_dimension=y_dimension,
            x_dimension=x_dimension,
            crs=crs,
            geographic=geographic,
            y_cell_size=y_cell_size,
            x_cell_size=x_cell_size,
        )

    def _store_dimensions(self, dataset: xarray.Dataset) -> tuple[str, str, str]:
        """Return the time, y and x dimension names the data variables of a store were written with."""
        names = sorted(str(name) for name in dataset.data_vars)
        if not names:
            raise RasterContractError("snapshot has no data variables")
        dimensions = tuple(str(dimension) for dimension in dataset[names[0]].dims)
        if len(dimensions) != CUBE_DIMENSION_COUNT:
            raise RasterContractError(f"variable {names[0]!r} is not written on a time, y and x cube: {dimensions}")
        return dimensions[0], dimensions[1], dimensions[2]

    def _store_crs(self, dataset: xarray.Dataset, fallback: str) -> str:
        """Return the projection a store records, from its projection code or its grid mapping coordinate."""
        code = dataset.attrs.get(PROJECTION_CODE_ATTRIBUTE)
        if isinstance(code, str) and code:
            return code
        if SPATIAL_REFERENCE_NAME in dataset.coords:
            attributes = dataset[SPATIAL_REFERENCE_NAME].attrs
            for key in (CRS_WELL_KNOWN_TEXT_ATTRIBUTE, SPATIAL_REFERENCE_NAME):
                well_known_text = attributes.get(key)
                if isinstance(well_known_text, str) and well_known_text:
                    return projection_code(well_known_text)
        return fallback

    def _store_cell_sizes(
        self,
        dataset: xarray.Dataset,
        grid: GridSpecification,
        y_dimension: str,
        x_dimension: str,
        *,
        geographic: bool,
    ) -> tuple[float, float]:
        """Return the y and x cell sizes of a store, measured on its own coordinates."""
        # An axis of one cell carries no spacing to measure, so its size comes from the bbox the store
        # declares, and only from the grid of the record when the store declares none either.
        fallback = self._attribute_cell_sizes(
            dataset,
            rows=int(dataset.sizes.get(y_dimension, 0)),
            columns=int(dataset.sizes.get(x_dimension, 0)),
        )
        if fallback is None:
            fallback = build_cell_sizes(grid)
        y_cell_size = _coordinate_spacing(dataset, y_dimension, wrap=False) or fallback[0]
        x_cell_size = _coordinate_spacing(dataset, x_dimension, wrap=geographic) or fallback[1]
        return y_cell_size, x_cell_size

    def _attribute_cell_sizes(self, dataset: xarray.Dataset, *, rows: int, columns: int) -> tuple[float, float] | None:
        """Return the cell sizes the bbox attribute of a store implies, or None when it carries no usable one."""
        values = dataset.attrs.get(SPATIAL_BBOX_ATTRIBUTE)
        if not isinstance(values, list | tuple | numpy.ndarray) or len(values) != BBOX_VALUE_COUNT:
            return None
        if rows < 1 or columns < 1:
            return None
        try:
            numbers = numpy.asarray(values, dtype="float64").ravel()
        except (TypeError, ValueError):
            return None
        y_cell_size = float(numbers[3] - numbers[1]) / rows
        x_cell_size = float(numbers[2] - numbers[0]) / columns
        if not math.isfinite(y_cell_size) or not math.isfinite(x_cell_size):
            return None
        if y_cell_size <= 0.0 or x_cell_size <= 0.0:
            return None
        return y_cell_size, x_cell_size

    def _nodata_value(self, array: xarray.DataArray) -> float | None:
        """Return the fill value the variable of one snapshot records, or None when it records none."""
        # The record describes the newest write, so falling back to it would let a draft that declares
        # a fill value change the statistics a published query reports.
        value = array.attrs.get(NODATA_ATTRIBUTE)
        if isinstance(value, int | float | numpy.integer | numpy.floating) and not isinstance(value, bool):
            number = float(value)
            if math.isfinite(number):
                return number
        return None

    def _apply_time_window(
        self,
        array: xarray.DataArray,
        store_grid: _StoreGrid,
        start: datetime | None,
        end: datetime | None,
    ) -> xarray.DataArray:
        """Restrict an array to a closed time range by masking timestamps rather than slicing labels."""
        if store_grid.time_dimension not in array.dims or (start is None and end is None):
            return array
        if store_grid.time_dimension not in array.coords:
            return array
        # A label slice needs a sorted axis, which a store written before the append guard existed is not:
        # it reads the wrong timesteps for labels it holds and raises KeyError for every other bound.
        values = numpy.asarray(array[store_grid.time_dimension].values, dtype="datetime64[ns]")
        selected = numpy.ones(values.shape, dtype=bool)
        if start is not None:
            selected &= values >= numpy.datetime64(to_naive_utc(start), "ns")
        if end is not None:
            selected &= values <= numpy.datetime64(to_naive_utc(end), "ns")
        return array.isel({store_grid.time_dimension: numpy.flatnonzero(selected)})

    def _apply_spatial_window(
        self,
        array: xarray.DataArray,
        store_grid: _StoreGrid,
        bbox: BoundingBox | None,
    ) -> xarray.DataArray:
        """Restrict an array to a bounding box by masking coordinate values rather than slicing labels."""
        if bbox is None:
            return array
        # A label slice needs a monotonic coordinate, which a grid wrapped across the antimeridian is not.
        selection = {
            store_grid.y_dimension: _coordinate_indices(
                _float_values(array[store_grid.y_dimension]),
                bbox.minimum_y,
                bbox.maximum_y,
                wrap=False,
            ),
            store_grid.x_dimension: _coordinate_indices(
                _float_values(array[store_grid.x_dimension]),
                bbox.minimum_x,
                bbox.maximum_x,
                wrap=store_grid.geographic,
            ),
        }
        return array.isel(selection)

    def _summarise(
        self,
        values: NDArray[Any],
        nodata_value: float | None,
    ) -> tuple[float | None, float | None, float | None]:
        """Return the minimum, maximum and mean of the finite cells, or None when there are none."""
        numbers = numpy.asarray(values, dtype="float64").ravel()
        finite = numbers[numpy.isfinite(numbers)]
        if nodata_value is not None:
            finite = finite[finite != nodata_value]
        if finite.size == 0:
            return None, None, None
        return float(finite.min()), float(finite.max()), float(finite.mean())

    def _envelope(self, store_grid: _StoreGrid, array: xarray.DataArray | xarray.Dataset) -> BoundingBox:
        """Return the envelope of the selected cells, grown by half a cell to cover their extent."""
        y_values = _float_values(array[store_grid.y_dimension])
        x_values = _float_values(array[store_grid.x_dimension])
        return BoundingBox(
            minimum_x=float(x_values.min()) - store_grid.x_cell_size / 2,
            minimum_y=float(y_values.min()) - store_grid.y_cell_size / 2,
            maximum_x=float(x_values.max()) + store_grid.x_cell_size / 2,
            maximum_y=float(y_values.max()) + store_grid.y_cell_size / 2,
        )

    def _published_snapshot(self, repository: icechunk.Repository) -> str | None:
        """Return the snapshot the published branch points at, or None when it does not exist."""
        if PUBLISHED_BRANCH not in repository.list_branches():
            return None
        return repository.lookup_branch(PUBLISHED_BRANCH)

    def _assert_publishable(self, repository: icechunk.Repository, dataset_identifier: str, target: str) -> None:
        """Refuse to publish a snapshot that holds no data, such as the snapshot a repository starts with."""
        try:
            dataset, _ = self._open_dataset(repository.readonly_session(snapshot_id=target))
        except (GroupNotFoundError, FileNotFoundError, KeyError) as error:
            raise NothingToPublishError(
                f"snapshot {target!r} of {dataset_identifier!r} holds no data variables to publish",
            ) from error
        try:
            if not dataset.data_vars:
                raise NothingToPublishError(
                    f"snapshot {target!r} of {dataset_identifier!r} holds no data variables to publish",
                )
        finally:
            dataset.close()

    def _reset_published_branch(
        self,
        repository: icechunk.Repository,
        dataset_identifier: str,
        target: str,
        previous: str,
    ) -> None:
        """Move the published branch with a compare-and-swap against the snapshot it points at."""
        with _publication_conflicts(f"published branch of {dataset_identifier!r} moved since it was read"):
            repository.reset_branch(PUBLISHED_BRANCH, target, from_snapshot_id=previous)

    def _publish_record(self, record: CoverageDataset, target: str, previous: str | None) -> CoverageDataset:
        """Return the record with its publication pointing at the newly published snapshot."""
        publication = Publication(
            published=True,
            published_at=current_timestamp(),
            snapshot_identifier=target,
            previous_snapshot_identifier=previous,
        )
        return record.model_copy(update={"publication": publication, "updated_at": current_timestamp()})

    def _build_record(
        self,
        dataset_identifier: str,
        grid: GridSpecification,
        dataset: xarray.Dataset,
        *,
        title: str | None,
        license: str | None,
        attribution: str | None,
        existing: CoverageDataset | None,
    ) -> CoverageDataset:
        """Build the catalog record of a newly written coverage."""
        timestamps = self._timestamps(dataset, grid.time_dimension)
        now = current_timestamp()
        return CoverageDataset(
            dataset_identifier=dataset_identifier,
            title=title or (existing.title if existing is not None else dataset_identifier),
            storage_key=raster_prefix(dataset_identifier),
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
            bbox=grid.bbox,
            license=license or (existing.license if existing is not None else None),
            attribution=attribution or (existing.attribution if existing is not None else None),
            publication=existing.publication if existing is not None else Publication(),
            grid=grid,
            variables=tuple(sorted(str(name) for name in dataset.data_vars)),
            temporal=TemporalExtent(start=timestamps[0], end=timestamps[-1]) if timestamps else None,
            timestep_count=int(dataset.sizes.get(grid.time_dimension, 0)),
        )

    def _extend_record(self, record: CoverageDataset, dataset: xarray.Dataset) -> CoverageDataset:
        """Return the record widened by the timesteps and variables of an append."""
        timestamps = self._timestamps(dataset, record.grid.time_dimension)
        temporal = record.temporal
        if timestamps:
            start = min(timestamps[0], temporal.start) if temporal is not None else timestamps[0]
            end = max(timestamps[-1], temporal.end) if temporal is not None else timestamps[-1]
            temporal = TemporalExtent(start=start, end=end)
        variables = tuple(sorted(set(record.variables) | {str(name) for name in dataset.data_vars}))
        return record.model_copy(
            update={
                "temporal": temporal,
                "variables": variables,
                "timestep_count": record.timestep_count + int(dataset.sizes.get(record.grid.time_dimension, 0)),
                "updated_at": current_timestamp(),
            },
        )

    def _timestamps(self, dataset: xarray.Dataset, time_dimension: str) -> list[datetime]:
        """Return the time coordinate of a cube as naive Python datetimes."""
        if time_dimension not in dataset.coords:
            return []
        values = numpy.asarray(dataset[time_dimension].values)
        if not numpy.issubdtype(values.dtype, numpy.datetime64):
            return []
        return [_as_datetime(value) for value in values.astype("datetime64[us]")]

    def _write_result(self, record: CoverageDataset, snapshot_identifier: str) -> RasterWriteResult:
        """Build the result reported after a create or an append."""
        return RasterWriteResult(
            dataset_identifier=record.dataset_identifier,
            snapshot_identifier=snapshot_identifier,
            timestep_count=record.timestep_count,
            variables=record.variables,
            published=record.publication.published,
        )


def _commit_metadata(license: str | None, attribution: str | None) -> dict[str, Any]:
    """Return the commit metadata of a write, carrying only the terms the write actually declares."""
    declared = {LICENSE_METADATA_KEY: license, ATTRIBUTION_METADATA_KEY: attribution}
    return {name: value for name, value in declared.items() if value is not None}


def _metadata_text(metadata: dict[str, Any] | None, key: str) -> str | None:
    """Return one commit metadata entry as text, or None when it is absent or was not written as text."""
    if metadata is None:
        return None
    value = metadata.get(key)
    return value if isinstance(value, str) else None


def _incoming_timestep_count(dataset: xarray.Dataset, time_dimension: str) -> int:
    """Return how many timesteps a cube carries, counting a cube without a time axis as one."""
    return int(dataset.sizes.get(time_dimension, 1))


def _coordinate_indices(values: FloatArray, minimum: float, maximum: float, *, wrap: bool) -> IndexArray:
    """Return the positions of the coordinate values inside a closed range, wrapping longitudes when asked."""
    if not wrap:
        return numpy.flatnonzero((values >= minimum) & (values <= maximum))
    bounds = wrap_longitudes(numpy.asarray([minimum, maximum], dtype="float64"))
    low, high = float(bounds[0]), float(bounds[1])
    if low <= high:
        return numpy.flatnonzero((values >= low) & (values <= high))
    # The window crosses the antimeridian, so the cells it covers are the union of its two halves.
    return numpy.flatnonzero((values >= low) | (values <= high))


def _coordinate_spacing(dataset: xarray.Dataset, dimension: str, *, wrap: bool) -> float:
    """Return the median step of a coordinate, or zero when it is absent or holds a single cell."""
    if dimension not in dataset.coords:
        return 0.0
    values = _float_values(dataset[dimension])
    if values.size < 2:
        return 0.0
    steps = numpy.diff(values)
    if wrap:
        # One step of a wrapped longitude axis jumps the whole span; folding it back keeps the median honest.
        steps = (steps + MAXIMUM_LONGITUDE) % LONGITUDE_SPAN - MAXIMUM_LONGITUDE
    spacing = float(numpy.median(numpy.abs(steps)))
    if not math.isfinite(spacing) or spacing <= 0.0:
        return 0.0
    return spacing


def _float_values(array: xarray.DataArray) -> FloatArray:
    """Return the values of a coordinate as a one dimensional float array."""
    return numpy.asarray(array.values, dtype="float64").ravel()


def _as_datetime(value: numpy.datetime64) -> datetime:
    """Convert a numpy datetime into a naive Python datetime."""
    converted = value.item()
    if not isinstance(converted, datetime):
        raise RasterContractError(f"time coordinate value is not a timestamp: {value!r}")
    return converted
