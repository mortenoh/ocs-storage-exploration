"""Projection of catalog records onto STAC Collections and the catalog document that lists them."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, assert_never

import pystac
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError
from pystac.extensions.datacube import SCHEMA_URI as DATACUBE_EXTENSION
from pystac.extensions.datacube import DatacubeExtension, Dimension, Variable
from pystac.extensions.table import SCHEMA_URI as TABLE_EXTENSION
from pystac.extensions.table import Column, TableExtension
from pystac.utils import datetime_to_str

from ocs_storage_exploration.storage.addresses import StorageAddress
from ocs_storage_exploration.storage.keys import VECTOR_DATA_NAME, format_vector_version
from ocs_storage_exploration.storage.models import (
    BoundingBox,
    CoverageDataset,
    Dataset,
    FeatureDataset,
    TemporalExtent,
)
from ocs_storage_exploration.storage.raster.grid import PROJECTION_CODE_ATTRIBUTE
from ocs_storage_exploration.storage.raster.repository import PUBLISHED_BRANCH, RasterRepository, VersionSelector
from ocs_storage_exploration.storage.vector.collection import VectorCollectionStore, VectorTableSchema

if TYPE_CHECKING:
    from ocs_storage_exploration.storage.service import StorageService

STAC_VERSION: Final[str] = "1.1.0"
STAC_PATH: Final[str] = "/stac"
CATALOG_IDENTIFIER: Final[str] = "ocs-storage-exploration"
CATALOG_TITLE: Final[str] = "OCS storage exploration"
CATALOG_DESCRIPTION: Final[str] = "Coverages and feature collections held by the storage exploration service"
DEFAULT_LICENSE: Final[str] = "other"
WGS84: Final[str] = "EPSG:4326"

# OCS advertises a Zarr v3 store with this exact string, and stac-js matches it as a literal rather
# than by parsing the parameters, so the spelling is copied byte for byte. There is no registered
# media type for an Icechunk repository, so the repository is advertised as the Zarr store it holds
# and the branch to open is named in a separate field.
ZARR_V3_MEDIA_TYPE: Final[str] = "application/vnd.zarr; version=3"
PARQUET_MEDIA_TYPE: Final[str] = pystac.MediaType.PARQUET
JSON_MEDIA_TYPE: Final[str] = pystac.MediaType.JSON

ICECHUNK_BRANCH_FIELD: Final[str] = "icechunk:branch"
ITEM_TYPE_FIELD: Final[str] = "ocs:item_type"
SNAPSHOT_FIELD: Final[str] = "ocs:snapshot_identifier"
VERSION_FIELD: Final[str] = "ocs:version"

CONFORMANCE_CLASSES: Final[tuple[str, ...]] = (
    "https://api.stacspec.org/v1.0.0/core",
    "https://api.stacspec.org/v1.0.0/collections",
)

__all__ = [
    "CATALOG_DESCRIPTION",
    "CATALOG_IDENTIFIER",
    "CATALOG_TITLE",
    "CONFORMANCE_CLASSES",
    "DATACUBE_EXTENSION",
    "DEFAULT_LICENSE",
    "ICECHUNK_BRANCH_FIELD",
    "ITEM_TYPE_FIELD",
    "JSON_MEDIA_TYPE",
    "PARQUET_MEDIA_TYPE",
    "SNAPSHOT_FIELD",
    "STAC_PATH",
    "STAC_VERSION",
    "TABLE_EXTENSION",
    "VERSION_FIELD",
    "ZARR_V3_MEDIA_TYPE",
    "build_catalog",
    "build_collection",
    "catalog_href",
    "collection_href",
    "collections_href",
    "reference_system",
    "wgs84_bounds",
]


def catalog_href(base_url: str) -> str:
    """Return the absolute URL of the STAC landing page."""
    return f"{base_url}{STAC_PATH}"


def collections_href(base_url: str) -> str:
    """Return the absolute URL of the STAC collections listing."""
    return f"{catalog_href(base_url)}/collections"


def collection_href(base_url: str, dataset_identifier: str) -> str:
    """Return the absolute URL of one STAC collection."""
    return f"{collections_href(base_url)}/{dataset_identifier}"


def reference_system(crs: str) -> int | str:
    """Return the EPSG code of a coordinate reference system, or its WKT2 when it has no code."""
    try:
        parsed = CRS.from_user_input(crs)
    except CRSError:
        return crs
    code = parsed.to_epsg()
    if code is not None:
        return int(code)
    return str(parsed.to_wkt())


def wgs84_bounds(bbox: BoundingBox, crs: str) -> tuple[float, float, float, float]:
    """Return an envelope in WGS84, reprojecting it when it was recorded in another frame."""
    try:
        source = CRS.from_user_input(crs)
    except CRSError:
        return bbox.as_tuple()
    if source == CRS.from_user_input(WGS84):
        return bbox.as_tuple()
    transformer = Transformer.from_crs(source, CRS.from_user_input(WGS84), always_xy=True)
    west, south, east, north = transformer.transform_bounds(*bbox.as_tuple())
    return (float(west), float(south), float(east), float(north))


def build_catalog(records: Sequence[Dataset], *, base_url: str) -> dict[str, Any]:
    """Build the STAC landing page, with the collections endpoint and one child link per record."""
    catalog = pystac.Catalog(id=CATALOG_IDENTIFIER, title=CATALOG_TITLE, description=CATALOG_DESCRIPTION)
    # pystac keeps a root link pointing at the in-memory object; every link here is an absolute URL
    # of this service instead, so the generated ones are dropped first.
    catalog.clear_links()
    root = catalog_href(base_url)
    catalog.add_link(pystac.Link(rel="self", target=root, media_type=JSON_MEDIA_TYPE))
    catalog.add_link(pystac.Link(rel="root", target=root, media_type=JSON_MEDIA_TYPE))
    catalog.add_link(
        pystac.Link(rel="data", target=collections_href(base_url), media_type=JSON_MEDIA_TYPE, title="Collections"),
    )
    for record in records:
        catalog.add_link(
            pystac.Link(
                rel="child",
                target=collection_href(base_url, record.dataset_identifier),
                media_type=JSON_MEDIA_TYPE,
                title=record.title,
            ),
        )
    payload = catalog.to_dict(include_self_link=True, transform_hrefs=False)
    payload["stac_version"] = STAC_VERSION
    payload["conformsTo"] = list(CONFORMANCE_CLASSES)
    return payload


def build_collection(record: Dataset, *, base_url: str, service: StorageService) -> dict[str, Any]:
    """Project one catalog record onto the STAC Collection its item type calls for."""
    match record:
        case CoverageDataset():
            return _build_coverage_collection(record, base_url=base_url, repository=service.raster)
        case FeatureDataset():
            return _build_feature_collection(record, base_url=base_url, store=service.vector)
        case _:  # pragma: no cover - the dataset union has no other member
            assert_never(record)


def _build_coverage_collection(
    record: CoverageDataset,
    *,
    base_url: str,
    repository: RasterRepository,
) -> dict[str, Any]:
    """Build the datacube Collection of a coverage, with its Icechunk repository as the data asset."""
    grid = record.grid
    envelope = record.bbox if record.bbox is not None else grid.bbox
    store_crs = _store_projection_code(record, repository)
    collection = pystac.Collection(
        id=record.dataset_identifier,
        title=record.title,
        description=_coverage_description(record),
        extent=pystac.Extent(
            spatial=pystac.SpatialExtent([list(wgs84_bounds(envelope, store_crs))]),
            temporal=_temporal_extent(record.temporal),
        ),
        license=record.license or DEFAULT_LICENSE,
        providers=_providers(record),
    )
    datacube = DatacubeExtension.ext(collection, add_if_missing=True)
    datacube.dimensions = _cube_dimensions(record, store_crs)
    datacube.variables = _cube_variables(record)
    collection.extra_fields[ITEM_TYPE_FIELD] = record.item_type.value
    if record.publication.snapshot_identifier is not None:
        collection.extra_fields[SNAPSHOT_FIELD] = record.publication.snapshot_identifier
    collection.add_asset(
        "icechunk",
        pystac.Asset(
            href=record.address,
            title="Icechunk repository",
            media_type=ZARR_V3_MEDIA_TYPE,
            roles=["data"],
            extra_fields={ICECHUNK_BRANCH_FIELD: PUBLISHED_BRANCH},
        ),
    )
    collection.add_asset(
        "api",
        pystac.Asset(
            href=f"{base_url}/api/v1/raster/{record.dataset_identifier}/query",
            title="Raster query endpoint",
            media_type=JSON_MEDIA_TYPE,
            roles=["metadata"],
        ),
    )
    return _finish(collection, base_url=base_url, dataset_identifier=record.dataset_identifier)


def _build_feature_collection(
    record: FeatureDataset,
    *,
    base_url: str,
    store: VectorCollectionStore,
) -> dict[str, Any]:
    """Build the table Collection of a vector collection, with the published GeoParquet as the data asset."""
    collection = pystac.Collection(
        id=record.dataset_identifier,
        title=record.title,
        description=_feature_description(record),
        extent=pystac.Extent(
            spatial=pystac.SpatialExtent([list(_feature_bounds(record))]),
            temporal=_temporal_extent(None),
        ),
        license=record.license or DEFAULT_LICENSE,
        providers=_providers(record),
    )
    advertised = _advertised_schema(record, store)
    table = TableExtension.ext(collection, add_if_missing=True)
    table.primary_geometry = record.features.primary_geometry
    collection.extra_fields[ITEM_TYPE_FIELD] = record.item_type.value
    if advertised is None:
        # Nothing readable to describe, so the record's own count is all there is to say.
        table.row_count = record.features.feature_count
        table.columns = []
    else:
        # The row count comes from the advertised Parquet rather than the record, whose feature
        # detail tracks the newest write and would overstate a collection rolled back to an
        # older version.
        table.row_count = advertised.row_count
        table.columns = [Column({"name": name, "type": kind}) for name, kind in advertised.column_types.items()]
        collection.extra_fields[VERSION_FIELD] = advertised.version
        collection.add_asset(
            "data",
            pystac.Asset(
                href=_parquet_href(record, advertised.version),
                title="GeoParquet data",
                media_type=PARQUET_MEDIA_TYPE,
                roles=["data"],
            ),
        )
    collection.add_asset(
        "api",
        pystac.Asset(
            href=f"{base_url}/api/v1/vector/{record.dataset_identifier}/features",
            title="Feature query endpoint",
            media_type=JSON_MEDIA_TYPE,
            roles=["metadata"],
        ),
    )
    return _finish(collection, base_url=base_url, dataset_identifier=record.dataset_identifier)


def _finish(collection: pystac.Collection, *, base_url: str, dataset_identifier: str) -> dict[str, Any]:
    """Attach the self, root and parent links of one collection and render it as a dictionary."""
    collection.clear_links()
    collection.add_link(
        pystac.Link(rel="self", target=collection_href(base_url, dataset_identifier), media_type=JSON_MEDIA_TYPE),
    )
    collection.add_link(pystac.Link(rel="root", target=catalog_href(base_url), media_type=JSON_MEDIA_TYPE))
    collection.add_link(pystac.Link(rel="parent", target=catalog_href(base_url), media_type=JSON_MEDIA_TYPE))
    payload = collection.to_dict(include_self_link=True, transform_hrefs=False)
    payload["type"] = "Collection"
    payload["stac_version"] = STAC_VERSION
    return payload


def _temporal_extent(temporal: TemporalExtent | None) -> pystac.TemporalExtent:
    """Return the STAC temporal extent of a record, left open at both ends when it has no time axis."""
    interval: list[datetime | None] = [None, None] if temporal is None else [temporal.start, temporal.end]
    return pystac.TemporalExtent([interval])


def _providers(record: Dataset) -> list[pystac.Provider] | None:
    """Return the provider carrying the attribution of a record, or None when it declares none."""
    if record.attribution is None:
        return None
    # Attribution is a licence condition under CC-BY rather than a courtesy, and `providers` is the
    # only collection-level field STAC defines for naming the party that has to be credited.
    return [
        pystac.Provider(
            name=record.attribution,
            roles=[pystac.ProviderRole.PRODUCER, pystac.ProviderRole.LICENSOR],
        ),
    ]


def _coverage_description(record: CoverageDataset) -> str:
    """Return the generated description of a coverage collection."""
    rows, columns = record.grid.shape
    variables = ", ".join(record.variables) if record.variables else "no variables"
    return (
        f"Icechunk-backed GeoZarr coverage on a {rows} by {columns} grid in {record.grid.crs}, "
        f"holding {record.timestep_count} timesteps of {variables}."
    )


def _feature_description(record: FeatureDataset) -> str:
    """Return the generated description of a feature collection."""
    geometry_types = ", ".join(record.features.geometry_types) if record.features.geometry_types else "no geometries"
    return (
        f"GeoParquet feature collection of {record.features.feature_count} features in {record.crs}, "
        f"identified by {record.features.identifier_property} and holding {geometry_types}."
    )


def _cube_dimensions(record: CoverageDataset, store_crs: str) -> dict[str, Dimension]:
    """Build the datacube dimensions of a coverage from its grid and its temporal extent."""
    grid = record.grid
    envelope = record.bbox if record.bbox is not None else grid.bbox
    system = reference_system(store_crs)
    dimensions = {
        grid.x_dimension: Dimension.from_dict(
            {
                "type": "spatial",
                "axis": "x",
                "extent": [envelope.minimum_x, envelope.maximum_x],
                "reference_system": system,
            },
        ),
        grid.y_dimension: Dimension.from_dict(
            {
                "type": "spatial",
                "axis": "y",
                "extent": [envelope.minimum_y, envelope.maximum_y],
                "reference_system": system,
            },
        ),
    }
    dimensions[grid.time_dimension] = Dimension.from_dict({"type": "temporal", "extent": _temporal_pair(record)})
    return dimensions


def _temporal_pair(record: CoverageDataset) -> list[str | None]:
    """Return the open or closed temporal extent of a coverage as the pair of strings STAC wants."""
    if record.temporal is None:
        return [None, None]
    return [datetime_to_str(record.temporal.start), datetime_to_str(record.temporal.end)]


def _cube_variables(record: CoverageDataset) -> dict[str, Variable]:
    """Build one datacube variable per data variable of a coverage, over its three grid dimensions."""
    grid = record.grid
    dimensions = [grid.time_dimension, grid.y_dimension, grid.x_dimension]
    return {name: Variable.from_dict({"dimensions": list(dimensions), "type": "data"}) for name in record.variables}


def _feature_bounds(record: FeatureDataset) -> tuple[float, float, float, float]:
    """Return the WGS84 envelope of a feature collection, falling back to the whole world."""
    if record.bbox is None:
        return (-180.0, -90.0, 180.0, 90.0)
    return wgs84_bounds(record.bbox, record.crs)


def _advertised_schema(record: FeatureDataset, store: VectorCollectionStore) -> VectorTableSchema | None:
    """Describe the version a feature collection advertises: the published one, else the newest written."""
    try:
        return store.table_schema(record.dataset_identifier)
    except Exception:
        # A collection whose pointer, prefix or Parquet cannot be read still has a record worth
        # advertising; it loses the data asset and its columns rather than taking the catalog down.
        return None


def _parquet_href(record: FeatureDataset, version: int) -> str:
    """Return the URI of the GeoParquet object of one version of a feature collection."""
    address = StorageAddress.from_uri(record.address)
    return address.joined("versions", format_vector_version(version), VECTOR_DATA_NAME).as_uri()


def _store_projection_code(record: CoverageDataset, repository: RasterRepository) -> str:
    """Return the projection code the store itself records, falling back to the grid of the record."""
    selector = VersionSelector.PUBLISHED if record.publication.published else VersionSelector.DRAFT
    try:
        attributes = repository.root_attributes(record.dataset_identifier, version=selector)
    except Exception:
        # The record already knows the grid it was written on, so a locked, missing or corrupt store
        # degrades to that rather than failing the projection.
        return record.grid.crs
    code = attributes.get(PROJECTION_CODE_ATTRIBUTE)
    if isinstance(code, str) and code:
        return code
    return record.grid.crs
