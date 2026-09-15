"""Versioned GeoParquet collection store that writes, reads, publishes and deletes vector datasets."""

from __future__ import annotations

import io
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast

import geopandas
import numpy
import obstore
import pyarrow
import pyarrow.fs
import pyarrow.parquet
import shapely
from obstore.exceptions import AlreadyExistsError, PreconditionError
from obstore.store import ObjectStore
from pydantic import BaseModel
from pyproj import CRS
from shapely.geometry.base import BaseGeometry

from ocs_storage_exploration.storage.addresses import StorageAddress
from ocs_storage_exploration.storage.errors import (
    FeatureCountGuardError,
    FeatureIdentityError,
    ItemTypeMismatchError,
    NothingToPublishError,
    PublicationConflictError,
    SelectableColumnError,
    SnapshotNotFoundError,
    StorageAddressError,
    StorageError,
)
from ocs_storage_exploration.storage.keys import (
    parse_vector_version,
    validate_dataset_identifier,
    vector_data_key,
    vector_pointer_key,
    vector_prefix,
    vector_version_prefix,
)
from ocs_storage_exploration.storage.models import (
    BoundingBox,
    FeatureDataset,
    FeatureDetail,
    ItemType,
    Publication,
    PublicationResult,
    VectorWriteResult,
    current_timestamp,
)
from ocs_storage_exploration.storage.protocols import Catalog, StorageBackend
from ocs_storage_exploration.storage.vector.predicates import WhereClause, build_filters

if TYPE_CHECKING:
    from obstore import PutResult

    from ocs_storage_exploration.settings import Settings

DEFAULT_CRS: Final[str] = "EPSG:4326"
GEOPARQUET_SCHEMA_VERSION: Final[str] = "1.1.0"
GEOMETRY_ENCODING: Final[str] = "WKB"
PARQUET_COMPRESSION: Final[str] = "zstd"
BBOX_DENSIFY_SEGMENTS: Final[int] = 32
POINTER_ENCODING: Final[str] = "utf-8"


class VectorCollectionPointer(BaseModel):
    """Pointer object naming the published version of a vector collection."""

    version: int
    key: str
    feature_count: int
    published_at: datetime


@dataclass(frozen=True, slots=True)
class VectorReadHandle:
    """Frame produced by a vector read together with the collection version it was read from."""

    frame: geopandas.GeoDataFrame
    version: int


@dataclass(frozen=True, slots=True)
class ParquetSource:
    """Resolved Parquet location: either a filesystem path or the object bytes buffered in memory."""

    path: str | None = None
    payload: bytes | None = None
    filesystem: pyarrow.fs.FileSystem | None = None

    def _require_path(self) -> str:
        """Return the path of this source, which only exists when the bytes were not buffered."""
        if self.path is None:
            raise StorageError("parquet source has neither a path nor buffered bytes")
        return self.path

    def _parquet_file(self) -> pyarrow.parquet.ParquetFile:
        """Open the Parquet footer of this source."""
        if self.payload is not None:
            return pyarrow.parquet.ParquetFile(pyarrow.BufferReader(self.payload))
        return pyarrow.parquet.ParquetFile(self._require_path(), filesystem=self.filesystem)

    def schema(self) -> pyarrow.Schema:
        """Return the Arrow schema of the Parquet source."""
        return self._parquet_file().schema_arrow

    def row_count(self) -> int:
        """Return how many rows the Parquet source holds."""
        return int(self._parquet_file().metadata.num_rows)

    def read(self, **keywords: Any) -> geopandas.GeoDataFrame:
        """Read the Parquet source into a GeoDataFrame."""
        if self.payload is not None:
            return geopandas.read_parquet(pyarrow.BufferReader(self.payload), **keywords)
        return geopandas.read_parquet(self._require_path(), filesystem=self.filesystem, **keywords)


