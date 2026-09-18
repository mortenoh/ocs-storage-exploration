"""Catalog record schemas, the tagged dataset union and operation result schemas."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ocs_storage_exploration.storage.addresses import SchemeName, validate_object_key
from ocs_storage_exploration.storage.errors import StorageAddressError

# A loose shape check rather than a lookup against the SPDX list: one SPDX-style identifier, or several joined
# by the SPDX operators. It accepts "CC-BY-4.0" and "proprietary" and refuses a sentence of prose,
# which is the only mistake worth catching before the value reaches a STAC client.
LICENSE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9.+-]*(?: (?:AND|OR|WITH) [A-Za-z0-9][A-Za-z0-9.+-]*)*$",
)


def current_timestamp() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def normalise_license(value: str | None) -> str | None:
    """Trim a licence identifier and refuse anything that is not an SPDX identifier or expression."""
    if value is None:
        return None
    cleaned = value.strip()
    if not LICENSE_PATTERN.match(cleaned):
        raise ValueError(f"license must be an SPDX identifier or expression, or 'proprietary': {value!r}")
    return cleaned


def normalise_attribution(value: str | None) -> str | None:
    """Trim an attribution string, treating a blank one as absent."""
    if value is None:
        return None
    return value.strip() or None


def normalise_storage_key(value: str) -> str:
    """Refuse a storage key that is not a plain object key relative to the base prefix of a backend."""
    try:
        return validate_object_key(value)
    except StorageAddressError as error:
        raise ValueError(f"storage key must be a relative object key: {value!r}") from error


class ItemType(StrEnum):
    """Discriminator separating gridded coverages from vector features."""

    COVERAGE = "coverage"
    FEATURE = "feature"


class DatasetLifecycle(StrEnum):
    """Whether a record stands for a dataset that exists, for one being deleted, or for one that is gone."""

    LIVE = "live"
    DELETING = "deleting"
    DELETED = "deleted"


class StorageFormat(StrEnum):
    """On-disk format a dataset is stored in."""

    ICECHUNK = "icechunk"
    GEOPARQUET = "geoparquet"


class BoundingBox(BaseModel):
    """Axis-aligned spatial envelope in the coordinate reference system of its owner."""

    model_config = ConfigDict(frozen=True)

    minimum_x: float
    minimum_y: float
    maximum_x: float
    maximum_y: float

    @model_validator(mode="after")
    def validate_ordering(self) -> Self:
        """Reject envelopes whose maximum is not greater than its minimum."""
        if self.maximum_x <= self.minimum_x:
            raise ValueError("maximum_x must be greater than minimum_x")
        if self.maximum_y <= self.minimum_y:
            raise ValueError("maximum_y must be greater than minimum_y")
        return self

    @classmethod
    def from_sequence(cls, values: tuple[float, float, float, float]) -> BoundingBox:
        """Build an envelope from a minimum_x, minimum_y, maximum_x, maximum_y sequence."""
        return cls(minimum_x=values[0], minimum_y=values[1], maximum_x=values[2], maximum_y=values[3])

    def as_tuple(self) -> tuple[float, float, float, float]:
        """Return the envelope as a minimum_x, minimum_y, maximum_x, maximum_y tuple."""
        return (self.minimum_x, self.minimum_y, self.maximum_x, self.maximum_y)


class TemporalExtent(BaseModel):
    """Inclusive time span covered by a dataset."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_ordering(self) -> Self:
        """Reject spans that end before they start."""
        if self.end < self.start:
            raise ValueError("end must not be earlier than start")
        return self


class GridSpecification(BaseModel):
    """Regular grid a coverage is written on, including its dimension names."""

    shape: tuple[int, int]
    bbox: BoundingBox
    crs: str
    data_type: str = "float32"
    nodata_value: float | None = None
    time_dimension: str = "t"
    y_dimension: str = "y"
    x_dimension: str = "x"
    attributes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_grid(self) -> Self:
        """Reject non-positive shapes and non-finite fill values."""
        rows, columns = self.shape
        if rows < 1 or columns < 1:
            raise ValueError("shape must be positive in both dimensions")
        if self.nodata_value is not None and not math.isfinite(self.nodata_value):
            raise ValueError("nodata_value must be finite because Icechunk rejects non-finite attributes")
        return self


class FeatureDetail(BaseModel):
    """Feature-level metadata of a vector collection."""

    identifier_property: str
    feature_count: int = Field(default=0, ge=0)
    primary_geometry: str = "geometry"
    geometry_types: tuple[str, ...] = ()
    selectable_columns: tuple[str, ...] = ()


class Publication(BaseModel):
    """Pointer state describing what is currently published for a dataset."""

    published: bool = False
    published_at: datetime | None = None
    snapshot_identifier: str | None = None
    version: int | None = None
    previous_snapshot_identifier: str | None = None
    previous_version: int | None = None


