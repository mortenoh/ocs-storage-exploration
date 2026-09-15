"""Request and response models of the HTTP API."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal, Self

import xarray
from pydantic import BaseModel, Field, field_validator, model_validator
from pyproj import CRS
from pyproj.exceptions import CRSError

from ocs_storage_exploration.storage.addresses import StorageScheme
from ocs_storage_exploration.storage.errors import PublicationSelectorError, RasterContractError
from ocs_storage_exploration.storage.models import (
    BackendDescription,
    BoundingBox,
    CoverageDataset,
    Dataset,
    GridSpecification,
    RasterVersion,
)
from ocs_storage_exploration.storage.raster import TimeStep, build_synthetic_cube, build_timestamps
from ocs_storage_exploration.storage.vector import DEFAULT_CRS, VectorReadHandle, crs_identifier

TIME_STEP_ATTRIBUTE: Final[str] = "time_step"
TIME_STEP_VALUES: Final[frozenset[str]] = frozenset(step.value for step in TimeStep)
MAXIMUM_TIMESTEP_COUNT: Final[int] = 512
MAXIMUM_GRID_SIDE: Final[int] = 4096
WORLD_BBOX: Final[BoundingBox] = BoundingBox(minimum_x=-180.0, minimum_y=-90.0, maximum_x=180.0, maximum_y=90.0)
DEFAULT_START_TIME: Final[datetime] = datetime(2020, 1, 1, tzinfo=UTC)

GridSide = Annotated[int, Field(ge=1, le=MAXIMUM_GRID_SIDE)]


def validate_crs(value: str) -> str:
    """Refuse a coordinate reference system that pyproj cannot parse."""
    try:
        CRS.from_user_input(value)
    except CRSError as error:
        raise ValueError(f"unknown coordinate reference system: {value!r}") from error
    return value


def resolve_time_step(value: str | None) -> TimeStep:
    """Resolve the time step recorded on a grid, falling back to a daily step."""
    if value in TIME_STEP_VALUES:
        return TimeStep(value)
    return TimeStep.DAY


class HealthResponse(BaseModel):
    """Health probe response naming the service version and the active storage backend."""

    status: Literal["ok"] = "ok"
    version: str
    backend: StorageScheme


class CreateRasterRequest(BaseModel):
    """Request describing the synthetic coverage the service generates and writes."""

    title: str | None = None
    shape: tuple[GridSide, GridSide] = (16, 32)
    bbox: BoundingBox = WORLD_BBOX
    crs: str = DEFAULT_CRS
    variable: str = Field(default="value", min_length=1)
    timestep_count: int = Field(default=3, ge=1, le=MAXIMUM_TIMESTEP_COUNT)
    start_time: datetime = DEFAULT_START_TIME
    step: TimeStep = TimeStep.MONTH
    data_type: Literal["float32", "float64", "int16"] = "float32"
    nodata_value: float | None = Field(default=None, allow_inf_nan=False)
    seed: int = 0
    overwrite: bool = False
    publish: bool = False

    @field_validator("crs")
    @classmethod
    def check_crs(cls, value: str) -> str:
        """Refuse a coordinate reference system that pyproj cannot parse."""
        return validate_crs(value)

    def to_grid(self) -> GridSpecification:
        """Build the grid of this request, recording the time step so an append can continue the axis."""
        return GridSpecification(
            shape=self.shape,
            bbox=self.bbox,
            crs=self.crs,
            data_type=self.data_type,
            nodata_value=self.nodata_value,
            attributes={TIME_STEP_ATTRIBUTE: self.step.value},
        )

    def to_cube(self, grid: GridSpecification) -> xarray.Dataset:
        """Build the synthetic cube of this request on one grid."""
        timestamps = build_timestamps(self.start_time, self.timestep_count, self.step)
        return build_synthetic_cube(grid, variable=self.variable, timestamps=timestamps, seed=self.seed)


class AppendRasterRequest(BaseModel):
    """Request describing how many timesteps to generate and append to an existing coverage."""

    timestep_count: int = Field(default=1, ge=1, le=MAXIMUM_TIMESTEP_COUNT)
    seed: int = 0
    publish: bool = False

    def to_cube(self, record: CoverageDataset) -> xarray.Dataset:
        """Build the cube that continues the time axis of a coverage with the step its grid records."""
        if record.temporal is None or not record.variables:
            raise RasterContractError(f"coverage {record.dataset_identifier!r} has no time axis to continue")
        step = resolve_time_step(record.grid.attributes.get(TIME_STEP_ATTRIBUTE))
        # The axis is regular, so the continuation is the tail of the same series rebuilt one span longer.
        timestamps = build_timestamps(record.temporal.start, record.timestep_count + self.timestep_count, step)
        return build_synthetic_cube(
            record.grid,
            variable=record.variables[0],
            timestamps=timestamps[record.timestep_count :],
            seed=self.seed,
        )


class PublishRequest(BaseModel):
    """Request naming what to publish: a snapshot for a coverage or a version for a collection."""

    snapshot_identifier: str | None = None
    version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_selector(self) -> Self:
        """Refuse a request that names both a snapshot and a version."""
        if self.snapshot_identifier is not None and self.version is not None:
            raise ValueError("publish takes either snapshot_identifier or version, not both")
        return self

    def coverage_snapshot(self) -> str | None:
        """Return the snapshot a coverage publication targets, refusing the collection selector."""
        if self.version is not None:
            raise PublicationSelectorError("a coverage is published by snapshot_identifier, not by version")
        return self.snapshot_identifier

    def collection_version(self) -> int | None:
        """Return the version a collection publication targets, refusing the coverage selector."""
        if self.snapshot_identifier is not None:
            raise PublicationSelectorError("a collection is published by version, not by snapshot_identifier")
        return self.version


class CreateVectorRequest(BaseModel):
    """Request carrying the GeoJSON FeatureCollection a vector collection version is written from."""

    title: str | None = None
    identifier_property: str = Field(default="id", min_length=1)
    crs: str = DEFAULT_CRS
    selectable_columns: tuple[str, ...] = ()
    publish: bool = False
    feature_collection: dict[str, Any]

    @field_validator("crs")
    @classmethod
    def check_crs(cls, value: str) -> str:
        """Refuse a coordinate reference system that pyproj cannot parse."""
        return validate_crs(value)


class FeatureCollectionResponse(BaseModel):
    """GeoJSON FeatureCollection answered in the coordinate reference system of the collection."""

    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[dict[str, Any]]
    number_returned: int
    number_matched: int | None = None
    version: int
    crs: str
    truncated: bool = False

    @classmethod
    def from_handle(cls, handle: VectorReadHandle, *, limit: int | None = None) -> FeatureCollectionResponse:
        """Render a read handle as GeoJSON, keeping the coordinates in the frame of the collection."""
        payload: dict[str, Any] = json.loads(handle.frame.to_json())
        features: list[dict[str, Any]] = list(payload["features"])
        # A read that returns exactly as many features as the limit cannot be told from a full page.
        truncated = limit is not None and len(features) >= limit
        return cls(
            features=features,
            number_returned=len(features),
            number_matched=None if truncated else len(features),
            version=handle.version,
            crs=crs_identifier(handle.frame.crs) if handle.frame.crs is not None else DEFAULT_CRS,
            truncated=truncated,
        )


class DatasetListResponse(BaseModel):
    """List of catalog records, optionally filtered by item type."""

    items: list[Dataset]


class BackendListResponse(BaseModel):
    """List of backend descriptions, the active backend first."""

    items: list[BackendDescription]


class RasterVersionListResponse(BaseModel):
    """List of the snapshots in the history of one coverage, newest first."""

    items: list[RasterVersion]
