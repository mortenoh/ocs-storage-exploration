"""STAC router serving the landing page, the collections listing and one collection per dataset."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from ocs_storage_exploration.api.dependencies import StorageServiceDependency
from ocs_storage_exploration.storage.errors import DatasetNotFoundError
from ocs_storage_exploration.storage.models import Dataset
from ocs_storage_exploration.storage.service import StorageService
from ocs_storage_exploration.storage.stac import (
    JSON_MEDIA_TYPE,
    build_catalog,
    build_collection,
    catalog_href,
    collections_href,
)

router = APIRouter(prefix="/stac", tags=["stac"])

PublishedOnlyQuery = Annotated[
    bool,
    Query(alias="published_only", description="Advertise only datasets that have something published"),
]


def base_url_of(request: Request) -> str:
    """Return the absolute base URL of the service, honouring the root path it is mounted under."""
    return str(request.base_url).rstrip("/")


def advertised_datasets(storage: StorageService, *, published_only: bool) -> list[Dataset]:
    """List the dataset records the catalog advertises, dropping drafts unless they were asked for."""
    records = storage.list_datasets()
    if not published_only:
        return records
    return [record for record in records if record.publication.published]


@router.get("", summary="Read the STAC landing page")
async def read_landing_page(
    request: Request,
    storage: StorageServiceDependency,
    published_only: PublishedOnlyQuery = True,
) -> dict[str, Any]:
    """Answer the STAC catalog naming the collections endpoint and every advertised collection."""
    records = advertised_datasets(storage, published_only=published_only)
    return build_catalog(records, base_url=base_url_of(request))


@router.get("/collections", summary="List the STAC collections")
async def list_collections(
    request: Request,
    storage: StorageServiceDependency,
    published_only: PublishedOnlyQuery = True,
) -> dict[str, Any]:
    """Answer every advertised dataset record projected onto a STAC collection."""
    base_url = base_url_of(request)
    records = advertised_datasets(storage, published_only=published_only)
    return {
        "collections": [build_collection(record, base_url=base_url, service=storage) for record in records],
        "links": [
            {"rel": "self", "href": collections_href(base_url), "type": JSON_MEDIA_TYPE},
            {"rel": "root", "href": catalog_href(base_url), "type": JSON_MEDIA_TYPE},
            {"rel": "parent", "href": catalog_href(base_url), "type": JSON_MEDIA_TYPE},
        ],
    }


@router.get("/collections/{dataset_identifier}", summary="Read one STAC collection")
async def read_collection(
    dataset_identifier: str,
    request: Request,
    storage: StorageServiceDependency,
    published_only: PublishedOnlyQuery = True,
) -> dict[str, Any]:
    """Answer one dataset record as a STAC collection, refusing a draft unless drafts were asked for."""
    record = storage.get_dataset(dataset_identifier)
    if published_only and not record.publication.published:
        raise DatasetNotFoundError(f"dataset {dataset_identifier!r} has nothing published")
    return build_collection(record, base_url=base_url_of(request), service=storage)
