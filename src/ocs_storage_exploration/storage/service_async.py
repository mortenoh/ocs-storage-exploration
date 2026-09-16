"""Awaitable facade over the storage service: a native async catalog and bounded worker threads for the engines."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, TypeVar

import anyio
import anyio.to_thread

from ocs_storage_exploration.storage.catalog_async import AsyncObjectCatalog
from ocs_storage_exploration.storage.errors import StorageTimeoutError
from ocs_storage_exploration.storage.protocols import AsyncCatalog, StorageBackend
from ocs_storage_exploration.storage.raster.ingest import RasterIngestPlan, ingest_raster_files
from ocs_storage_exploration.storage.raster.repository import (
    DEFAULT_APPEND_MESSAGE,
    DEFAULT_CREATE_MESSAGE,
    DEFAULT_VERSION_LIMIT,
    RasterRepository,
    RasterStoreDescription,
    VersionSelector,
)
from ocs_storage_exploration.storage.schemas import (
    BackendDescription,
    BoundingBox,
    CoverageDataset,
    Dataset,
    FeatureDataset,
    GridSpecification,
    ItemType,
    PublicationResult,
    RasterIngestResult,
    RasterQuerySummary,
    RasterVersion,
    RasterWriteResult,
    VectorVersionMetadata,
    VectorWriteResult,
)
from ocs_storage_exploration.storage.service import (
    StorageService,
    require_collection_record,
    require_coverage_record,
    require_live_record,
)
from ocs_storage_exploration.storage.vector.collection import (
    DEFAULT_CRS,
    MAXIMUM_RESERVATION_ATTEMPTS,
    VectorCollectionPointer,
    VectorCollectionStore,
    VectorReadHandle,
    VectorTableSchema,
)
from ocs_storage_exploration.storage.vector.ingest import VectorIngestPlan, ingest_vector_file

if TYPE_CHECKING:
    from datetime import datetime

    import geopandas
    import xarray
    from geojson_pydantic import FeatureCollection
    from pluginkit import PluginManager

    from ocs_storage_exploration.settings import Settings
    from ocs_storage_exploration.storage.vector.predicates import WhereClause

ResultT = TypeVar("ResultT")

type RasterCubeSource = xarray.Dataset | Callable[[], xarray.Dataset]

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

# How long a shutdown waits for the threads of abandoned calls. It is deliberately not the storage
# timeout: that one is sized for the slowest legitimate call (180 seconds by default) and using it
# here would hold a container in its shutdown grace period for minutes over a call nobody awaits.
ABANDONED_DRAIN_SECONDS: Final[float] = 5.0


def resolve_cube(source: RasterCubeSource) -> xarray.Dataset:
    """Return the cube of a source, calling a deferred one so it is allocated wherever this runs."""
    if callable(source):
        return source()
    return source


class StorageOperationRunner:
    """Runs one blocking storage call at a time per limiter token, and never longer than the timeout.

    The limiter bounds how many engine calls are in flight at once, so a burst of requests cannot open
    more Icechunk sessions or Parquet readers than the deployment was sized for. The timeout bounds how
    long a caller waits for a token and for the call itself.

    A call that outlives the timeout is abandoned rather than cancelled: no library here offers
    cancellation, so its thread runs to completion with its result discarded. It keeps its limiter
    token until that thread returns, so a wedged call occupies a slot instead of letting the next
    caller start a second thread the deployment was never sized for. A caller still queued for a
    token is cancelled outright, because nobody is waiting for the work any more.
    """

    def __init__(self, *, max_concurrent_operations: int, timeout_seconds: float) -> None:
        """Size the limiter and record the timeout every operation is bounded by."""
        self._limiter = anyio.CapacityLimiter(max_concurrent_operations)
        # A call holds its operation token before it asks for a thread, so this second limiter of the
        # same size can never block. It exists only to keep anyio's process-wide default thread
        # limiter, which is sized for the whole program, out of this bound.
        self._worker_limiter = anyio.CapacityLimiter(max_concurrent_operations)
        self._timeout_seconds = timeout_seconds
        self._abandoned: set[asyncio.Task[Any]] = set()

    @property
    def limiter(self) -> anyio.CapacityLimiter:
        """Limiter bounding how many storage operations run at once."""
        return self._limiter

    @property
    def timeout_seconds(self) -> float:
        """Wall time one storage operation may spend waiting for a token and running."""
        return self._timeout_seconds

    @property
    def abandoned_count(self) -> int:
        """How many timed-out calls still hold a worker thread and its limiter token."""
        return len(self._abandoned)

    async def run(self, operation: Callable[[], ResultT], *, description: str) -> ResultT:
        """Run one blocking storage call on a worker thread, bounded by the limiter and the timeout."""
        holding = asyncio.Event()
        task = asyncio.ensure_future(self._hold_token_and_run(operation, holding))
        try:
            _, pending = await asyncio.wait({task}, timeout=self._timeout_seconds)
        except asyncio.CancelledError:
            self._stop_waiting(task, holding, description)
            raise
        if pending:
            self._stop_waiting(task, holding, description)
            raise StorageTimeoutError(f"{description} did not finish within {self._timeout_seconds} seconds")
        return task.result()

    async def aclose(self) -> None:
        """Wait, with a bound, for the threads of the calls this runner abandoned."""
        draining = set(self._abandoned)
        if not draining:
            return
        await asyncio.wait(draining, timeout=ABANDONED_DRAIN_SECONDS)

    async def _hold_token_and_run(self, operation: Callable[[], ResultT], holding: asyncio.Event) -> ResultT:
        """Hold one limiter token for the whole life of a blocking call, the worker thread included."""
        # The token is taken here rather than by `run_sync` so that it is this task that borrows it:
        # a task abandoned with its thread still running gives the token back when the thread returns.
        async with self._limiter:
            holding.set()
            return await anyio.to_thread.run_sync(operation, abandon_on_cancel=False, limiter=self._worker_limiter)

    def _stop_waiting(self, task: asyncio.Task[Any], holding: asyncio.Event, description: str) -> None:
        """Drop a call still queued for a token, and abandon one that already holds a worker thread."""
        # The event loop keeps only a weak reference to a running task, so either branch has to hold one.
        task.add_done_callback(self._release_abandoned)
        if not holding.is_set():
            # Nothing was borrowed and no thread was started, so the call can simply be dropped.
            task.cancel()
            return
        # Cancelling now would hand the token to the next caller while the thread it was taken for is
        # still running, which is how a limit of one ended up running four threads at once.
        self._abandoned.add(task)
        LOGGER.warning(
            "%s did not finish within %s seconds and was abandoned; its worker thread keeps its limiter token",
            description,
            self._timeout_seconds,
        )

    def _release_abandoned(self, task: asyncio.Task[Any]) -> None:
        """Forget an abandoned call once its thread returned, consuming whatever it raised."""
        self._abandoned.discard(task)
        if not task.cancelled():
            # Nobody is left to receive it, and an unretrieved exception would be logged on collection.
            task.exception()


class AsyncRasterRepository:
    """Awaitable counterpart of RasterRepository, running every call on a bounded worker thread."""

    def __init__(self, repository: RasterRepository, runner: StorageOperationRunner) -> None:
        """Bind the facade to one raster engine and the runner that bounds it."""
        self._repository = repository
        self._runner = runner

    @property
    def repository(self) -> RasterRepository:
        """Raster engine this facade awaits."""
        return self._repository

    async def create(
        self,
        dataset_identifier: str,
        grid: GridSpecification,
        dataset: RasterCubeSource,
        *,
        title: str | None = None,
        license: str | None = None,
        attribution: str | None = None,
        overwrite: bool = False,
        message: str = DEFAULT_CREATE_MESSAGE,
    ) -> RasterWriteResult:
        """Write a coverage onto the main branch and record it in the catalog.

        A caller that can defer building its cube passes a callable instead of a dataset: the cube is
        then allocated on the worker thread, inside the limiter and the timeout, rather than on the
        event loop of a caller that would block every other request while it fills the array.
        """
        return await self._runner.run(
            lambda: self._repository.create(
                dataset_identifier,
                grid,
                resolve_cube(dataset),
                title=title,
                license=license,
                attribution=attribution,
                overwrite=overwrite,
                message=message,
            ),
            description=f"creating coverage {dataset_identifier!r}",
        )

    async def append(
        self,
        dataset_identifier: str,
        dataset: RasterCubeSource,
        *,
        message: str = DEFAULT_APPEND_MESSAGE,
    ) -> RasterWriteResult:
        """Append timesteps to a coverage after checking that it matches what is already committed."""
        return await self._runner.run(
            lambda: self._repository.append(dataset_identifier, resolve_cube(dataset), message=message),
            description=f"appending to coverage {dataset_identifier!r}",
        )

    async def ingest(self, dataset_identifier: str, plan: RasterIngestPlan) -> RasterIngestResult:
        """Write the files of an ingest plan as one coverage, creating it and appending the rest in order.

        The whole plan runs as one operation rather than one per file: it spends a single limiter token
        for a sequence of writes that have to stay in order anyway, and the timeout then bounds the
        ingest a caller is waiting on rather than each file inside it.
        """
        return await self._runner.run(
            lambda: ingest_raster_files(self._repository, dataset_identifier, plan),
            description=f"ingesting {len(plan.files)} files into coverage {dataset_identifier!r}",
        )

    async def describe(
        self,
        dataset_identifier: str,
        *,
        version: VersionSelector = VersionSelector.PUBLISHED,
        snapshot_identifier: str | None = None,
    ) -> RasterStoreDescription:
        """Describe one coverage snapshot from the store it was written to rather than from its record."""
        return await self._runner.run(
            lambda: self._repository.describe(
                dataset_identifier,
                version=version,
                snapshot_identifier=snapshot_identifier,
            ),
            description=f"describing coverage {dataset_identifier!r}",
        )

    async def query(
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
        return await self._runner.run(
            lambda: self._repository.query(
                dataset_identifier,
                variable=variable,
                bbox=bbox,
                start=start,
                end=end,
                version=version,
                snapshot_identifier=snapshot_identifier,
            ),
            description=f"querying coverage {dataset_identifier!r}",
        )

    async def publish(self, dataset_identifier: str, *, snapshot_identifier: str | None = None) -> PublicationResult:
        """Move the published branch onto a snapshot of the main branch, creating it on first publication."""
        return await self._runner.run(
            lambda: self._repository.publish(dataset_identifier, snapshot_identifier=snapshot_identifier),
            description=f"publishing coverage {dataset_identifier!r}",
        )

    async def reconcile_publication(self, dataset_identifier: str) -> CoverageDataset:
        """Rewrite the publication block of a record from the published branch, which is the only truth."""
        return await self._runner.run(
            lambda: self._repository.reconcile_publication(dataset_identifier),
            description=f"reconciling coverage {dataset_identifier!r}",
        )

    async def versions(self, dataset_identifier: str, *, limit: int = DEFAULT_VERSION_LIMIT) -> list[RasterVersion]:
        """List the snapshots of the main branch newest first, marking the published one."""
        return await self._runner.run(
            lambda: self._repository.versions(dataset_identifier, limit=limit),
            description=f"listing the snapshots of coverage {dataset_identifier!r}",
        )

    async def root_attributes(
        self,
        dataset_identifier: str,
        *,
        version: VersionSelector = VersionSelector.PUBLISHED,
    ) -> dict[str, Any]:
        """Return the root attributes of a coverage as they were written."""
        return await self._runner.run(
            lambda: self._repository.root_attributes(dataset_identifier, version=version),
            description=f"reading the attributes of coverage {dataset_identifier!r}",
        )

    async def delete(self, dataset_identifier: str) -> None:
        """Delete the catalog record of a coverage first, then every object of its repository."""
        await self._runner.run(
            lambda: self._repository.delete(dataset_identifier),
            description=f"deleting coverage {dataset_identifier!r}",
        )


class AsyncVectorCollectionStore:
    """Awaitable counterpart of VectorCollectionStore, running every call on a bounded worker thread."""

    def __init__(self, store: VectorCollectionStore, runner: StorageOperationRunner) -> None:
        """Bind the facade to one vector engine and the runner that bounds it."""
        self._store = store
        self._runner = runner

    @property
    def store(self) -> VectorCollectionStore:
        """Vector engine this facade awaits."""
        return self._store

    async def write(
        self,
        collection_identifier: str,
        frame: geopandas.GeoDataFrame,
        *,
        identifier_property: str,
        title: str | None = None,
        license: str | None = None,
        attribution: str | None = None,
        selectable_columns: Sequence[str] = (),
        publish: bool = False,
    ) -> VectorWriteResult:
        """Write a frame as the next version of a collection and optionally publish it right away."""
        return await self._runner.run(
            lambda: self._store.write(
                collection_identifier,
                frame,
                identifier_property=identifier_property,
                title=title,
                license=license,
                attribution=attribution,
                selectable_columns=selectable_columns,
                publish=publish,
            ),
            description=f"writing collection {collection_identifier!r}",
        )

    async def write_geojson(
        self,
        collection_identifier: str,
        feature_collection: FeatureCollection | Mapping[str, Any],
        *,
        identifier_property: str,
        crs: str = DEFAULT_CRS,
        title: str | None = None,
        license: str | None = None,
        attribution: str | None = None,
        selectable_columns: Sequence[str] = (),
        publish: bool = False,
    ) -> VectorWriteResult:
        """Write a GeoJSON FeatureCollection as the next version of a collection."""
        return await self._runner.run(
            lambda: self._store.write_geojson(
                collection_identifier,
                feature_collection,
                identifier_property=identifier_property,
                crs=crs,
                title=title,
                license=license,
                attribution=attribution,
                selectable_columns=selectable_columns,
                publish=publish,
            ),
            description=f"writing collection {collection_identifier!r}",
        )

    async def ingest(self, collection_identifier: str, plan: VectorIngestPlan) -> VectorWriteResult:
        """Read one local vector file and write it as the next version of a collection."""
        return await self._runner.run(
            lambda: ingest_vector_file(self._store, collection_identifier, plan),
            description=f"ingesting {plan.path.name!r} into collection {collection_identifier!r}",
        )

    async def read(
        self,
        collection_identifier: str,
        *,
        bbox: BoundingBox | None = None,
        bbox_crs: str = DEFAULT_CRS,
        where: WhereClause | None = None,
        columns: Sequence[str] | None = None,
        limit: int | None = None,
        version: int | None = None,
    ) -> VectorReadHandle:
        """Read one version of a collection, pruning by envelope and clause before the exact intersection test."""
        return await self._runner.run(
            lambda: self._store.read(
                collection_identifier,
                bbox=bbox,
                bbox_crs=bbox_crs,
                where=where,
                columns=columns,
                limit=limit,
                version=version,
            ),
            description=f"reading collection {collection_identifier!r}",
        )

    async def publish(self, collection_identifier: str, *, version: int | None = None) -> PublicationResult:
        """Move the published pointer of a collection to one version, which is also how a rollback works."""
        return await self._runner.run(
            lambda: self._store.publish(collection_identifier, version=version),
            description=f"publishing collection {collection_identifier!r}",
        )

    async def versions(self, collection_identifier: str) -> list[int]:
        """List the version numbers a collection finished writing, in ascending order."""
        return await self._runner.run(
            lambda: self._store.versions(collection_identifier),
            description=f"listing the versions of collection {collection_identifier!r}",
        )

    async def claimed_versions(self, collection_identifier: str) -> list[int]:
        """List every version number a collection claimed, including a write that never finished."""
        return await self._runner.run(
            lambda: self._store.claimed_versions(collection_identifier),
            description=f"listing the claimed versions of collection {collection_identifier!r}",
        )

    async def reserve_version(
        self,
        collection_identifier: str,
        *,
        attempts: int = MAXIMUM_RESERVATION_ATTEMPTS,
    ) -> int:
        """Claim the next free version number by creating its reservation object, which no two writers share."""
        return await self._runner.run(
            lambda: self._store.reserve_version(collection_identifier, attempts=attempts),
            description=f"reserving a version of collection {collection_identifier!r}",
        )

    async def version_metadata(self, collection_identifier: str, version: int) -> VectorVersionMetadata:
        """Read the metadata sidecar describing one version of a collection as it was written."""
        return await self._runner.run(
            lambda: self._store.version_metadata(collection_identifier, version),
            description=f"reading version {version} of collection {collection_identifier!r}",
        )

    async def published_metadata(self, collection_identifier: str) -> VectorVersionMetadata | None:
        """Read the metadata sidecar of the published version, or None while nothing is published."""
        return await self._runner.run(
            lambda: self._store.published_metadata(collection_identifier),
            description=f"reading the published version of collection {collection_identifier!r}",
        )

    async def table_schema(self, collection_identifier: str, *, version: int | None = None) -> VectorTableSchema:
        """Describe one version of a collection from its Parquet footer alone, reading no row group."""
        return await self._runner.run(
            lambda: self._store.table_schema(collection_identifier, version=version),
            description=f"reading the table schema of collection {collection_identifier!r}",
        )

    async def pointer(self, collection_identifier: str) -> VectorCollectionPointer | None:
        """Read the published-version pointer object of a collection, or None when nothing is published."""
        return await self._runner.run(
            lambda: self._store.pointer(collection_identifier),
            description=f"reading the pointer of collection {collection_identifier!r}",
        )

    async def current_version(self, collection_identifier: str) -> int | None:
        """Return the published version of a collection, or None when nothing is published."""
        return await self._runner.run(
            lambda: self._store.current_version(collection_identifier),
            description=f"reading the published version of collection {collection_identifier!r}",
        )

    async def delete(self, collection_identifier: str) -> int:
        """Delete the catalog record of a collection first, then every object below its prefix."""
        return await self._runner.run(
            lambda: self._store.delete(collection_identifier),
            description=f"deleting collection {collection_identifier!r}",
        )


class AsyncStorageService:
    """Awaitable facade over one StorageService, for callers that own an event loop rather than a thread.

    Catalog reads are answered natively through obstore's async API, because obstore is the one layer of
    this service that is asynchronous at all. Every engine call is a blocking Icechunk, xarray, geopandas
    or pyarrow call, so it runs on a worker thread instead, bounded by one capacity limiter and one
    timeout shared by both engines.
    """

    def __init__(self, service: StorageService, catalog: AsyncCatalog | None = None) -> None:
        """Wrap one sync service, sizing the runner from the settings it was built with."""
        self._service = service
        self._catalog = catalog if catalog is not None else AsyncObjectCatalog(service.backend)
        self._runner = StorageOperationRunner(
            max_concurrent_operations=service.settings.max_concurrent_storage_operations,
            timeout_seconds=service.settings.storage_operation_timeout_seconds,
        )
        self._raster = AsyncRasterRepository(service.raster, self._runner)
        self._vector = AsyncVectorCollectionStore(service.vector, self._runner)

    @classmethod
    def from_settings(cls, settings: Settings, plugin_manager: PluginManager | None = None) -> AsyncStorageService:
        """Build the sync service from one settings block and wrap it in this facade."""
        return cls(StorageService.from_settings(settings, plugin_manager))

    @property
    def service(self) -> StorageService:
        """Sync service this facade wraps, for the projections that take one."""
        return self._service

    @property
    def settings(self) -> Settings:
        """Settings the wrapped service was built with."""
        return self._service.settings

    @property
    def backend(self) -> StorageBackend:
        """Backend the wrapped service resolved."""
        return self._service.backend

    @property
    def catalog(self) -> AsyncCatalog:
        """Catalog answering record reads and writes natively, without a worker thread."""
        return self._catalog

    @property
    def raster(self) -> AsyncRasterRepository:
        """Awaitable raster engine."""
        return self._raster

    @property
    def vector(self) -> AsyncVectorCollectionStore:
        """Awaitable vector engine."""
        return self._vector

    @property
    def runner(self) -> StorageOperationRunner:
        """Runner bounding every engine call of this facade."""
        return self._runner

    async def aclose(self) -> None:
        """Wait, with a bound, for the worker threads of every call this facade abandoned."""
        await self._runner.aclose()

    async def run_blocking(self, operation: Callable[[], ResultT], *, description: str) -> ResultT:
        """Run one composed blocking storage call on a worker thread under the same limiter and timeout.

        This is the seam for a projection that needs several engine calls in one go, such as building a
        STAC collection: running it as one operation spends one limiter token instead of several, and
        bounds the whole projection rather than each call inside it.
        """
        return await self._runner.run(operation, description=description)

    async def describe_backends(self) -> list[BackendDescription]:
        """Describe the active backend, then every other scheme the plugins provide as inactive."""
        return await self._runner.run(self._service.describe_backends, description="describing the backends")

    async def list_datasets(self, item_type: ItemType | None = None) -> list[Dataset]:
        """List every dataset record, optionally filtered by item type."""
        return await self._catalog.list_datasets(item_type)

    async def get_dataset(self, dataset_identifier: str) -> Dataset:
        """Read one dataset record or raise DatasetNotFoundError."""
        return require_live_record(await self._catalog.require(dataset_identifier), dataset_identifier)

    async def require_coverage(self, dataset_identifier: str) -> CoverageDataset:
        """Read the record of a coverage, refusing a dataset of another item type."""
        return require_coverage_record(await self.get_dataset(dataset_identifier), dataset_identifier)

    async def require_collection(self, dataset_identifier: str) -> FeatureDataset:
        """Read the record of a vector collection, refusing a dataset of another item type."""
        return require_collection_record(await self.get_dataset(dataset_identifier), dataset_identifier)

    async def delete_dataset(self, dataset_identifier: str) -> Dataset:
        """Read the record of a dataset and delete it through the engine its item type names.

        The record is read raw rather than through ``get_dataset``: a deletion that did not finish
        leaves a record marked as deleting, and deleting again is how that deletion is completed.
        """
        record = await self._catalog.require(dataset_identifier)
        return await self._runner.run(
            lambda: self._service.delete_record(record),
            description=f"deleting dataset {dataset_identifier!r}",
        )
