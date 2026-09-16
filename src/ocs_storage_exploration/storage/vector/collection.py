"""Versioned GeoParquet collection store that writes, reads, publishes and deletes vector datasets."""

from __future__ import annotations

import io
import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast

import geopandas
import numpy
import pyarrow
import pyarrow.fs
import pyarrow.parquet
import shapely
from geojson_pydantic import FeatureCollection
from pydantic import BaseModel, ValidationError
from pyproj import CRS
from pyproj.exceptions import CRSError as PyprojCrsError
from shapely.geometry.base import BaseGeometry

from ocs_storage_exploration.storage.addresses import StorageAddress
from ocs_storage_exploration.storage.errors import (
    CrsError,
    DatasetNotFoundError,
    FeatureCountGuardError,
    FeatureIdentityError,
    ItemTypeMismatchError,
    NothingToPublishError,
    PublicationConflictError,
    SelectableColumnError,
    SnapshotNotFoundError,
    StorageAddressError,
    StorageError,
    VectorInputError,
)
from ocs_storage_exploration.storage.failures import backend_transport_failures
from ocs_storage_exploration.storage.keys import (
    VECTOR_METADATA_NAME,
    parse_vector_version,
    validate_dataset_identifier,
    vector_data_key,
    vector_pointer_key,
    vector_prefix,
    vector_reservation_key,
    vector_version_metadata_key,
    vector_version_prefix,
)
from ocs_storage_exploration.storage.objects import (
    create_object,
    create_object_if_absent,
    put_object,
    read_object,
    replace_object,
)
from ocs_storage_exploration.storage.protocols import Catalog, StorageBackend
from ocs_storage_exploration.storage.schemas import (
    BoundingBox,
    DatasetLifecycle,
    FeatureDataset,
    ItemType,
    Publication,
    PublicationResult,
    VectorVersionMetadata,
    VectorWriteResult,
    current_timestamp,
)
from ocs_storage_exploration.storage.vector.predicates import WhereClause, build_filters

if TYPE_CHECKING:
    from ocs_storage_exploration.settings import Settings

DEFAULT_CRS: Final[str] = "EPSG:4326"
GEOPARQUET_SCHEMA_VERSION: Final[str] = "1.1.0"
GEOMETRY_ENCODING: Final[str] = "WKB"
PARQUET_COMPRESSION: Final[str] = "zstd"
BBOX_DENSIFY_SEGMENTS: Final[int] = 32
OBJECT_ENCODING: Final[str] = "utf-8"
POINTER_LABEL: Final[str] = "pointer"
METADATA_LABEL: Final[str] = "version metadata"
MAXIMUM_RESERVATION_ATTEMPTS: Final[int] = 32


class VectorCollectionPointer(BaseModel):
    """Pointer object naming the published version of a vector collection."""

    version: int
    key: str
    feature_count: int
    published_at: datetime


class VectorVersionReservation(BaseModel):
    """Reservation object claiming one version number of a collection before its data is written."""

    version: int
    reserved_at: datetime


@dataclass(frozen=True, slots=True)
class CollectionEntry:
    """The catalog record of a vector collection together with the revision it was read at."""

    record: FeatureDataset
    revision: str


@dataclass(frozen=True, slots=True)
class VectorReadHandle:
    """Frame produced by a vector read together with the collection version it was read from."""

    frame: geopandas.GeoDataFrame
    version: int


@dataclass(frozen=True, slots=True)
class VectorTableSchema:
    """Row count and column types of one version of a collection, as its Parquet footer records them."""

    version: int
    row_count: int
    column_types: dict[str, str]


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
        with backend_transport_failures(f"reading the Parquet footer of {self._require_path()!r}"):
            return pyarrow.parquet.ParquetFile(self._require_path(), filesystem=self.filesystem)

    def schema(self) -> pyarrow.Schema:
        """Return the Arrow schema of the Parquet source."""
        return self._parquet_file().schema_arrow

    def row_count(self) -> int:
        """Return how many rows the Parquet source holds."""
        return int(self._parquet_file().metadata.num_rows)

    def schema_and_row_count(self) -> tuple[pyarrow.Schema, int]:
        """Return the Arrow schema and the row count of the Parquet source from one footer read."""
        parquet_file = self._parquet_file()
        return parquet_file.schema_arrow, int(parquet_file.metadata.num_rows)

    def read(self, **keywords: Any) -> geopandas.GeoDataFrame:
        """Read the Parquet source into a GeoDataFrame."""
        if self.payload is not None:
            return geopandas.read_parquet(pyarrow.BufferReader(self.payload), **keywords)
        with backend_transport_failures(f"reading {self._require_path()!r}"):
            return geopandas.read_parquet(self._require_path(), filesystem=self.filesystem, **keywords)