def crs_identifier(crs: Any) -> str:
    """Render a coordinate reference system as an authority code when it has one."""
    reference = CRS.from_user_input(crs)
    authority = reference.to_authority()
    if authority is None:
        return str(reference.to_string())
    return f"{authority[0]}:{authority[1]}"


def same_crs(left: Any, right: Any) -> bool:
    """Report whether two coordinate reference systems describe the same frame."""
    return bool(CRS.from_user_input(left) == CRS.from_user_input(right))


def frame_bounding_box(frame: geopandas.GeoDataFrame) -> BoundingBox | None:
    """Return the envelope of a frame, or None when it is empty or collapses to a line or point."""
    if frame.empty:
        return None
    bounds = [float(value) for value in frame.total_bounds]
    if not all(math.isfinite(value) for value in bounds):
        return None
    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        return None
    return BoundingBox(minimum_x=bounds[0], minimum_y=bounds[1], maximum_x=bounds[2], maximum_y=bounds[3])


def build_frame_from_geojson(
    feature_collection: Mapping[str, Any],
    *,
    identifier_property: str,
    crs: str = DEFAULT_CRS,
) -> geopandas.GeoDataFrame:
    """Build a GeoDataFrame from a GeoJSON FeatureCollection, falling back to the feature id for the identifier."""
    features = feature_collection.get("features")
    if not isinstance(features, list) or not features:
        raise FeatureIdentityError("geojson feature collection must carry a non-empty features array")
    prepared: list[dict[str, Any]] = []
    for feature in features:
        properties = dict(feature.get("properties") or {})
        if properties.get(identifier_property) is None and feature.get("id") is not None:
            properties[identifier_property] = feature["id"]
        prepared.append({**feature, "properties": properties})
    return geopandas.GeoDataFrame.from_features(prepared, crs=crs)


def versions_prefix(identifier: str) -> str:
    """Return the key prefix holding every version directory of a collection."""
    # Derived from the version prefix so the layout stays owned by keys.py alone.
    return vector_version_prefix(identifier, 1).rsplit("/", maxsplit=1)[0]


