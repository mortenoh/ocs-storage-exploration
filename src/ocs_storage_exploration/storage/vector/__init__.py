"""Vector storage engine over versioned GeoParquet collections."""

from ocs_storage_exploration.storage.vector.collection import (
    DEFAULT_CRS,
    GEOPARQUET_SCHEMA_VERSION,
    ParquetSource,
    VectorCollectionPointer,
    VectorCollectionStore,
    VectorReadHandle,
    VectorTableSchema,
    build_frame_from_geojson,
    crs_identifier,
    frame_bounding_box,
    same_crs,
    versions_prefix,
)
from ocs_storage_exploration.storage.vector.predicates import (
    WhereClause,
    build_filters,
    build_prefix_filter,
    coerce_clause_value,
    is_prefix_value,
    parse_where,
)

__all__ = [
    "DEFAULT_CRS",
    "GEOPARQUET_SCHEMA_VERSION",
    "ParquetSource",
    "VectorCollectionPointer",
    "VectorCollectionStore",
    "VectorReadHandle",
    "VectorTableSchema",
    "WhereClause",
    "build_filters",
    "build_frame_from_geojson",
    "build_prefix_filter",
    "coerce_clause_value",
    "crs_identifier",
    "frame_bounding_box",
    "is_prefix_value",
    "parse_where",
    "same_crs",
    "versions_prefix",
]
