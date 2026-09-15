"""Backends router describing the active backend and the other registered schemes."""

from __future__ import annotations

from fastapi import APIRouter

from ocs_storage_exploration.api.dependencies import StorageServiceDependency
from ocs_storage_exploration.api.schemas import BackendListResponse

router = APIRouter(prefix="/api/v1", tags=["backends"])


@router.get("/backends", summary="List the storage backends")
async def list_backends(storage: StorageServiceDependency) -> BackendListResponse:
    """List the active backend first, then every other registered scheme as inactive."""
    return BackendListResponse(items=storage.describe_backends())