class VectorCollectionStore:
    """Stores vector collections as immutable GeoParquet versions with a published pointer object."""

    def __init__(self, backend: StorageBackend, catalog: Catalog, settings: Settings) -> None:
        """Bind the store to one backend, catalog and settings block, remembering no pointer etag yet."""
        self._backend = backend
        self._catalog = catalog
        self._settings = settings
        self._pointer_etags: dict[str, str] = {}

    @property
    def backend(self) -> StorageBackend:
        """Backend the collection objects are stored in."""
        return self._backend

    @property
    def catalog(self) -> Catalog:
        """Catalog holding the record that makes a collection exist."""
        return self._catalog

    def write(
        self,
        collection_identifier: str,
        frame: geopandas.GeoDataFrame,
        *,
        identifier_property: str,
        title: str | None = None,
        selectable_columns: Sequence[str] = (),
        publish: bool = False,
    ) -> VectorWriteResult:
        """Write a frame as the next version of a collection and optionally publish it right away."""
        identifier = validate_dataset_identifier(collection_identifier)
        prepared = self._prepare_frame(frame, identifier_property=identifier_property)
        declared = self._validate_selectable_columns(prepared, selectable_columns)
        version = self._next_version(identifier)
        self._write_parquet(self._backend.address(vector_data_key(identifier, version)), prepared)
        record = self._build_record(
            identifier,
            prepared,
            identifier_property=identifier_property,
            title=title,
            selectable_columns=declared,
        )
        self._catalog.put(record)
        if publish:
            self.publish(identifier, version=version)
        return VectorWriteResult(
            dataset_identifier=identifier,
            version=version,
            feature_count=int(len(prepared)),
            published=publish,
        )

    def write_geojson(
        self,
        collection_identifier: str,
        feature_collection: Mapping[str, Any],
        *,
        identifier_property: str,
        crs: str = DEFAULT_CRS,
        title: str | None = None,
        selectable_columns: Sequence[str] = (),
        publish: bool = False,
    ) -> VectorWriteResult:
        """Write a GeoJSON FeatureCollection as the next version of a collection."""
        frame = build_frame_from_geojson(feature_collection, identifier_property=identifier_property, crs=crs)
        return self.write(
            collection_identifier,
            frame,
            identifier_property=identifier_property,
            title=title,
            selectable_columns=selectable_columns,
            publish=publish,
        )

    def read(
        self,
        collection_identifier: str,
        *,
        bbox: BoundingBox | None = None,
        bbox_crs: str = DEFAULT_CRS,
        where: WhereClause | None = None,
        columns: Sequence[str] | None = None,
        limit: int | None = None,
        version: int | None = None,
    ) -> VectorReadHandle:
        """Read one version of a collection, pruning by envelope and clause before the exact intersection test."""
        identifier = validate_dataset_identifier(collection_identifier)
        record = self._require_feature_record(identifier)
        self._guard_unqualified_read(identifier, record, bbox=bbox, where=where)
        resolved = self._resolve_version(identifier, version)
        source = self._parquet_source(self._backend.address(vector_data_key(identifier, resolved)))
        schema = source.schema()
        keywords: dict[str, Any] = {}
        filters = build_filters(
            where or {},
            selectable_columns=record.features.selectable_columns,
            schema=schema,
        )
        if filters:
            keywords["filters"] = filters
        window = None if bbox is None else self._resolve_window(bbox, bbox_crs, record.crs)
        if window is not None:
            keywords["bbox"] = window.bounds
        if columns is not None:
            keywords["columns"] = self._resolve_columns(identifier, columns, record, schema)
        frame = source.read(**keywords)
        if window is not None:
            # The covering bbox column prunes by envelope only, so the exact test has to run here.
            frame = frame[frame.geometry.intersects(window)]
        if limit is not None:
            frame = frame.head(limit)
        return VectorReadHandle(frame=frame.reset_index(drop=True), version=resolved)

    def publish(self, collection_identifier: str, *, version: int | None = None) -> PublicationResult:
        """Move the published pointer of a collection to one version, which is also how a rollback works."""
        identifier = validate_dataset_identifier(collection_identifier)
        record = self._require_feature_record(identifier)
        available = self.versions(identifier)
        if not available:
            raise NothingToPublishError(f"collection {identifier!r} has no written version to publish")
        target = available[-1] if version is None else version
        if target not in available:
            raise SnapshotNotFoundError(f"collection {identifier!r} has no version {target}")
        pointer = self._read_pointer(identifier)
        previous_version = None if pointer is None else pointer.version
        if pointer is None or pointer.version != target:
            self._write_pointer(identifier, target)
        published_at = current_timestamp()
        record.publication = Publication(
            published=True,
            published_at=published_at,
            version=target,
            previous_version=previous_version,
        )
        record.updated_at = published_at
        self._catalog.put(record)
        return PublicationResult(
            dataset_identifier=identifier,
            item_type=ItemType.FEATURE,
            published=True,
            version=target,
            previous_version=previous_version,
        )

    def versions(self, collection_identifier: str) -> list[int]:
        """List the version numbers already written for a collection, in ascending order."""
        identifier = validate_dataset_identifier(collection_identifier)
        address = self._backend.address(versions_prefix(identifier))
        marker = f"{address.key}/"
        found: set[int] = set()
        for key in self._backend.list_keys(address):
            if not key.startswith(marker):
                continue
            name = key[len(marker) :].split("/", maxsplit=1)[0]
            try:
                found.add(parse_vector_version(name))
            except StorageAddressError:
                continue
        return sorted(found)

    def current_version(self, collection_identifier: str) -> int | None:
        """Return the published version of a collection, or None when nothing is published."""
        identifier = validate_dataset_identifier(collection_identifier)
        pointer = self._read_pointer(identifier)
        return None if pointer is None else pointer.version

    def delete(self, collection_identifier: str) -> int:
        """Delete the catalog record of a collection first, then every object below its prefix."""
        identifier = validate_dataset_identifier(collection_identifier)
        self._require_feature_record(identifier)
        self._catalog.delete(identifier)
        self._pointer_etags.pop(identifier, None)
        return self._backend.delete_prefix(self._backend.address(vector_prefix(identifier)))

    def _prepare_frame(self, frame: geopandas.GeoDataFrame, *, identifier_property: str) -> geopandas.GeoDataFrame:
        """Validate feature identity and the coordinate reference system, then Hilbert-sort the frame."""
        geometry_column = frame.active_geometry_name
        if geometry_column is None:
            raise FeatureIdentityError("vector frame must have an active geometry column")
        if identifier_property not in frame.columns:
            raise FeatureIdentityError(
                f"identifier property {identifier_property!r} is missing; available columns are {list(frame.columns)}",
            )
        if frame.crs is None:
            raise FeatureIdentityError("vector frame must declare a coordinate reference system")
        identifiers = frame[identifier_property]
        null_rows = [position for position, missing in enumerate(identifiers.isna().tolist()) if missing]
        if null_rows:
            raise FeatureIdentityError(
                f"identifier property {identifier_property!r} is null in rows {null_rows}",
            )
        duplicates = sorted({str(value) for value in identifiers[identifiers.duplicated()].tolist()})
        if duplicates:
            raise FeatureIdentityError(
                f"identifier property {identifier_property!r} repeats values {duplicates}",
            )
        if frame.geometry.isna().any():
            raise FeatureIdentityError("vector frame must not contain a missing geometry")
        if frame.empty:
            return frame.reset_index(drop=True)
        order = numpy.argsort(frame.geometry.hilbert_distance().to_numpy(), kind="stable")
        return frame.iloc[order].reset_index(drop=True)

    def _validate_selectable_columns(
        self,
        frame: geopandas.GeoDataFrame,
        selectable_columns: Sequence[str],
    ) -> tuple[str, ...]:
        """Check every declared selectable column is a real, non geometry column of the frame."""
        geometry_column = frame.active_geometry_name
        declared: list[str] = []
        for column in selectable_columns:
            if column in declared:
                continue
            if column not in frame.columns:
                raise SelectableColumnError(f"selectable column {column!r} is not a column of the frame")
            if column == geometry_column:
                raise SelectableColumnError(f"the geometry column {column!r} cannot be declared selectable")
            declared.append(column)
        return tuple(declared)

    def _next_version(self, identifier: str) -> int:
        """Return the version number one past the highest version already written."""
        existing = self.versions(identifier)
        return existing[-1] + 1 if existing else 1

    def _write_parquet(self, address: StorageAddress, frame: geopandas.GeoDataFrame) -> None:
        """Write a frame as GeoParquet with a covering bbox column, through pyarrow or buffered through obstore."""
        options: dict[str, Any] = {
            "write_covering_bbox": True,
            "schema_version": GEOPARQUET_SCHEMA_VERSION,
            "geometry_encoding": GEOMETRY_ENCODING,
            "compression": PARQUET_COMPRESSION,
            "row_group_size": self._settings.parquet_row_group_size,
        }
        filesystem = self._backend.parquet_filesystem()
        if filesystem is None:
            buffer = io.BytesIO()
            frame.to_parquet(buffer, **options)
            obstore.put(self._backend.object_store(), address.key, buffer.getvalue())
            return
        # to_parquet will not create the version directory itself on a local filesystem.
        filesystem.create_dir(self._backend.parquet_path(address.parent()), recursive=True)
        frame.to_parquet(self._backend.parquet_path(address), filesystem=filesystem, **options)

    def _parquet_source(self, address: StorageAddress) -> ParquetSource:
        """Resolve an address into a Parquet source, buffering the bytes when no pyarrow filesystem exists."""
        filesystem = self._backend.parquet_filesystem()
        if filesystem is None:
            try:
                payload = bytes(obstore.get(self._backend.object_store(), address.key).bytes())
            except FileNotFoundError as error:
                # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
                raise SnapshotNotFoundError(f"no vector data object at {address.as_uri()}") from error
            return ParquetSource(payload=payload)
        return ParquetSource(path=self._backend.parquet_path(address), filesystem=filesystem)

    def _build_record(
        self,
        identifier: str,
        frame: geopandas.GeoDataFrame,
        *,
        identifier_property: str,
        title: str | None,
        selectable_columns: tuple[str, ...],
    ) -> FeatureDataset:
        """Build the catalog record describing the collection after a write, keeping what an earlier record held."""
        existing = self._catalog.get(identifier)
        if existing is not None and not isinstance(existing, FeatureDataset):
            raise ItemTypeMismatchError(f"dataset {identifier!r} is not a vector collection")
        detail = FeatureDetail(
            identifier_property=identifier_property,
            feature_count=int(len(frame)),
            primary_geometry=str(frame.active_geometry_name),
            geometry_types=tuple(sorted({str(value) for value in frame.geom_type.dropna().unique()})),
            selectable_columns=selectable_columns,
        )
        written_at = current_timestamp()
        return FeatureDataset(
            dataset_identifier=identifier,
            title=title or (existing.title if existing is not None else identifier),
            address=self._backend.address(vector_prefix(identifier)).as_uri(),
            created_at=existing.created_at if existing is not None else written_at,
            updated_at=written_at,
            bbox=frame_bounding_box(frame),
            publication=existing.publication if existing is not None else Publication(),
            crs=crs_identifier(frame.crs),
            features=detail,
        )

    def _require_feature_record(self, identifier: str) -> FeatureDataset:
        """Read the catalog record of a collection and refuse anything that is not a vector dataset."""
        record = self._catalog.require(identifier)
        if not isinstance(record, FeatureDataset):
            raise ItemTypeMismatchError(f"dataset {identifier!r} is not a vector collection")
        return record

    def _guard_unqualified_read(
        self,
        identifier: str,
        record: FeatureDataset,
        *,
        bbox: BoundingBox | None,
        where: WhereClause | None,
    ) -> None:
        """Refuse a read that carries neither an envelope nor a clause when the collection is too large."""
        if bbox is not None or where:
            return
        threshold = self._settings.max_unqualified_feature_count
        if record.features.feature_count > threshold:
            raise FeatureCountGuardError(
                f"collection {identifier!r} holds {record.features.feature_count} features; "
                f"an unqualified read is limited to {threshold}",
            )

    def _resolve_version(self, identifier: str, version: int | None) -> int:
        """Resolve the version to read: the requested one, else the published one, else the latest written."""
        available = self.versions(identifier)
        if not available:
            raise SnapshotNotFoundError(f"collection {identifier!r} has no written version")
        if version is not None:
            if version not in available:
                raise SnapshotNotFoundError(f"collection {identifier!r} has no version {version}")
            return version
        pointer = self._read_pointer(identifier)
        if pointer is not None and pointer.version in available:
            return pointer.version
        return available[-1]

    def _resolve_window(self, bbox: BoundingBox, bbox_crs: str, collection_crs: str) -> BaseGeometry:
        """Turn a request envelope into a polygon in the collection frame, densified before it is reprojected."""
        window = shapely.box(*bbox.as_tuple())
        if same_crs(bbox_crs, collection_crs):
            return window
        span = max(bbox.maximum_x - bbox.minimum_x, bbox.maximum_y - bbox.minimum_y)
        densified = shapely.segmentize(window, max_segment_length=span / BBOX_DENSIFY_SEGMENTS)
        projected = geopandas.GeoSeries([densified], crs=bbox_crs).to_crs(collection_crs)
        return cast(BaseGeometry, projected.iloc[0])

    def _resolve_columns(
        self,
        identifier: str,
        columns: Sequence[str],
        record: FeatureDataset,
        schema: pyarrow.Schema,
    ) -> list[str]:
        """Return the columns to read, always keeping the identifier property and the geometry column."""
        selected: list[str] = []
        for column in (*columns, record.features.identifier_property, record.features.primary_geometry):
            if column in selected:
                continue
            if column not in schema.names:
                raise SelectableColumnError(f"column {column!r} is not present in collection {identifier!r}")
            selected.append(column)
        return selected

    def _pointer_address(self, identifier: str) -> StorageAddress:
        """Return the address of the published-version pointer object of a collection."""
        return self._backend.address(vector_pointer_key(identifier))

    def _read_pointer(self, identifier: str) -> VectorCollectionPointer | None:
        """Read the pointer object of a collection and remember its etag, or return None when it is absent."""
        key = self._pointer_address(identifier).key
        try:
            result = obstore.get(self._backend.object_store(), key)
            etag = result.meta.get("e_tag")
            payload = bytes(result.bytes())
        except FileNotFoundError:
            # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
            self._pointer_etags.pop(identifier, None)
            return None
        self._remember_pointer_etag(identifier, etag)
        return VectorCollectionPointer.model_validate_json(payload)

    def _write_pointer(self, identifier: str, version: int) -> VectorCollectionPointer:
        """Write the pointer object of a collection under compare-and-swap against the etag last read."""
        data_address = self._backend.address(vector_data_key(identifier, version))
        pointer = VectorCollectionPointer(
            version=version,
            key=data_address.key,
            feature_count=self._parquet_source(data_address).row_count(),
            published_at=current_timestamp(),
        )
        key = self._pointer_address(identifier).key
        store = self._backend.object_store()
        payload = pointer.model_dump_json().encode(POINTER_ENCODING)
        known_etag = self._pointer_etags.get(identifier)
        if known_etag is None:
            result = self._create_pointer(store, key, payload)
        else:
            result = self._replace_pointer(store, key, payload, known_etag)
        self._remember_pointer_etag(identifier, result.get("e_tag"))
        return pointer

    def _create_pointer(self, store: ObjectStore, key: str, payload: bytes) -> PutResult:
        """Create the pointer object, refusing to overwrite one a concurrent publication wrote first."""
        try:
            return obstore.put(store, key, payload, mode="create")
        except AlreadyExistsError as error:
            raise PublicationConflictError(f"pointer {key!r} was created by a concurrent publication") from error

    def _replace_pointer(self, store: ObjectStore, key: str, payload: bytes, etag: str) -> PutResult:
        """Replace the pointer object only while it still carries the etag this store last saw."""
        try:
            return obstore.put(store, key, payload, mode={"e_tag": etag})
        except PreconditionError as error:
            raise PublicationConflictError(f"pointer {key!r} changed since it was read") from error
        except FileNotFoundError as error:
            raise PublicationConflictError(f"pointer {key!r} was deleted since it was read") from error
        except NotImplementedError:
            self._assert_pointer_unchanged(store, key, etag)
            return obstore.put(store, key, payload)

    def _assert_pointer_unchanged(self, store: ObjectStore, key: str, etag: str) -> None:
        """Emulate compare-and-swap for stores without conditional writes, which is not atomic."""
        try:
            current = obstore.head(store, key)
        except FileNotFoundError as error:
            raise PublicationConflictError(f"pointer {key!r} was deleted since it was read") from error
        if current.get("e_tag") != etag:
            raise PublicationConflictError(f"pointer {key!r} changed since it was read")

    def _remember_pointer_etag(self, identifier: str, etag: str | None) -> None:
        """Record or drop the etag last seen for the pointer object of one collection."""
        if etag is None:
            self._pointer_etags.pop(identifier, None)
        else:
            self._pointer_etags[identifier] = etag
