"""Catalog record models, the tagged dataset union and operation result models."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ocs_storage_exploration.storage.addresses import StorageScheme


def current_timestamp() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class ItemType(StrEnum):
    """Discriminator separating gridded coverages from vector features."""

    COVERAGE = "coverage"
    FEATURE = "feature"


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
    address: str
    schema_version: str = "1"
    created_at: datetime = Field(default_factory=current_timestamp)
    updated_at: datetime = Field(default_factory=current_timestamp)
    bbox: BoundingBox | None = None
    publication: Publication = Field(default_factory=Publication)


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

    scheme: StorageScheme
    root: str
    base_prefix: str
    available: bool = True
    supports_parquet_filesystem: bool = True
    details: dict[str, str] = Field(default_factory=dict)
