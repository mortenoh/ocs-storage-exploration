"""Raster engine creating, reading, querying and publishing Icechunk-backed GeoZarr coverages."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

import icechunk
import numpy
import xarray
from icechunk.xarray import to_icechunk
from numpy.typing import NDArray
from zarr.errors import GroupNotFoundError

from ocs_storage_exploration.storage.addresses import StorageAddress
from ocs_storage_exploration.storage.errors import (
    DatasetAlreadyExistsError,
    ItemTypeMismatchError,
    NothingToPublishError,
    PublicationConflictError,
    QuerySizeGuardError,
    RasterContractError,
    SnapshotNotFoundError,
)
from ocs_storage_exploration.storage.keys import raster_prefix, validate_dataset_identifier
from ocs_storage_exploration.storage.models import (
    BoundingBox,
    CoverageDataset,
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
from ocs_storage_exploration.storage.protocols import Catalog, StorageBackend
from ocs_storage_exploration.storage.raster.grid import (
    apply_geozarr_attributes,
    assert_finite_attributes,
    build_cell_sizes,
    build_coordinates,
    to_naive_utc,
)

if TYPE_CHECKING:
    from ocs_storage_exploration.settings import Settings

MAIN_BRANCH: Final[str] = "main"
PUBLISHED_BRANCH: Final[str] = "published"
FALLBACK_GROUP: Final[str] = "0"
DEFAULT_VERSION_LIMIT: Final[int] = 100
DEFAULT_CREATE_MESSAGE: Final[str] = "initial write"
DEFAULT_APPEND_MESSAGE: Final[str] = "append"

FloatArray = NDArray[numpy.float64]


class VersionSelector(StrEnum):
    """Pointer a raster read follows when no explicit snapshot is requested."""

    PUBLISHED = "published"
    DRAFT = "draft"


@dataclass(frozen=True, slots=True)
class RasterReadHandle:
    """Open dataset of one raster snapshot together with the snapshot and group it was read from."""

    dataset: xarray.Dataset
    snapshot_identifier: str
    group: str | None = None


def oriented_slice(values: FloatArray, minimum: float, maximum: float) -> slice:
    """Return a label slice ordered to match an ascending or a descending coordinate."""
    if values.size > 1 and bool(values[0] > values[-1]):
        return slice(maximum, minimum)
    return slice(minimum, maximum)


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
        overwrite: bool = False,
        message: str = DEFAULT_CREATE_MESSAGE,
    ) -> RasterWriteResult:
        """Write a coverage onto the main branch and record it in the catalog."""
        identifier = validate_dataset_identifier(dataset_identifier)
        existing = self._existing_coverage(identifier, overwrite=overwrite)
        prepared = self._prepare(grid, dataset)
        repository = self._open_repository(identifier)
        session = repository.writable_session(MAIN_BRANCH)
        to_icechunk(prepared, session, mode="w")
        snapshot_identifier = session.commit(message)
        record = self._build_record(identifier, grid, prepared, title=title, existing=existing)
        self._catalog.put(record)
        return self._write_result(record, snapshot_identifier)

    def append(
        self,
        dataset_identifier: str,
        dataset: xarray.Dataset,
        *,
        message: str = DEFAULT_APPEND_MESSAGE,
    ) -> RasterWriteResult:
        """Append timesteps to a coverage after checking that its spatial coordinates match."""
        identifier = validate_dataset_identifier(dataset_identifier)
        record = self._require_coverage(identifier)
        prepared = self._prepare(record.grid, dataset)
        repository = self._open_repository(identifier)
        self._assert_committed_coordinates(repository, record, prepared)
        session = repository.writable_session(MAIN_BRANCH)
        to_icechunk(prepared, session, append_dim=record.grid.time_dimension)
        snapshot_identifier = session.commit(message)
        updated = self._extend_record(record, prepared)
        self._catalog.put(updated)
        return self._write_result(updated, snapshot_identifier)

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
        session = self._readonly_session(repository, record, version, snapshot_identifier)
        dataset, group = self._open_dataset(session)
        try:
            yield RasterReadHandle(dataset=dataset, snapshot_identifier=session.snapshot_id, group=group)
        finally:
            dataset.close()

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
        grid = record.grid
        with self.read(identifier, version=version, snapshot_identifier=snapshot_identifier) as handle:
            name = self._select_variable(handle.dataset, variable)
            window = self._apply_spatial_window(
                self._apply_time_window(handle.dataset[name], grid, start, end), grid, bbox
            )
            cell_count = int(window.size)
            if cell_count == 0:
                raise QuerySizeGuardError(f"query window of {identifier!r} selects no cells")
            if cell_count > self._settings.max_query_cell_count:
                raise QuerySizeGuardError(
                    f"query window of {identifier!r} reads {cell_count} cells, "
                    f"more than the {self._settings.max_query_cell_count} allowed",
                )
            minimum, maximum, mean = self._summarise(window.values, grid.nodata_value)
            return RasterQuerySummary(
                dataset_identifier=identifier,
                variable=name,
                bbox=self._window_bbox(grid, window),
                crs=grid.crs,
                snapshot_identifier=handle.snapshot_identifier,
                timestep_count=int(window.sizes.get(grid.time_dimension, 1)),
                cell_count=cell_count,
                minimum=minimum,
                maximum=maximum,
                mean=mean,
            )

    def publish(self, dataset_identifier: str, *, snapshot_identifier: str | None = None) -> PublicationResult:
        """Move the published branch onto a snapshot of the main branch, creating it on first publication."""
        identifier = validate_dataset_identifier(dataset_identifier)
        record = self._require_coverage(identifier)
        repository = self._open_repository(identifier)
        ancestry = list(repository.ancestry(branch=MAIN_BRANCH))
        target = snapshot_identifier if snapshot_identifier is not None else repository.lookup_branch(MAIN_BRANCH)
        if target not in {information.id for information in ancestry}:
            raise SnapshotNotFoundError(f"snapshot {target!r} is not in the history of {identifier!r}")
        if not ancestry or ancestry[0].parent_id is None:
            raise NothingToPublishError(f"dataset {identifier!r} has no committed content")
        previous = self._published_snapshot(repository)
        changed = previous != target or not record.publication.published
        if previous is None:
            repository.create_branch(PUBLISHED_BRANCH, target)
        elif previous != target:
            self._reset_published_branch(repository, identifier, target, previous)
        if changed:
            self._catalog.put(self._publish_record(record, target, previous))
        return PublicationResult(
            dataset_identifier=identifier,
            item_type=ItemType.COVERAGE,
            published=True,
            changed=changed,
            snapshot_identifier=target,
            previous_snapshot_identifier=previous,
        )

    def versions(self, dataset_identifier: str, *, limit: int = DEFAULT_VERSION_LIMIT) -> list[RasterVersion]:
        """List the snapshots of the main branch newest first, marking the published one."""
        identifier = validate_dataset_identifier(dataset_identifier)
        self._require_coverage(identifier)
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
        """Delete the catalog record of a coverage first, then every object of its repository."""
        identifier = validate_dataset_identifier(dataset_identifier)
        self._require_coverage(identifier)
        self._catalog.delete(identifier)
        self._backend.delete_prefix(self.repository_address(identifier))

    def _open_repository(self, dataset_identifier: str) -> icechunk.Repository:
        """Open the Icechunk repository of a coverage, creating it when it does not exist."""
        return icechunk.Repository.open_or_create(
            self._backend.icechunk_storage(self.repository_address(dataset_identifier))
        )

    def _existing_coverage(self, dataset_identifier: str, *, overwrite: bool) -> CoverageDataset | None:
        """Return the record being overwritten, refusing an existing dataset unless overwrite is set."""
        existing = self._catalog.get(dataset_identifier)
        if existing is None:
            return None
        if not isinstance(existing, CoverageDataset):
            raise ItemTypeMismatchError(f"dataset {dataset_identifier!r} is not a coverage")
        if not overwrite:
            raise DatasetAlreadyExistsError(f"dataset {dataset_identifier!r} already exists")
        return existing

    def _require_coverage(self, dataset_identifier: str) -> CoverageDataset:
        """Read the coverage record of a dataset or raise."""
        record = self._catalog.require(dataset_identifier)
        if not isinstance(record, CoverageDataset):
            raise ItemTypeMismatchError(f"dataset {dataset_identifier!r} is not a coverage")
        return record

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

    def _assert_committed_coordinates(
        self,
        repository: icechunk.Repository,
        record: CoverageDataset,
        dataset: xarray.Dataset,
    ) -> None:
        """Refuse an append whose spatial coordinates differ from the committed ones."""
        committed, _ = self._open_dataset(repository.readonly_session(MAIN_BRANCH))
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
            missing = sorted({str(name) for name in committed.data_vars} - {str(name) for name in dataset.data_vars})
            if missing:
                raise RasterContractError(f"append is missing the committed variables {missing}")
        finally:
            committed.close()

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
        if version is VersionSelector.PUBLISHED and record.publication.published:
            if PUBLISHED_BRANCH in repository.list_branches():
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

    def _apply_time_window(
        self,
        array: xarray.DataArray,
        grid: GridSpecification,
        start: datetime | None,
        end: datetime | None,
    ) -> xarray.DataArray:
        """Restrict an array to a closed time range when one is requested."""
        if grid.time_dimension not in array.dims or (start is None and end is None):
            return array
        lower = to_naive_utc(start) if start is not None else None
        upper = to_naive_utc(end) if end is not None else None
        return array.sel({grid.time_dimension: slice(lower, upper)})

    def _apply_spatial_window(
        self,
        array: xarray.DataArray,
        grid: GridSpecification,
        bbox: BoundingBox | None,
    ) -> xarray.DataArray:
        """Restrict an array to a bounding box, honouring descending coordinates."""
        if bbox is None:
            return array
        selection = {
            grid.y_dimension: oriented_slice(_float_values(array[grid.y_dimension]), bbox.minimum_y, bbox.maximum_y),
            grid.x_dimension: oriented_slice(_float_values(array[grid.x_dimension]), bbox.minimum_x, bbox.maximum_x),
        }
        return array.sel(selection)

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

    def _window_bbox(self, grid: GridSpecification, array: xarray.DataArray) -> BoundingBox:
        """Return the envelope of the selected cells, grown by half a cell to cover their extent."""
        y_size, x_size = build_cell_sizes(grid)
        y_values = _float_values(array[grid.y_dimension])
        x_values = _float_values(array[grid.x_dimension])
        return BoundingBox(
            minimum_x=float(x_values.min()) - x_size / 2,
            minimum_y=float(y_values.min()) - y_size / 2,
            maximum_x=float(x_values.max()) + x_size / 2,
            maximum_y=float(y_values.max()) + y_size / 2,
        )

    def _published_snapshot(self, repository: icechunk.Repository) -> str | None:
        """Return the snapshot the published branch points at, or None when it does not exist."""
        if PUBLISHED_BRANCH not in repository.list_branches():
            return None
        return repository.lookup_branch(PUBLISHED_BRANCH)

    def _reset_published_branch(
        self,
        repository: icechunk.Repository,
        dataset_identifier: str,
        target: str,
        previous: str,
    ) -> None:
        """Move the published branch with a compare-and-swap against the snapshot it points at."""
        try:
            repository.reset_branch(PUBLISHED_BRANCH, target, from_snapshot_id=previous)
        except icechunk.ConflictError as error:
            raise PublicationConflictError(
                f"published branch of {dataset_identifier!r} moved since it was read",
            ) from error

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
        existing: CoverageDataset | None,
    ) -> CoverageDataset:
        """Build the catalog record of a newly written coverage."""
        timestamps = self._timestamps(dataset, grid)
        now = current_timestamp()
        return CoverageDataset(
            dataset_identifier=dataset_identifier,
            title=title or (existing.title if existing is not None else dataset_identifier),
            address=self.repository_address(dataset_identifier).as_uri(),
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
            bbox=grid.bbox,
            publication=existing.publication if existing is not None else Publication(),
            grid=grid,
            variables=tuple(sorted(str(name) for name in dataset.data_vars)),
            temporal=TemporalExtent(start=timestamps[0], end=timestamps[-1]) if timestamps else None,
            timestep_count=int(dataset.sizes.get(grid.time_dimension, 0)),
        )

    def _extend_record(self, record: CoverageDataset, dataset: xarray.Dataset) -> CoverageDataset:
        """Return the record widened by the timesteps and variables of an append."""
        timestamps = self._timestamps(dataset, record.grid)
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

    def _timestamps(self, dataset: xarray.Dataset, grid: GridSpecification) -> list[datetime]:
        """Return the time coordinate of a cube as naive Python datetimes."""
        if grid.time_dimension not in dataset.coords:
            return []
        values = numpy.asarray(dataset[grid.time_dimension].values)
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


def _float_values(array: xarray.DataArray) -> FloatArray:
    """Return the values of a coordinate as a one dimensional float array."""
    return numpy.asarray(array.values, dtype="float64").ravel()


def _as_datetime(value: numpy.datetime64) -> datetime:
    """Convert a numpy datetime into a naive Python datetime."""
    converted = value.item()
    if not isinstance(converted, datetime):
        raise RasterContractError(f"time coordinate value is not a timestamp: {value!r}")
    return converted
