"""FastAPI application factory for the storage exploration service."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ocs_storage_exploration import __version__
from ocs_storage_exploration.api import backends, datasets, health, raster, stac, vector
from ocs_storage_exploration.settings import Settings, get_settings
from ocs_storage_exploration.storage.errors import StorageError
from ocs_storage_exploration.storage.service import StorageService
from ocs_storage_exploration.storage.service_async import AsyncStorageService

APPLICATION_TITLE = "OCS storage exploration"
APPLICATION_DESCRIPTION = "Prototype of a unified raster and vector storage abstraction for the Open Climate Service"


async def handle_storage_error(request: Request, exception: Exception) -> JSONResponse:
    """Map a storage error onto the status code it declares."""
    if not isinstance(exception, StorageError):
        raise exception
    return JSONResponse(
        status_code=exception.status_code,
        content={"error": type(exception).__name__, "detail": exception.message},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application, wiring the storage service into the lifespan."""
    resolved_settings = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None]:
        """Build the storage service and its awaitable facade for the lifetime of the application."""
        service = StorageService.from_settings(resolved_settings)
        application.state.storage = service
        # Every route awaits the async facade; the sync service stays on the state because the STAC
        # projection takes one, and because the facade wraps it rather than replacing it.
        awaitable = AsyncStorageService(service)
        application.state.storage_async = awaitable
        # The backend and the catalog stay on the state so a dependency that needs one handle
        # does not have to reach through the service.
        application.state.backend = service.backend
        application.state.catalog = service.catalog
        try:
            yield
        finally:
            # A timed-out call keeps its worker thread and its limiter token; shutdown waits for them
            # rather than tearing the loop down while a write is still touching the object store.
            await awaitable.aclose()

    application = FastAPI(
        title=APPLICATION_TITLE,
        description=APPLICATION_DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.add_exception_handler(StorageError, handle_storage_error)
    application.include_router(health.router)
    application.include_router(backends.router)
    application.include_router(datasets.router)
    application.include_router(raster.router)
    application.include_router(vector.router)
    application.include_router(stac.router)
    return application