def require_crs(value: Any, *, label: str = "crs") -> CRS:
    """Parse a coordinate reference system, refusing anything pyproj cannot read."""
    try:
        return CRS.from_user_input(value)
    except (PyprojCrsError, TypeError, ValueError) as error:
        raise CrsError(f"{label} is not a coordinate reference system: {value!r}") from error


def crs_identifier(crs: Any) -> str:
    """Render a coordinate reference system as an authority code when it has one."""
    reference = require_crs(crs)
    authority = reference.to_authority()
    if authority is None:
        return str(reference.to_string())
    return f"{authority[0]}:{authority[1]}"


def same_crs(left: Any, right: Any) -> bool:
    """Report whether two coordinate reference systems describe the same frame."""
    return bool(require_crs(left) == require_crs(right))


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


def validate_feature_collection(feature_collection: FeatureCollection | Mapping[str, Any]) -> FeatureCollection:
    """Validate a mapping into a geojson FeatureCollection, naming where a malformed one goes wrong."""
    if isinstance(feature_collection, FeatureCollection):
        return feature_collection
    try:
        return FeatureCollection.model_validate(feature_collection)
    except ValidationError as error:
        first = error.errors(include_url=False)[0]
        location = ".".join(str(part) for part in first["loc"]) or "the feature collection"
        raise VectorInputError(f"geojson is malformed at {location}: {first['msg']}") from error


