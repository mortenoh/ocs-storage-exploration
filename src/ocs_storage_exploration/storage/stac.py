"""Projection of catalog records onto STAC Collections and the catalog document that lists them."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
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
from ocs_storage_exploration.storage.raster.repository import (
    PUBLISHED_BRANCH,
    RasterRepository,
    RasterStoreDescription,
    VersionSelector,
)
from ocs_storage_exploration.storage.schemas import (
    BoundingBox,
    CoverageDataset,
    Dataset,
    FeatureDataset,
    TemporalExtent,
    VectorVersionMetadata,
)
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


@dataclass(frozen=True, slots=True)
class _CoverageFacts:
    """Grid, variables and extents of the coverage snapshot a collection advertises."""

    snapshot_identifier: str | None
    variables: tuple[str, ...]
    crs: str
    bbox: BoundingBox
    temporal: TemporalExtent | None
    timestep_count: int
    time_dimension: str
    y_dimension: str
    x_dimension: str
    shape: tuple[int, int]
    license: str | None
    attribution: str | None


@dataclass(frozen=True, slots=True)
class _FeatureFacts:
    """Metadata and Parquet footer of the collection version a collection advertises."""

    version: int | None
    crs: str
    bbox: BoundingBox | None
    feature_count: int
    identifier_property: str
    primary_geometry: str
    geometry_types: tuple[str, ...]
    column_types: dict[str, str]
    license: str | None
    attribution: str | None


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
    facts = _coverage_facts(record, repository)
    collection = pystac.Collection(
        id=record.dataset_identifier,
        title=record.title,
        description=_coverage_description(facts),
        extent=pystac.Extent(
            spatial=pystac.SpatialExtent([list(wgs84_bounds(facts.bbox, facts.crs))]),
            temporal=_temporal_extent(facts.temporal),
        ),
        license=facts.license or DEFAULT_LICENSE,
        providers=_providers(facts.attribution),
    )
    datacube = DatacubeExtension.ext(collection, add_if_missing=True)
    datacube.dimensions = _cube_dimensions(facts)
    datacube.variables = _cube_variables(facts)
    collection.extra_fields[ITEM_TYPE_FIELD] = record.item_type.value
    if facts.snapshot_identifier is not None:
        collection.extra_fields[SNAPSHOT_FIELD] = facts.snapshot_identifier
    collection.add_asset(
        "icechunk",
        pystac.Asset(
            href=repository.backend.address(record.storage_key).as_uri(),
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
    facts = _feature_facts(record, store)
    collection = pystac.Collection(
        id=record.dataset_identifier,
        title=record.title,
        description=_feature_description(facts),
        extent=pystac.Extent(
            spatial=pystac.SpatialExtent([list(_feature_bounds(facts))]),
            temporal=_temporal_extent(None),
        ),
        license=facts.license or DEFAULT_LICENSE,
        providers=_providers(facts.attribution),
    )
    table = TableExtension.ext(collection, add_if_missing=True)
    table.primary_geometry = facts.primary_geometry
    table.row_count = facts.feature_count
    table.columns = [Column({"name": name, "type": kind}) for name, kind in facts.column_types.items()]
    collection.extra_fields[ITEM_TYPE_FIELD] = record.item_type.value
    if facts.version is not None:
        collection.extra_fields[VERSION_FIELD] = facts.version
        collection.add_asset(
            "data",
            pystac.Asset(
                href=_parquet_href(store.backend.address(record.storage_key), facts.version),
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


def _providers(attribution: str | None) -> list[pystac.Provider] | None:
    """Return the provider carrying an attribution, or None when there is none to credit."""
    if attribution is None:
        return None
    # Attribution is a licence condition under CC-BY rather than a courtesy, and `providers` is the
    # only collection-level field STAC defines for naming the party that has to be credited.
    return [
        pystac.Provider(
            name=attribution,
            roles=[pystac.ProviderRole.PRODUCER, pystac.ProviderRole.LICENSOR],
        ),
    ]


def _coverage_facts(record: CoverageDataset, repository: RasterRepository) -> _CoverageFacts:
    """Describe the coverage snapshot a collection advertises, degrading to the record when it cannot be read."""
    description = _describe_store(record, repository)
    if description is None:
        grid = record.grid
        return _CoverageFacts(
            snapshot_identifier=record.publication.snapshot_identifier,
            variables=record.variables,
            crs=grid.crs,
            bbox=record.bbox if record.bbox is not None else grid.bbox,
            temporal=record.temporal,
            timestep_count=record.timestep_count,
            time_dimension=grid.time_dimension,
            y_dimension=grid.y_dimension,
            x_dimension=grid.x_dimension,
            shape=grid.shape,
            license=record.license,
            attribution=record.attribution,
        )
    # Every field describes the snapshot that was opened rather than the newest write, which the record
    # tracks. The licence and the attribution are part of that: they are the terms the advertised bytes
    # were committed under, and a draft written under other terms must not change what a published
    # snapshot promises.
    return _CoverageFacts(
        snapshot_identifier=description.snapshot_identifier,
        variables=description.variables,
        crs=description.crs,
        bbox=description.bbox,
        temporal=_store_temporal(description),
        timestep_count=description.timestep_count,
        time_dimension=description.time_dimension,
        y_dimension=description.y_dimension,
        x_dimension=description.x_dimension,
        shape=description.shape,
        license=description.license,
        attribution=description.attribution,
    )


def _describe_store(record: CoverageDataset, repository: RasterRepository) -> RasterStoreDescription | None:
    """Describe the snapshot a coverage advertises: the published one, or the draft when nothing is published."""
    # The advertised snapshot is the one the reader would get, so a coverage rolled back to an older
    # snapshot advertises that snapshot's time axis rather than everything ever written.
    selector = VersionSelector.PUBLISHED if record.publication.published else VersionSelector.DRAFT
    try:
        return repository.describe(record.dataset_identifier, version=selector)
    except Exception:
        # A store that is locked, missing or unreadable still has a record worth advertising; the
        # collection degrades to what the record holds rather than taking the catalog down.
        return None


def _store_temporal(description: RasterStoreDescription) -> TemporalExtent | None:
    """Return the temporal extent of a described snapshot, or None when it carries no time coordinate."""
    if description.temporal_start is None or description.temporal_end is None:
        return None
    return TemporalExtent(start=description.temporal_start, end=description.temporal_end)


def _feature_facts(record: FeatureDataset, store: VectorCollectionStore) -> _FeatureFacts:
    """Describe the collection version advertised, degrading to the record when no version can be read."""
    advertised = _advertised_version(record, store)
    if advertised is None:
        detail = record.features
        return _FeatureFacts(
            version=None,
            crs=record.crs,
            bbox=record.bbox,
            feature_count=detail.feature_count,
            identifier_property=detail.identifier_property,
            primary_geometry=detail.primary_geometry,
            geometry_types=detail.geometry_types,
            column_types={},
            license=record.license,
            attribution=record.attribution,
        )
    metadata, table = advertised
    # Every field describes the advertised version rather than the newest write, which the record
    # tracks and which would overstate a collection rolled back to an older version. The licence and
    # the attribution are part of that: they are the terms the advertised bytes were written under,
    # and a draft written under other terms must not change what a published version promises.
    return _FeatureFacts(
        version=metadata.version,
        crs=metadata.crs,
        bbox=metadata.bbox,
        feature_count=table.row_count,
        identifier_property=metadata.identifier_property,
        primary_geometry=metadata.primary_geometry,
        geometry_types=metadata.geometry_types,
        column_types=table.column_types,
        license=metadata.license,
        attribution=metadata.attribution,
    )


def _advertised_version(
    record: FeatureDataset,
    store: VectorCollectionStore,
) -> tuple[VectorVersionMetadata, VectorTableSchema] | None:
    """Read the sidecar and the Parquet footer of the version a feature collection advertises."""
    try:
        metadata = _advertised_metadata(record, store)
        if metadata is None:
            return None
        return metadata, store.table_schema(record.dataset_identifier, version=metadata.version)
    except Exception:
        # A collection whose pointer, prefix or Parquet cannot be read still has a record worth
        # advertising; it loses the data asset and its columns rather than taking the catalog down.
        return None


def _advertised_metadata(record: FeatureDataset, store: VectorCollectionStore) -> VectorVersionMetadata | None:
    """Return the sidecar of the published version, or of the newest written one for a draft."""
    if record.publication.published:
        return store.published_metadata(record.dataset_identifier)
    written = store.versions(record.dataset_identifier)
    if not written:
        return None
    return store.version_metadata(record.dataset_identifier, written[-1])


def _coverage_description(facts: _CoverageFacts) -> str:
    """Return the generated description of a coverage collection."""
    rows, columns = facts.shape
    variables = ", ".join(facts.variables) if facts.variables else "no variables"
    return (
        f"Icechunk-backed GeoZarr coverage on a {rows} by {columns} grid in {facts.crs}, "
        f"holding {facts.timestep_count} timesteps of {variables}."
    )


def _feature_description(facts: _FeatureFacts) -> str:
    """Return the generated description of a feature collection."""
    geometry_types = ", ".join(facts.geometry_types) if facts.geometry_types else "no geometries"
    return (
        f"GeoParquet feature collection of {facts.feature_count} features in {facts.crs}, "
        f"identified by {facts.identifier_property} and holding {geometry_types}."
    )


def _cube_dimensions(facts: _CoverageFacts) -> dict[str, Dimension]:
    """Build the datacube dimensions of a coverage from the snapshot it advertises."""
    system = reference_system(facts.crs)
    return {
        facts.x_dimension: Dimension.from_dict(
            {
                "type": "spatial",
                "axis": "x",
                "extent": [facts.bbox.minimum_x, facts.bbox.maximum_x],
                "reference_system": system,
            },
        ),
        facts.y_dimension: Dimension.from_dict(
            {
                "type": "spatial",
                "axis": "y",
                "extent": [facts.bbox.minimum_y, facts.bbox.maximum_y],
                "reference_system": system,
            },
        ),
        facts.time_dimension: Dimension.from_dict({"type": "temporal", "extent": _temporal_pair(facts)}),
    }


def _temporal_pair(facts: _CoverageFacts) -> list[str | None]:
    """Return the open or closed temporal extent of a coverage as the pair of strings STAC wants."""
    if facts.temporal is None:
        return [None, None]
    return [datetime_to_str(facts.temporal.start), datetime_to_str(facts.temporal.end)]


def _cube_variables(facts: _CoverageFacts) -> dict[str, Variable]:
    """Build one datacube variable per data variable of a coverage, over its three cube dimensions."""
    dimensions = [facts.time_dimension, facts.y_dimension, facts.x_dimension]
    return {name: Variable.from_dict({"dimensions": list(dimensions), "type": "data"}) for name in facts.variables}


def _feature_bounds(facts: _FeatureFacts) -> tuple[float, float, float, float]:
    """Return the WGS84 envelope of a feature collection, falling back to the whole world."""
    if facts.bbox is None:
        return (-180.0, -90.0, 180.0, 90.0)
    return wgs84_bounds(facts.bbox, facts.crs)


def _parquet_href(address: StorageAddress, version: int) -> str:
    """Return the URI of the GeoParquet object of one version of a feature collection."""
    return address.joined("versions", format_vector_version(version), VECTOR_DATA_NAME).as_uri()
