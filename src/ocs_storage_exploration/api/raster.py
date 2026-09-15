"""Raster router creating, appending to, querying and publishing synthetic coverages."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, Query, status

from ocs_storage_exploration.api.dependencies import StorageServiceDependency
from ocs_storage_exploration.api.parameters import BoundingBoxQuery, parse_bbox
from ocs_storage_exploration.api.schemas import (
    AppendRasterRequest,
    CreateRasterRequest,
    PublishRequest,
    RasterVersionListResponse,
)
from ocs_storage_exploration.storage.models import PublicationResult, RasterQuerySummary, RasterWriteResult
from ocs_storage_exploration.storage.raster import VersionSelector

router = APIRouter(prefix="/api/v1/raster", tags=["raster"])

DEFAULT_VERSION_LIMIT: Final[int] = 100
MAXIMUM_VERSION_LIMIT: Final[int] = 1000

VersionLimitQuery = Annotated[int, Query(ge=1, le=MAXIMUM_VERSION_LIMIT, description="How many snapshots to report")]


@router.post("/{dataset_identifier}", status_code=status.HTTP_201_CREATED, summary="Create a synthetic coverage")
async def create_raster(
    dataset_identifier: str,
    request: CreateRasterRequest,
    storage: StorageServiceDependency,
) -> RasterWriteResult:
    """Generate a synthetic cube, write it as a new coverage and publish it when the request asks for it."""
    grid = request.to_grid()
    result = storage.raster.create(
        dataset_identifier,
        grid,
        request.to_cube(grid),
        title=request.title,
        overwrite=request.overwrite,
    )
    if not request.publish:
        return result
    storage.raster.publish(dataset_identifier, snapshot_identifier=result.snapshot_identifier)
    return result.model_copy(update={"published": True})


@router.post("/{dataset_identifier}/append", summary="Append timesteps to a coverage")
async def append_raster(
    dataset_identifier: str,
    request: AppendRasterRequest,
    storage: StorageServiceDependency,
) -> RasterWriteResult:
    """Continue the time axis of a coverage with more synthetic timesteps."""
    record = storage.require_coverage(dataset_identifier)
    result = storage.raster.append(dataset_identifier, request.to_cube(record))
    if not request.publish:
        return result
    storage.raster.publish(dataset_identifier, snapshot_identifier=result.snapshot_identifier)
    return result.model_copy(update={"published": True})


@router.get("/{dataset_identifier}/query", summary="Summarise a window of a coverage")
async def query_raster(
    dataset_identifier: str,
    storage: StorageServiceDependency,
    bbox: BoundingBoxQuery = None,
    start: datetime | None = None,
    end: datetime | None = None,
    variable: str | None = None,
    version: VersionSelector = VersionSelector.PUBLISHED,
    snapshot_identifier: str | None = None,
) -> RasterQuerySummary:
    """Summarise one variable of a coverage over a spatial and temporal window."""
    return storage.raster.query(
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
    storage: StorageServiceDependency,
    request: PublishRequest | None = None,
) -> PublicationResult:
    """Move the published branch onto a snapshot, which is also how a rollback is spelled."""
    selection = request if request is not None else PublishRequest()
    return storage.raster.publish(dataset_identifier, snapshot_identifier=selection.coverage_snapshot())


@router.get("/{dataset_identifier}/versions", summary="List the snapshots of a coverage")
async def list_raster_versions(
    dataset_identifier: str,
    storage: StorageServiceDependency,
    limit: VersionLimitQuery = DEFAULT_VERSION_LIMIT,
) -> RasterVersionListResponse:
    """List the snapshots of a coverage newest first, marking the published one."""
    return RasterVersionListResponse(items=storage.raster.versions(dataset_identifier, limit=limit))
