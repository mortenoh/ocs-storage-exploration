"""Backends router describing the active backend and the other registered schemes."""

from __future__ import annotations

from fastapi import APIRouter

from ocs_storage_exploration.api.dependencies import AsyncStorageServiceDependency
from ocs_storage_exploration.api.schemas import BackendListResponse

router = APIRouter(prefix="/api/v1", tags=["backends"])

# Every route is an async def awaiting AsyncStorageService: the blocking engine calls run on bounded
# worker threads and the event loop stays free. See docs/architecture.md for the threading model.


@router.get("/backends", summary="List the storage backends")
async def list_backends(storage: AsyncStorageServiceDependency) -> BackendListResponse:
    """List the active backend first, then every other registered scheme as inactive."""
    return BackendListResponse(items=await storage.describe_backends())
