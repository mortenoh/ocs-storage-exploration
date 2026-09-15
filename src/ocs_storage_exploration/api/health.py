"""Health router reporting the service version and the active storage backend."""

from __future__ import annotations

from fastapi import APIRouter

from ocs_storage_exploration import __version__
from ocs_storage_exploration.api.dependencies import StorageBackendDependency
from ocs_storage_exploration.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", summary="Report service health")
async def read_health(backend: StorageBackendDependency) -> HealthResponse:
    """Report that the service is up, with its version and active backend scheme."""
    return HealthResponse(status="ok", version=__version__, backend=backend.scheme)
