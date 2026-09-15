"""Vector router writing GeoJSON collections, reading features and publishing versions."""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Query, status

from ocs_storage_exploration.api.dependencies import StorageServiceDependency
from ocs_storage_exploration.api.parameters import (
    BoundingBoxCrsQuery,
    BoundingBoxQuery,
    ColumnsQuery,
    WhereQuery,
    parse_bbox,
    parse_columns,
)
from ocs_storage_exploration.api.schemas import (
    CreateVectorRequest,
    FeatureCollectionResponse,
    PublishRequest,
)
from ocs_storage_exploration.storage.models import PublicationResult, VectorWriteResult
from ocs_storage_exploration.storage.vector import DEFAULT_CRS, parse_where

router = APIRouter(prefix="/api/v1/vector", tags=["vector"])

MAXIMUM_FEATURE_LIMIT: Final[int] = 100_000

FeatureLimitQuery = Annotated[
    int | None, Query(ge=1, le=MAXIMUM_FEATURE_LIMIT, description="How many features to keep")
]
VersionQuery = Annotated[int | None, Query(ge=1, description="Version to read instead of the published one")]


@router.post("/{dataset_identifier}", status_code=status.HTTP_201_CREATED, summary="Write a vector collection")
async def create_vector(
    dataset_identifier: str,
    request: CreateVectorRequest,
    storage: StorageServiceDependency,
) -> VectorWriteResult:
    """Write a GeoJSON FeatureCollection as the next version of a vector collection."""
    return storage.vector.write_geojson(
        dataset_identifier,
        request.feature_collection,
        identifier_property=request.identifier_property,
        crs=request.crs,
        title=request.title,
        selectable_columns=request.selectable_columns,
        publish=request.publish,
    )


@router.get("/{dataset_identifier}/features", summary="Read features of a vector collection")
async def read_features(
    dataset_identifier: str,
    storage: StorageServiceDependency,
    bbox: BoundingBoxQuery = None,
    bbox_crs: BoundingBoxCrsQuery = None,
    where: WhereQuery = None,
    columns: ColumnsQuery = None,
    limit: FeatureLimitQuery = None,
    version: VersionQuery = None,
) -> FeatureCollectionResponse:
    """Read features as GeoJSON, keeping the coordinates in the frame the collection was written in."""
    selected = parse_columns(columns)
    handle = storage.vector.read(
        dataset_identifier,
        bbox=parse_bbox(bbox),
        bbox_crs=bbox_crs or DEFAULT_CRS,
        where=parse_where(where or []),
        columns=selected or None,
        limit=limit,
        version=version,
    )
    return FeatureCollectionResponse.from_handle(handle, limit=limit)


@router.post("/{dataset_identifier}/publish", summary="Publish a collection version")
async def publish_vector(
    dataset_identifier: str,
    storage: StorageServiceDependency,
    request: PublishRequest | None = None,
) -> PublicationResult:
    """Move the published pointer onto a version, which is also how a rollback is spelled."""
    selection = request if request is not None else PublishRequest()
    return storage.vector.publish(dataset_identifier, version=selection.collection_version())
