"""Datasets router listing, reading and deleting catalog records of either item type."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from ocs_storage_exploration.api.dependencies import AsyncStorageServiceDependency
from ocs_storage_exploration.api.schemas import DatasetListResponse
from ocs_storage_exploration.storage.schemas import Dataset, ItemType

router = APIRouter(prefix="/api/v1", tags=["datasets"])

# Every route is an async def awaiting AsyncStorageService: the blocking engine calls run on bounded
# worker threads and the event loop stays free. See docs/architecture.md for the threading model.

ItemTypeQuery = Annotated[ItemType | None, Query(alias="item_type", description="Keep only coverages or features")]


@router.get("/datasets", summary="List dataset records")
async def list_datasets(storage: AsyncStorageServiceDependency, item_type: ItemTypeQuery = None) -> DatasetListResponse:
    """List every dataset record, optionally filtered by item type."""
    return DatasetListResponse(items=await storage.list_datasets(item_type))


@router.get("/datasets/{dataset_identifier}", summary="Read one dataset record")
async def read_dataset(dataset_identifier: str, storage: AsyncStorageServiceDependency) -> Dataset:
    """Read the catalog record that makes one dataset exist."""
    return await storage.get_dataset(dataset_identifier)


@router.delete(
    "/datasets/{dataset_identifier}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one dataset",
)
async def delete_dataset(dataset_identifier: str, storage: AsyncStorageServiceDependency) -> None:
    """Delete a dataset through the engine its item type names."""
    await storage.delete_dataset(dataset_identifier)
