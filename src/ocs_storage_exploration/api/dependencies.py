"""FastAPI dependencies resolving the storage objects built during the application lifespan."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.protocols import Catalog, StorageBackend
from ocs_storage_exploration.storage.service import StorageService
from ocs_storage_exploration.storage.service_async import AsyncStorageService


def get_settings_from_request(request: Request) -> Settings:
    """Return the settings stored on the application state."""
    settings: Settings = request.app.state.settings
    return settings


def get_storage_service(request: Request) -> StorageService:
    """Return the storage service built during the application lifespan."""
    service: StorageService = request.app.state.storage
    return service


def get_async_storage_service(request: Request) -> AsyncStorageService:
    """Return the awaitable storage facade built during the application lifespan."""
    service: AsyncStorageService = request.app.state.storage_async
    return service


def get_storage_backend(request: Request) -> StorageBackend:
    """Return the storage backend built during the application lifespan."""
    backend: StorageBackend = request.app.state.backend
    return backend


def get_catalog(request: Request) -> Catalog:
    """Return the dataset catalog built during the application lifespan."""
    catalog: Catalog = request.app.state.catalog
    return catalog


SettingsDependency = Annotated[Settings, Depends(get_settings_from_request)]
StorageServiceDependency = Annotated[StorageService, Depends(get_storage_service)]
AsyncStorageServiceDependency = Annotated[AsyncStorageService, Depends(get_async_storage_service)]
StorageBackendDependency = Annotated[StorageBackend, Depends(get_storage_backend)]
CatalogDependency = Annotated[Catalog, Depends(get_catalog)]