def build_frame_from_geojson(
    feature_collection: FeatureCollection | Mapping[str, Any],
    *,
    identifier_property: str,
    crs: str = DEFAULT_CRS,
) -> geopandas.GeoDataFrame:
    """Build a GeoDataFrame from a GeoJSON FeatureCollection, falling back to the feature id for the identifier."""
    collection = validate_feature_collection(feature_collection)
    if not collection.features:
        raise FeatureIdentityError("geojson feature collection must carry a non-empty features array")
    prepared: list[dict[str, Any]] = []
    for index, feature in enumerate(collection.features):
        if feature.geometry is None:
            raise VectorInputError(f"feature {index} has a null geometry, which a collection version cannot store")
        properties = dict(feature.properties or {})
        if properties.get(identifier_property) is None and feature.id is not None:
            properties[identifier_property] = feature.id
        prepared.append({"type": "Feature", "geometry": feature.geometry.model_dump(), "properties": properties})
    return geopandas.GeoDataFrame.from_features(prepared, crs=require_crs(crs))


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
        self._pointer_state = threading.local()

    @property
    def _pointer_etags(self) -> dict[str, str]:
        """Return the pointer etags this thread last read, so no publication writes against another thread's."""
        # One store serves several requests on the threadpool. A shared cache would let a publication
        # replace a pointer using the etag a concurrent reader refreshed, which is the lost update
        # the compare-and-swap exists to refuse. Every publication reads the pointer before writing
        # it, so a per-thread cache loses nothing.
        etags: dict[str, str] | None = getattr(self._pointer_state, "etags", None)
        if etags is None:
            etags = {}
            self._pointer_state.etags = etags
        return etags

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
        license: str | None = None,
        attribution: str | None = None,
        selectable_columns: Sequence[str] = (),
        publish: bool = False,
    ) -> VectorWriteResult:
        """Write a frame as the next version of a collection and optionally publish it right away."""
        identifier = validate_dataset_identifier(collection_identifier)
        existing = self._existing_collection(identifier)
        if existing is not None and existing.record.is_deleting:
            # The prefix this write is about to fill is being emptied. Finishing that deletion first is
            # the only way to write into a prefix nobody else is sweeping; the write then creates the
            # record conditionally, so a second writer doing the same thing loses at the create.
            self._complete_deletion(existing)
            existing = None
        prepared = self._prepare_frame(frame, identifier_property=identifier_property)
        declared = self._validate_selectable_columns(prepared, selectable_columns)
        previous = None if existing is None else existing.record
        version = self.reserve_version(identifier)
        self._write_parquet(self._backend.address(vector_data_key(identifier, version)), prepared)
        metadata = self._write_version_metadata(
            identifier,
            version,
            prepared,
            identifier_property=identifier_property,
            selectable_columns=declared,
            license=license or (previous.license if previous is not None else None),
            attribution=attribution or (previous.attribution if previous is not None else None),
        )
        record = self._build_record(identifier, metadata, title=title, previous=previous)
        if existing is None:
            self._catalog.put(record, create=True)
        else:
            self._catalog.put(record, revision=existing.revision)
        if publish:
            self.publish(identifier, version=version)
        return VectorWriteResult(
            dataset_identifier=identifier,
            version=version,
            feature_count=metadata.feature_count,
            published=publish,
        )

    def write_geojson(
        self,
        collection_identifier: str,
        feature_collection: FeatureCollection | Mapping[str, Any],
        *,
        identifier_property: str,
        crs: str = DEFAULT_CRS,
        title: str | None = None,
        license: str | None = None,
        attribution: str | None = None,
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
            license=license,
            attribution=attribution,
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
        # The record proves the collection exists and is a vector dataset; it describes no version.
        self._require_collection(identifier)
        resolved = self._resolve_version(identifier, version)
        # Every field below describes the version being read, not whatever the newest write left behind.
        metadata = self.version_metadata(identifier, resolved)
        self._guard_unqualified_read(identifier, metadata, bbox=bbox, where=where)
        source = self._parquet_source(self._backend.address(vector_data_key(identifier, resolved)))
        schema = source.schema()
        keywords: dict[str, Any] = {}
        filters = build_filters(
            where or {},
            selectable_columns=metadata.selectable_columns,
            schema=schema,
        )
        if filters:
            keywords["filters"] = filters
        window = None if bbox is None else self._resolve_window(bbox, bbox_crs, metadata.crs)
        if window is not None:
            keywords["bbox"] = window.bounds
        if columns is not None:
            keywords["columns"] = self._resolve_columns(identifier, columns, metadata, schema)
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
        entry = self._require_collection(identifier)
        available = self.versions(identifier)
        if not available:
            raise NothingToPublishError(f"collection {identifier!r} has no written version to publish")
        target = available[-1] if version is None else version
        if target not in available:
            raise SnapshotNotFoundError(f"collection {identifier!r} has no version {target}")
        metadata = self.version_metadata(identifier, target)
        pointer = self._read_pointer(identifier)
        previous_version = None if pointer is None else pointer.version
        if pointer is None or pointer.version != target:
            self._write_pointer(identifier, metadata)
        published_at = current_timestamp()
        entry.record.publication = Publication(
            published=True,
            published_at=published_at,
            version=target,
            previous_version=previous_version,
        )
        entry.record.updated_at = published_at
        self._catalog.put(entry.record, revision=entry.revision)
        return PublicationResult(
            dataset_identifier=identifier,
            item_type=ItemType.FEATURE,
            published=True,
            version=target,
            previous_version=previous_version,
        )

    def versions(self, collection_identifier: str) -> list[int]:
        """List the version numbers a collection finished writing, in ascending order."""
        return self._list_versions(collection_identifier, completed_only=True)

    def claimed_versions(self, collection_identifier: str) -> list[int]:
        """List every version number a collection claimed, including a write that never finished."""
        return self._list_versions(collection_identifier, completed_only=False)

    def reserve_version(
        self,
        collection_identifier: str,
        *,
        attempts: int = MAXIMUM_RESERVATION_ATTEMPTS,
    ) -> int:
        """Claim the next free version number by creating its reservation object, which no two writers can share."""
        identifier = validate_dataset_identifier(collection_identifier)
        written = self.versions(identifier)
        candidate = written[-1] + 1 if written else 1
        store = self._backend.object_store()
        for _ in range(attempts):
            reservation = VectorVersionReservation(version=candidate, reserved_at=current_timestamp())
            key = self._backend.address(vector_reservation_key(identifier, candidate)).key
            claimed = create_object_if_absent(store, key, reservation.model_dump_json().encode(OBJECT_ENCODING))
            # A reservation object that is ours is not enough: an earlier crash may have left data behind.
            if claimed is not None and not self._data_object_exists(identifier, candidate):
                return candidate
            candidate += 1
        raise PublicationConflictError(
            f"collection {identifier!r} found no free version number in {attempts} attempts",
        )

    def version_metadata(self, collection_identifier: str, version: int) -> VectorVersionMetadata:
        """Read the metadata sidecar describing one version of a collection as it was written."""
        identifier = validate_dataset_identifier(collection_identifier)
        key = self._backend.address(vector_version_metadata_key(identifier, version)).key
        stored = read_object(self._backend.object_store(), key)
        if stored is None:
            raise SnapshotNotFoundError(f"collection {identifier!r} has no version {version}")
        return VectorVersionMetadata.model_validate_json(stored.payload)

    def published_metadata(self, collection_identifier: str) -> VectorVersionMetadata | None:
        """Read the metadata sidecar of the published version, or None while nothing is published."""
        pointer = self.pointer(collection_identifier)
        return None if pointer is None else self.version_metadata(collection_identifier, pointer.version)

    def table_schema(self, collection_identifier: str, *, version: int | None = None) -> VectorTableSchema:
        """Describe one version of a collection from its Parquet footer alone, reading no row group."""
        identifier = validate_dataset_identifier(collection_identifier)
        resolved = self._resolve_version(identifier, version)
        source = self._parquet_source(self._backend.address(vector_data_key(identifier, resolved)))
        schema, row_count = source.schema_and_row_count()
        return VectorTableSchema(
            version=resolved,
            row_count=row_count,
            column_types={str(field.name): str(field.type) for field in schema},
        )

    def pointer(self, collection_identifier: str) -> VectorCollectionPointer | None:
        """Read the published-version pointer object of a collection, or None when nothing is published."""
        return self._read_pointer(validate_dataset_identifier(collection_identifier))

    def current_version(self, collection_identifier: str) -> int | None:
        """Return the published version of a collection, or None when nothing is published."""
        pointer = self.pointer(collection_identifier)
        return None if pointer is None else pointer.version

    def delete(self, collection_identifier: str) -> int:
        """Mark the record of a collection as deleting, sweep every object below its prefix, then drop it.

        The record goes last rather than first: while it is there and marked, a concurrent writer is
        told a deletion is running instead of finding no record at all and writing into a prefix that
        is about to be emptied. A deletion that stops half way leaves the marked record behind, and
        deleting again, or writing the collection again, finishes it.
        """
        identifier = validate_dataset_identifier(collection_identifier)
        # Read raw: a record already marked as deleting is exactly what this call is here to finish.
        entry = self._collection_entry(identifier)
        reserved = self._reserve_deletion(entry)
        removed = self._sweep(identifier)
        self._drop_reserved_record(identifier, reserved)
        return removed

    def _list_versions(self, collection_identifier: str, *, completed_only: bool) -> list[int]:
        """List the version numbers below the versions prefix, optionally only those with a metadata sidecar."""
        identifier = validate_dataset_identifier(collection_identifier)
        address = self._backend.address(versions_prefix(identifier))
        marker = f"{address.key}/"
        found: set[int] = set()
        for key in self._backend.list_keys(address):
            if not key.startswith(marker):
                continue
            name, _, remainder = key[len(marker) :].partition("/")
            if completed_only and remainder != VECTOR_METADATA_NAME:
                continue
            try:
                found.add(parse_vector_version(name))
            except StorageAddressError:
                continue
        return sorted(found)

    def _data_object_exists(self, identifier: str, version: int) -> bool:
        """Report whether the GeoParquet object of one version is already on the backend."""
        return self._backend.exists(self._backend.address(vector_data_key(identifier, version)))

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
            put_object(self._backend.object_store(), address.key, buffer.getvalue())
            return
        with backend_transport_failures(f"writing {address.as_uri()}"):
            if isinstance(filesystem, pyarrow.fs.LocalFileSystem):
                # to_parquet will not create the version directory itself on a local filesystem. An object
                # store has no directories, and create_dir would leave a marker object behind that a prefix
                # sweep cannot delete, so the call is made only where it is needed.
                filesystem.create_dir(self._backend.parquet_path(address.parent()), recursive=True)
            frame.to_parquet(self._backend.parquet_path(address), filesystem=filesystem, **options)

    def _parquet_source(self, address: StorageAddress) -> ParquetSource:
        """Resolve an address into a Parquet source, buffering the bytes when no pyarrow filesystem exists."""
        filesystem = self._backend.parquet_filesystem()
        if filesystem is None:
            stored = read_object(self._backend.object_store(), address.key)
            if stored is None:
                raise SnapshotNotFoundError(f"no vector data object at {address.as_uri()}")
            return ParquetSource(payload=stored.payload)
        return ParquetSource(path=self._backend.parquet_path(address), filesystem=filesystem)

    def _write_version_metadata(
        self,
        identifier: str,
        version: int,
        frame: geopandas.GeoDataFrame,
        *,
        identifier_property: str,
        selectable_columns: tuple[str, ...],
        license: str | None,
        attribution: str | None,
    ) -> VectorVersionMetadata:
        """Write the sidecar that completes a version, as a create so no concurrent writer can replace it."""
        metadata = VectorVersionMetadata(
            version=version,
            crs=crs_identifier(frame.crs),
            feature_count=int(len(frame)),
            identifier_property=identifier_property,
            primary_geometry=str(frame.active_geometry_name),
            geometry_types=tuple(sorted({str(value) for value in frame.geom_type.dropna().unique()})),
            selectable_columns=selectable_columns,
            bbox=frame_bounding_box(frame),
            written_at=current_timestamp(),
            license=license,
            attribution=attribution,
        )
        key = self._backend.address(vector_version_metadata_key(identifier, version)).key
        payload = metadata.model_dump_json().encode(OBJECT_ENCODING)
        create_object(self._backend.object_store(), key, payload, label=METADATA_LABEL)
        return metadata

    def _build_record(
        self,
        identifier: str,
        metadata: VectorVersionMetadata,
        *,
        title: str | None,
        previous: FeatureDataset | None,
    ) -> FeatureDataset:
        """Build the catalog record describing the latest write, keeping what an earlier record held."""
        return FeatureDataset(
            dataset_identifier=identifier,
            title=title or (previous.title if previous is not None else identifier),
            storage_key=vector_prefix(identifier),
            created_at=previous.created_at if previous is not None else metadata.written_at,
            updated_at=metadata.written_at,
            bbox=metadata.bbox,
            license=metadata.license,
            attribution=metadata.attribution,
            publication=previous.publication if previous is not None else Publication(),
            crs=metadata.crs,
            features=metadata.feature_detail(),
        )

    def _existing_collection(self, identifier: str) -> CollectionEntry | None:
        """Read the catalog entry of a collection, or None when this write is the first one."""
        entry = self._catalog.get_entry(identifier)
        if entry is None:
            return None
        if not isinstance(entry.record, FeatureDataset):
            raise ItemTypeMismatchError(f"dataset {identifier!r} is not a vector collection")
        return CollectionEntry(record=entry.record, revision=entry.revision)

    def _collection_entry(self, identifier: str) -> CollectionEntry:
        """Read the catalog entry of a collection, including one whose deletion is under way."""
        entry = self._catalog.require_entry(identifier)
        if not isinstance(entry.record, FeatureDataset):
            raise ItemTypeMismatchError(f"dataset {identifier!r} is not a vector collection")
        return CollectionEntry(record=entry.record, revision=entry.revision)

    def _require_collection(self, identifier: str) -> CollectionEntry:
        """Read the catalog entry of a live collection, refusing another item type or a deletion under way."""
        entry = self._collection_entry(identifier)
        if entry.record.is_deleting:
            raise DatasetNotFoundError(f"collection {identifier!r} is being deleted")
        return entry

    def _sweep(self, identifier: str) -> int:
        """Delete every object below the prefix of a collection and forget the pointer etag of this thread."""
        removed = self._backend.delete_prefix(self._backend.address(vector_prefix(identifier)))
        self._pointer_etags.pop(identifier, None)
        return removed

    def _reserve_deletion(self, entry: CollectionEntry) -> str | None:
        """Mark a record as deleting under compare-and-swap, or report that another deleter got there first."""
        identifier = entry.record.dataset_identifier
        if entry.record.is_deleting:
            return entry.revision
        marked = entry.record.model_copy(
            update={"lifecycle": DatasetLifecycle.DELETING, "updated_at": current_timestamp()},
        )
        try:
            self._catalog.put(marked, revision=entry.revision)
        except PublicationConflictError:
            # Another deleter marked it first, or a writer changed it. Sweeping is still correct and
            # idempotent, but dropping a record this call never reserved is not, so it is left alone.
            return None
        reserved = self._catalog.get_entry(identifier)
        return None if reserved is None else reserved.revision

    def _drop_reserved_record(self, identifier: str, reserved: str | None) -> None:
        """Delete the record of a swept collection, leaving alone one that was reclaimed in the meantime."""
        if reserved is None:
            return
        entry = self._catalog.get_entry(identifier)
        # Object stores offer no conditional delete, so this is a read followed by a delete rather than
        # one compare-and-swap. It refuses the outcome that matters: erasing the record of a collection
        # that was written again under the same identifier while this deletion was sweeping.
        if entry is None or entry.revision != reserved:
            return
        self._catalog.delete(identifier)

    def _complete_deletion(self, entry: CollectionEntry) -> None:
        """Finish a deletion that stopped half way, so the caller can start from an empty prefix."""
        identifier = entry.record.dataset_identifier
        self._sweep(identifier)
        self._drop_reserved_record(identifier, entry.revision)

    def _guard_unqualified_read(
        self,
        identifier: str,
        metadata: VectorVersionMetadata,
        *,
        bbox: BoundingBox | None,
        where: WhereClause | None,
    ) -> None:
        """Refuse a read that carries neither an envelope nor a clause when the selected version is too large."""
        if bbox is not None or where:
            return
        threshold = self._settings.max_unqualified_feature_count
        if metadata.feature_count > threshold:
            raise FeatureCountGuardError(
                f"collection {identifier!r} holds {metadata.feature_count} features; "
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
        require_crs(bbox_crs, label="bbox-crs")
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
        metadata: VectorVersionMetadata,
        schema: pyarrow.Schema,
    ) -> list[str]:
        """Return the columns to read, always keeping the identifier property and the geometry column."""
        selected: list[str] = []
        for column in (*columns, metadata.identifier_property, metadata.primary_geometry):
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
        stored = read_object(self._backend.object_store(), key)
        if stored is None:
            self._pointer_etags.pop(identifier, None)
            return None
        self._remember_pointer_etag(identifier, stored.revision)
        return VectorCollectionPointer.model_validate_json(stored.payload)

    def _write_pointer(self, identifier: str, metadata: VectorVersionMetadata) -> VectorCollectionPointer:
        """Write the pointer object of a collection under compare-and-swap against the etag last read."""
        pointer = VectorCollectionPointer(
            version=metadata.version,
            key=self._backend.address(vector_data_key(identifier, metadata.version)).key,
            feature_count=metadata.feature_count,
            published_at=current_timestamp(),
        )
        key = self._pointer_address(identifier).key
        store = self._backend.object_store()
        payload = pointer.model_dump_json().encode(OBJECT_ENCODING)
        known_etag = self._pointer_etags.get(identifier)
        if known_etag is None:
            result = create_object(store, key, payload, label=POINTER_LABEL)
        else:
            result = replace_object(store, key, payload, known_etag, label=POINTER_LABEL)
        self._remember_pointer_etag(identifier, result.get("e_tag"))
        return pointer

    def _remember_pointer_etag(self, identifier: str, etag: str | None) -> None:
        """Record or drop the etag last seen for the pointer object of one collection."""
        if etag is None:
            self._pointer_etags.pop(identifier, None)
        else:
            self._pointer_etags[identifier] = etag
