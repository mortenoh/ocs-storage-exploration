"""STAC router serving the landing page, the collections listing and one collection per dataset."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from ocs_storage_exploration.api.dependencies import AsyncStorageServiceDependency
from ocs_storage_exploration.storage.errors import DatasetNotFoundError
from ocs_storage_exploration.storage.schemas import Dataset
from ocs_storage_exploration.storage.stac import (
    JSON_MEDIA_TYPE,
    build_catalog,
    build_collection,
    catalog_href,
    collections_href,
)

router = APIRouter(prefix="/stac", tags=["stac"])

# Every route is an async def awaiting AsyncStorageService: the catalog listing is answered natively
# through obstore, and the projection of a collection, which reads a store or a Parquet footer per
# record, runs as one bounded worker-thread call. See docs/architecture.md for the threading model.

PublishedOnlyQuery = Annotated[
    bool,
    Query(alias="published_only", description="Advertise only datasets that have something published"),
]


def base_url_of(request: Request) -> str:
    """Return the absolute base URL of the service, honouring the root path it is mounted under."""
    return str(request.base_url).rstrip("/")


def advertised_datasets(records: Sequence[Dataset], *, published_only: bool) -> list[Dataset]:
    """Keep the dataset records the catalog advertises, dropping drafts unless they were asked for."""
    if not published_only:
        return list(records)
    return [record for record in records if record.publication.published]


@router.get("", summary="Read the STAC landing page")
async def read_landing_page(
    request: Request,
    storage: AsyncStorageServiceDependency,
    published_only: PublishedOnlyQuery = True,
) -> dict[str, Any]:
    """Answer the STAC catalog naming the collections endpoint and every advertised collection."""
    records = advertised_datasets(await storage.list_datasets(), published_only=published_only)
    # The landing page reads no store, so it is built on the event loop rather than on a worker thread.
    return build_catalog(records, base_url=base_url_of(request))


@router.get("/collections", summary="List the STAC collections")
async def list_collections(
    request: Request,
    storage: AsyncStorageServiceDependency,
    published_only: PublishedOnlyQuery = True,
) -> dict[str, Any]:
    """Answer every advertised dataset record projected onto a STAC collection."""
    base_url = base_url_of(request)
    records = advertised_datasets(await storage.list_datasets(), published_only=published_only)
    collections = await storage.run_blocking(
        lambda: [build_collection(record, base_url=base_url, service=storage.service) for record in records],
        description="projecting the STAC collections",
    )
    return {
        "collections": collections,
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
    storage: AsyncStorageServiceDependency,
    published_only: PublishedOnlyQuery = True,
) -> dict[str, Any]:
    """Answer one dataset record as a STAC collection, refusing a draft unless drafts were asked for."""
    record = await storage.get_dataset(dataset_identifier)
    if published_only and not record.publication.published:
        raise DatasetNotFoundError(f"dataset {dataset_identifier!r} has nothing published")
    base_url = base_url_of(request)
    return await storage.run_blocking(
        lambda: build_collection(record, base_url=base_url, service=storage.service),
        description=f"projecting the STAC collection of {dataset_identifier!r}",
    )
