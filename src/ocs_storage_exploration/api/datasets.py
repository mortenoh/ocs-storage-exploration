"""Datasets router listing, reading and deleting catalog records of either item type."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from ocs_storage_exploration.api.dependencies import StorageServiceDependency
from ocs_storage_exploration.api.schemas import DatasetListResponse
from ocs_storage_exploration.storage.models import Dataset, ItemType

router = APIRouter(prefix="/api/v1", tags=["datasets"])

ItemTypeQuery = Annotated[ItemType | None, Query(alias="item_type", description="Keep only coverages or features")]


@router.get("/datasets", summary="List dataset records")
async def list_datasets(storage: StorageServiceDependency, item_type: ItemTypeQuery = None) -> DatasetListResponse:
    """List every dataset record, optionally filtered by item type."""
    return DatasetListResponse(items=storage.list_datasets(item_type))


@router.get("/datasets/{dataset_identifier}", summary="Read one dataset record")
async def read_dataset(dataset_identifier: str, storage: StorageServiceDependency) -> Dataset:
    """Read the catalog record that makes one dataset exist."""
    return storage.get_dataset(dataset_identifier)


@router.delete(
    "/datasets/{dataset_identifier}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one dataset",
)
async def delete_dataset(dataset_identifier: str, storage: StorageServiceDependency) -> None:
    """Delete a dataset through the engine its item type names."""
    storage.delete_dataset(dataset_identifier)
