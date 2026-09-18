"""Vector router writing GeoJSON collections, reading features and publishing versions."""

from __future__ import annotations

from functools import partial
from typing import Annotated, Final

from fastapi import APIRouter, Query, status

from ocs_storage_exploration.api.dependencies import AsyncStorageServiceDependency, SettingsDependency
from ocs_storage_exploration.api.parameters import (
    BoundingBoxCrsQuery,
    BoundingBoxQuery,
    ColumnsQuery,
    WhereQuery,
    parse_bbox,
    parse_columns,
    parse_crs,
)
from ocs_storage_exploration.api.schemas import (
    CreateVectorRequest,
    FeatureCollectionResponse,
    IngestVectorRequest,
    PublishRequest,
)
from ocs_storage_exploration.storage.schemas import PublicationResult, VectorWriteResult
from ocs_storage_exploration.storage.vector import DEFAULT_CRS, parse_where

router = APIRouter(prefix="/api/v1/vector", tags=["vector"])

# Every route is an async def awaiting AsyncStorageService: the blocking engine calls run on bounded
# worker threads and the event loop stays free. See docs/architecture.md for the threading model.

MAXIMUM_FEATURE_LIMIT: Final[int] = 100_000

FeatureLimitQuery = Annotated[
    int | None, Query(ge=1, le=MAXIMUM_FEATURE_LIMIT, description="How many features to keep")
]
VersionQuery = Annotated[int | None, Query(ge=1, description="Version to read instead of the published one")]


@router.post("/{dataset_identifier}", status_code=status.HTTP_201_CREATED, summary="Write a vector collection")
async def create_vector(
    dataset_identifier: str,
    request: CreateVectorRequest,
    storage: AsyncStorageServiceDependency,
) -> VectorWriteResult:
    """Write a GeoJSON FeatureCollection as the next version of a vector collection."""
    return await storage.vector.write_geojson(
        dataset_identifier,
        request.feature_collection,
        identifier_property=request.identifier_property,
        crs=request.crs,
        title=request.title,
        license=request.license,
        attribution=request.attribution,
        selectable_columns=request.selectable_columns,
        publish=request.publish,
    )


@router.post(
    "/{dataset_identifier}/ingest",
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a local vector file into a collection",
)
async def ingest_vector(
    dataset_identifier: str,
    request: IngestVectorRequest,
    storage: AsyncStorageServiceDependency,
    settings: SettingsDependency,
) -> VectorWriteResult:
    """Read one local GeoJSON or GeoParquet file below the ingest roots as the next version of a collection."""
    # The plan is handed over unresolved: resolving its path touches the filesystem, so it belongs on
    # the worker thread with the write rather than on the event loop in front of it.
    return await storage.vector.ingest(dataset_identifier, partial(request.to_plan, settings))


@router.get("/{dataset_identifier}/features", summary="Read features of a vector collection")
async def read_features(
    dataset_identifier: str,
    storage: AsyncStorageServiceDependency,
    bbox: BoundingBoxQuery = None,
    bbox_crs: BoundingBoxCrsQuery = None,
    where: WhereQuery = None,
    columns: ColumnsQuery = None,
    limit: FeatureLimitQuery = None,
    version: VersionQuery = None,
) -> FeatureCollectionResponse:
    """Read features as GeoJSON, keeping the coordinates in the frame the collection was written in."""
    selected = parse_columns(columns)
    # The GeoJSON conversion is as blocking as the read, so it runs on the same worker thread rather
    # than on the event loop: a fifty thousand feature answer would otherwise stall every other request.
    return await storage.vector.read_as(
        dataset_identifier,
        partial(FeatureCollectionResponse.from_handle, limit=limit),
        bbox=parse_bbox(bbox),
        bbox_crs=parse_crs(bbox_crs, parameter="bbox-crs") or DEFAULT_CRS,
        where=parse_where(where or []),
        columns=selected or None,
        limit=limit,
        version=version,
    )


@router.post("/{dataset_identifier}/publish", summary="Publish a collection version")
async def publish_vector(
    dataset_identifier: str,
    storage: AsyncStorageServiceDependency,
    request: PublishRequest | None = None,
) -> PublicationResult:
    """Move the published pointer onto a version, which is also how a rollback is spelled."""
    selection = request if request is not None else PublishRequest()
    return await storage.vector.publish(dataset_identifier, version=selection.collection_version())
