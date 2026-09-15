"""Reads local GeoJSON and GeoParquet files and writes them as a version of a vector collection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import geopandas
import pandas

from ocs_storage_exploration.storage.errors import VectorInputError
from ocs_storage_exploration.storage.failures import backend_transport_failures
from ocs_storage_exploration.storage.paths import resolve_ingest_path
from ocs_storage_exploration.storage.schemas import VectorWriteResult
from ocs_storage_exploration.storage.vector.collection import DEFAULT_CRS, VectorCollectionStore, require_crs

GEOJSON_SUFFIXES: Final[frozenset[str]] = frozenset({".geojson", ".json", ".ndjson", ".gpkg", ".shp", ".fgb"})
GEOPARQUET_SUFFIXES: Final[frozenset[str]] = frozenset({".parquet", ".pq", ".geoparquet"})
DEFAULT_IDENTIFIER_PROPERTY: Final[str] = "id"


@dataclass(frozen=True, slots=True)
class VectorIngestPlan:
    """One resolved vector ingest: the file to read plus the metadata the collection version records."""

    path: Path
    identifier_property: str = DEFAULT_IDENTIFIER_PROPERTY
    selectable_columns: tuple[str, ...] = ()
    crs: str = DEFAULT_CRS
    title: str | None = None
    license: str | None = None
    attribution: str | None = None
    publish: bool = False


def build_vector_ingest_plan(
    *,
    path: str,
    roots: Sequence[Path],
    identifier_property: str = DEFAULT_IDENTIFIER_PROPERTY,
    selectable_columns: Sequence[str] = (),
    crs: str = DEFAULT_CRS,
    title: str | None = None,
    license: str | None = None,
    attribution: str | None = None,
    publish: bool = False,
    working_directory: Path | None = None,
) -> VectorIngestPlan:
    """Resolve the requested path against the ingest roots and record the metadata of the write."""
    return VectorIngestPlan(
        path=resolve_ingest_path(path, roots=roots, working_directory=working_directory),
        identifier_property=identifier_property,
        selectable_columns=tuple(selectable_columns),
        crs=crs,
        title=title,
        license=license,
        attribution=attribution,
        publish=publish,
    )


def ingest_vector_file(
    store: VectorCollectionStore,
    collection_identifier: str,
    plan: VectorIngestPlan,
) -> VectorWriteResult:
    """Read one vector file and write it as the next version of a collection."""
    frame = open_vector_file(plan.path, crs=plan.crs)
    return store.write(
        collection_identifier,
        frame,
        identifier_property=plan.identifier_property,
        title=plan.title,
        license=plan.license,
        attribution=plan.attribution,
        selectable_columns=plan.selectable_columns,
        publish=plan.publish,
    )


def open_vector_file(path: Path, *, crs: str = DEFAULT_CRS) -> geopandas.GeoDataFrame:
    """Read a vector file into a frame, declaring the fallback frame for a source that carries none."""
    suffix = path.suffix.lower()
    with backend_transport_failures(f"reading {path.name!r}"):
        if suffix in GEOPARQUET_SUFFIXES:
            frame = geopandas.read_parquet(path)
        elif suffix in GEOJSON_SUFFIXES:
            frame = geopandas.read_file(path)
        else:
            raise VectorInputError(
                f"{path.name!r} has no readable vector suffix: expected one of "
                f"{sorted(GEOJSON_SUFFIXES | GEOPARQUET_SUFFIXES)}",
            )
    if frame.crs is None:
        # GeoJSON is defined to be WGS84 and a shapefile without a .prj declares nothing, so the
        # request's frame stands in rather than the write being refused for a missing projection.
        frame = frame.set_crs(require_crs(crs))
    if frame.active_geometry_name is None:
        raise VectorInputError(f"{path.name!r} holds no geometry column, so it is not a vector collection")
    return drop_empty_structure_columns(frame)


def drop_empty_structure_columns(frame: geopandas.GeoDataFrame) -> geopandas.GeoDataFrame:
    """Drop columns whose every row is an empty object or array, which Parquet has no type to write.

    Real exports carry them: the DHIS2 organisation units in the samples have a `dimensions` property
    that is `{}` on every feature. Parquet refuses a struct with no child field, and a column that is
    empty in every row carries nothing that could be lost by leaving it out.
    """
    geometry_column = frame.active_geometry_name
    empty = [
        str(column)
        for column in frame.columns
        if str(column) != geometry_column and _is_empty_structure_column(frame[column])
    ]
    if not empty:
        return frame
    return frame.drop(columns=empty)


def _is_empty_structure_column(values: pandas.Series[Any]) -> bool:
    """Report whether every value of a column is an empty mapping or an empty sequence."""
    if values.dtype != object or values.empty:
        return False
    return all(isinstance(value, Mapping | list | tuple) and len(value) == 0 for value in values)