class DatasetBase(BaseModel):
    """Fields shared by every catalog record regardless of item type."""

    model_config = ConfigDict(extra="forbid")

    dataset_identifier: str
    title: str
    # The key of the dataset below the base prefix of the backend, which is what backend.address takes.
    # A record holding an absolute URI would name the root it happened to be written under, so the same
    # bytes served from a container, from another machine or from S3 would advertise a root that is gone.
    storage_key: str
    schema_version: str = "1"
    # A deletion marks the record before it sweeps the objects, so the record is what tells a
    # concurrent writer that the prefix it is about to write into is being emptied, and it tombstones
    # the record afterwards rather than deleting it, so every transition is a compare-and-swap.
    lifecycle: DatasetLifecycle = DatasetLifecycle.LIVE
    created_at: datetime = Field(default_factory=current_timestamp)
    updated_at: datetime = Field(default_factory=current_timestamp)
    bbox: BoundingBox | None = None
    license: str | None = None
    attribution: str | None = None
    publication: Publication = Field(default_factory=Publication)

    @property
    def is_live(self) -> bool:
        """Report whether this record stands for a dataset a reader is allowed to see."""
        return self.lifecycle is DatasetLifecycle.LIVE

    @property
    def is_deleting(self) -> bool:
        """Report whether this record stands for a deletion under way rather than a live dataset."""
        return self.lifecycle is DatasetLifecycle.DELETING

    @property
    def is_tombstone(self) -> bool:
        """Report whether this record is the tombstone a finished deletion left behind."""
        return self.lifecycle is DatasetLifecycle.DELETED

    @field_validator("storage_key")
    @classmethod
    def validate_storage_key(cls, value: str) -> str:
        """Refuse a storage key that is absolute or climbs out of the backend it is read against."""
        return normalise_storage_key(value)

    @field_validator("license")
    @classmethod
    def validate_license(cls, value: str | None) -> str | None:
        """Refuse a licence that is neither an SPDX identifier nor an SPDX expression."""
        return normalise_license(value)

    @field_validator("attribution")
    @classmethod
    def validate_attribution(cls, value: str | None) -> str | None:
        """Trim the attribution string, treating a blank one as absent."""
        return normalise_attribution(value)


class CoverageDataset(DatasetBase):
    """Catalog record of a gridded coverage stored as Icechunk-backed GeoZarr."""

    item_type: Literal[ItemType.COVERAGE] = ItemType.COVERAGE
    storage_format: Literal[StorageFormat.ICECHUNK] = StorageFormat.ICECHUNK
    grid: GridSpecification
    variables: tuple[str, ...] = ()
    temporal: TemporalExtent | None = None
    timestep_count: int = Field(default=0, ge=0)


class FeatureDataset(DatasetBase):
    """Catalog record of a vector collection stored as versioned GeoParquet."""

    item_type: Literal[ItemType.FEATURE] = ItemType.FEATURE
    storage_format: Literal[StorageFormat.GEOPARQUET] = StorageFormat.GEOPARQUET
    crs: str
    features: FeatureDetail


Dataset = Annotated[CoverageDataset | FeatureDataset, Field(discriminator="item_type")]


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """A dataset record together with the revision of the catalog object it was read from."""

    record: Dataset
    revision: str


class VectorVersionMetadata(BaseModel):
    """Sidecar describing one immutable version of a vector collection as that version was written."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    crs: str
    feature_count: int = Field(default=0, ge=0)
    identifier_property: str
    primary_geometry: str = "geometry"
    geometry_types: tuple[str, ...] = ()
    selectable_columns: tuple[str, ...] = ()
    bbox: BoundingBox | None = None
    written_at: datetime = Field(default_factory=current_timestamp)
    license: str | None = None
    attribution: str | None = None

    @field_validator("license")
    @classmethod
    def validate_license(cls, value: str | None) -> str | None:
        """Refuse a licence that is neither an SPDX identifier nor an SPDX expression."""
        return normalise_license(value)

    @field_validator("attribution")
    @classmethod
    def validate_attribution(cls, value: str | None) -> str | None:
        """Trim the attribution string, treating a blank one as absent."""
        return normalise_attribution(value)

    def feature_detail(self) -> FeatureDetail:
        """Render this sidecar as the feature detail block a catalog record carries."""
        return FeatureDetail(
            identifier_property=self.identifier_property,
            feature_count=self.feature_count,
            primary_geometry=self.primary_geometry,
            geometry_types=self.geometry_types,
            selectable_columns=self.selectable_columns,
        )


class RasterVersion(BaseModel):
    """One snapshot in the ancestry of a raster repository."""

    snapshot_identifier: str
    message: str
    written_at: datetime
    is_published: bool = False


class RasterWriteResult(BaseModel):
    """Outcome of creating or appending to a raster dataset."""

    dataset_identifier: str
    snapshot_identifier: str
    timestep_count: int
    variables: tuple[str, ...]
    published: bool = False


class RasterIngestResult(RasterWriteResult):
    """Outcome of ingesting real raster files, naming the files that were read and the timesteps they carried."""

    files: tuple[str, ...] = ()
    timestamps: tuple[datetime, ...] = ()


class RasterQuerySummary(BaseModel):
    """Summary statistics of a windowed raster query."""

    dataset_identifier: str
    variable: str
    bbox: BoundingBox
    crs: str
    snapshot_identifier: str
    timestep_count: int
    cell_count: int
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None


class PublicationResult(BaseModel):
    """Outcome of moving the published pointer of a dataset."""

    dataset_identifier: str
    item_type: ItemType
    published: bool
    changed: bool = True
    snapshot_identifier: str | None = None
    version: int | None = None
    previous_snapshot_identifier: str | None = None
    previous_version: int | None = None


class VectorWriteResult(BaseModel):
    """Outcome of writing a new version of a vector collection."""

    dataset_identifier: str
    version: int
    feature_count: int
    published: bool = False


class BackendDescription(BaseModel):
    """Public description of a storage backend that never carries secrets."""

    scheme: SchemeName
    root: str
    base_prefix: str
    available: bool = True
    supports_parquet_filesystem: bool = True
    details: dict[str, str] = Field(default_factory=dict)
