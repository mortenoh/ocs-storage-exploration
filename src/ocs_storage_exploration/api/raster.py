"""Raster router creating, appending to, querying and publishing synthetic coverages."""

from __future__ import annotations

from datetime import datetime
from functools import partial
from typing import Annotated, Final

from fastapi import APIRouter, Query, status

from ocs_storage_exploration.api.dependencies import AsyncStorageServiceDependency, SettingsDependency
from ocs_storage_exploration.api.parameters import BoundingBoxQuery, parse_bbox
from ocs_storage_exploration.api.schemas import (
    AppendRasterRequest,
    CreateRasterRequest,
    IngestRasterRequest,
    PublishRequest,
    RasterVersionListResponse,
    assert_cube_size,
)
from ocs_storage_exploration.storage.raster import VersionSelector
from ocs_storage_exploration.storage.schemas import (
    PublicationResult,
    RasterIngestResult,
    RasterQuerySummary,
    RasterWriteResult,
)

router = APIRouter(prefix="/api/v1/raster", tags=["raster"])

# Every route is an async def awaiting AsyncStorageService: the blocking engine calls run on bounded
# worker threads and the event loop stays free. See docs/architecture.md for the threading model.

DEFAULT_VERSION_LIMIT: Final[int] = 100
MAXIMUM_VERSION_LIMIT: Final[int] = 1000

VersionLimitQuery = Annotated[int, Query(ge=1, le=MAXIMUM_VERSION_LIMIT, description="How many snapshots to report")]


@router.post("/{dataset_identifier}", status_code=status.HTTP_201_CREATED, summary="Create a synthetic coverage")
async def create_raster(
    dataset_identifier: str,
    request: CreateRasterRequest,
    storage: AsyncStorageServiceDependency,
    settings: SettingsDependency,
) -> RasterWriteResult:
    """Generate a synthetic cube, write it as a new coverage and publish it when the request asks for it."""
    # The guard belongs to the application that was asked, so it runs here rather than in a schema
    # validator, and it runs before anything allocates the cells it is counting.
    assert_cube_size(
        "requested cube",
        request.shape,
        request.timestep_count,
        max_cube_cells=settings.max_cube_cells,
    )
    grid = request.to_grid()
    result = await storage.raster.create(
        dataset_identifier,
        grid,
        partial(request.to_cube, grid),
        title=request.title,
        license=request.license,
        attribution=request.attribution,
        overwrite=request.overwrite,
    )
    if not request.publish:
        return result
    await storage.raster.publish(dataset_identifier, snapshot_identifier=result.snapshot_identifier)
    return result.model_copy(update={"published": True})


@router.post("/{dataset_identifier}/append", summary="Append timesteps to a coverage")
async def append_raster(
    dataset_identifier: str,
    request: AppendRasterRequest,
    storage: AsyncStorageServiceDependency,
    settings: SettingsDependency,
) -> RasterWriteResult:
    """Continue the time axis of a coverage with more synthetic timesteps."""
    record = await storage.require_coverage(dataset_identifier)
    assert_cube_size(
        f"append to {dataset_identifier!r}",
        record.grid.shape,
        request.timestep_count,
        max_cube_cells=settings.max_cube_cells,
    )
    result = await storage.raster.append(dataset_identifier, partial(request.to_cube, record))
    if not request.publish:
        return result
    await storage.raster.publish(dataset_identifier, snapshot_identifier=result.snapshot_identifier)
    return result.model_copy(update={"published": True})


@router.post(
    "/{dataset_identifier}/ingest",
    status_code=status.HTTP_201_CREATED,
    summary="Ingest local raster files into a coverage",
)
async def ingest_raster(
    dataset_identifier: str,
    request: IngestRasterRequest,
    storage: AsyncStorageServiceDependency,
    settings: SettingsDependency,
) -> RasterIngestResult:
    """Read local GeoTIFF, COG, NetCDF or Zarr files below the ingest roots and write them as one coverage."""
    return await storage.raster.ingest(dataset_identifier, request.to_plan(settings))


@router.get("/{dataset_identifier}/query", summary="Summarise a window of a coverage")
async def query_raster(
    dataset_identifier: str,
    storage: AsyncStorageServiceDependency,
    bbox: BoundingBoxQuery = None,
    start: datetime | None = None,
    end: datetime | None = None,
    variable: str | None = None,
    version: VersionSelector = VersionSelector.PUBLISHED,
    snapshot_identifier: str | None = None,
) -> RasterQuerySummary:
    """Summarise one variable of a coverage over a spatial and temporal window."""
    return await storage.raster.query(
        dataset_identifier,
        variable=variable,
        bbox=parse_bbox(bbox),
        start=start,
        end=end,
        version=version,
        snapshot_identifier=snapshot_identifier,
    )


@router.post("/{dataset_identifier}/publish", summary="Publish a coverage snapshot")
async def publish_raster(
    dataset_identifier: str,
    storage: AsyncStorageServiceDependency,
    request: PublishRequest | None = None,
) -> PublicationResult:
    """Move the published branch onto a snapshot, which is also how a rollback is spelled."""
    selection = request if request is not None else PublishRequest()
    return await storage.raster.publish(dataset_identifier, snapshot_identifier=selection.coverage_snapshot())


@router.get("/{dataset_identifier}/versions", summary="List the snapshots of a coverage")
async def list_raster_versions(
    dataset_identifier: str,
    storage: AsyncStorageServiceDependency,
    limit: VersionLimitQuery = DEFAULT_VERSION_LIMIT,
) -> RasterVersionListResponse:
    """List the snapshots of a coverage newest first, marking the published one."""
    return RasterVersionListResponse(items=await storage.raster.versions(dataset_identifier, limit=limit))
