"""Tests for the catalog record schemas and the discriminated dataset union."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from ocs_storage_exploration.storage.addresses import StorageScheme
from ocs_storage_exploration.storage.schemas import (
    BackendDescription,
    BoundingBox,
    CoverageDataset,
    Dataset,
    DatasetLifecycle,
    FeatureDataset,
    FeatureDetail,
    GridSpecification,
    ItemType,
    Publication,
    StorageFormat,
    TemporalExtent,
)

DATASET_ADAPTER: TypeAdapter[Dataset] = TypeAdapter(Dataset)

BBOX = BoundingBox(minimum_x=-10.0, minimum_y=-5.0, maximum_x=10.0, maximum_y=5.0)


def build_coverage() -> CoverageDataset:
    return CoverageDataset(
        dataset_identifier="temperature",
        title="Daily temperature",
        storage_key="raster/temperature",
        bbox=BBOX,
        grid=GridSpecification(shape=(4, 8), bbox=BBOX, crs="EPSG:4326", nodata_value=-9999.0),
        variables=("temperature",),
        temporal=TemporalExtent(start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 3, tzinfo=UTC)),
        timestep_count=3,
    )


def build_feature() -> FeatureDataset:
    return FeatureDataset(
        dataset_identifier="districts",
        title="Districts",
        storage_key="vector/districts",
        bbox=BBOX,
        crs="EPSG:4326",
        features=FeatureDetail(
            identifier_property="id",
            feature_count=12,
            geometry_types=("Polygon", "Point"),
            selectable_columns=("id", "name"),
        ),
        publication=Publication(published=True, version=1, published_at=datetime(2026, 1, 4, tzinfo=UTC)),
    )


def test_coverage_defaults_are_the_icechunk_contract() -> None:
    coverage = build_coverage()

    assert coverage.item_type is ItemType.COVERAGE
    assert coverage.storage_format is StorageFormat.ICECHUNK
    assert coverage.grid.time_dimension == "t"
    assert coverage.grid.y_dimension == "y"
    assert coverage.grid.x_dimension == "x"
    assert coverage.schema_version == "1"
    assert coverage.publication.published is False


def test_feature_defaults_are_the_geoparquet_contract() -> None:
    feature = build_feature()

    assert feature.item_type is ItemType.FEATURE
    assert feature.storage_format is StorageFormat.GEOPARQUET
    assert feature.features.primary_geometry == "geometry"


@pytest.mark.parametrize("builder", [build_coverage, build_feature], ids=["coverage", "feature"])
def test_union_survives_a_json_round_trip(builder: Callable[[], Dataset]) -> None:
    dataset = builder()

    payload = DATASET_ADAPTER.dump_json(dataset)
    restored = DATASET_ADAPTER.validate_json(payload)

    assert restored == dataset
    assert type(restored) is type(dataset)


def test_discriminator_selects_the_class() -> None:
    coverage_payload = DATASET_ADAPTER.dump_json(build_coverage())
    feature_payload = DATASET_ADAPTER.dump_json(build_feature())

    assert isinstance(DATASET_ADAPTER.validate_json(coverage_payload), CoverageDataset)
    assert isinstance(DATASET_ADAPTER.validate_json(feature_payload), FeatureDataset)


def test_unknown_item_type_is_rejected() -> None:
    payload = DATASET_ADAPTER.dump_python(build_coverage(), mode="json")
    payload["item_type"] = "mosaic"

    with pytest.raises(ValidationError):
        DATASET_ADAPTER.validate_python(payload)


def test_extra_fields_are_forbidden() -> None:
    payload = DATASET_ADAPTER.dump_python(build_coverage(), mode="json")
    payload["unexpected"] = 1

    with pytest.raises(ValidationError):
        DATASET_ADAPTER.validate_python(payload)


@pytest.mark.parametrize("nodata_value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_nodata_is_rejected(nodata_value: float) -> None:
    with pytest.raises(ValidationError):
        GridSpecification(shape=(2, 2), bbox=BBOX, crs="EPSG:4326", nodata_value=nodata_value)


@pytest.mark.parametrize("shape", [(0, 2), (2, 0), (-1, 2)])
def test_non_positive_shapes_are_rejected(shape: tuple[int, int]) -> None:
    with pytest.raises(ValidationError):
        GridSpecification(shape=shape, bbox=BBOX, crs="EPSG:4326")


def test_bounding_box_ordering_is_validated() -> None:
    with pytest.raises(ValidationError):
        BoundingBox(minimum_x=10.0, minimum_y=0.0, maximum_x=-10.0, maximum_y=5.0)


def test_bounding_box_sequence_round_trip() -> None:
    assert BoundingBox.from_sequence(BBOX.as_tuple()) == BBOX


def test_temporal_extent_ordering_is_validated() -> None:
    with pytest.raises(ValidationError):
        TemporalExtent(start=datetime(2026, 1, 3, tzinfo=UTC), end=datetime(2026, 1, 1, tzinfo=UTC))


def test_backend_description_defaults_hold_no_secret() -> None:
    description = BackendDescription(scheme=StorageScheme.MEMORY, root="memory", base_prefix="ocs")

    assert description.available is True
    assert description.details == {}


@pytest.mark.parametrize("value", ["CC-BY-4.0", "proprietary", "Apache-2.0 OR MIT", "GPL-2.0-only WITH Classpath-2.0"])
def test_an_spdx_licence_is_accepted(value: str) -> None:
    record = build_coverage().model_copy(update={"license": value})

    assert CoverageDataset.model_validate(record.model_dump()).license == value


@pytest.mark.parametrize("value", ["Creative Commons Attribution 4.0", "", "  ", "cc by/4.0"])
def test_free_text_is_refused_as_a_licence(value: str) -> None:
    payload = build_coverage().model_dump()
    payload["license"] = value

    with pytest.raises(ValidationError):
        CoverageDataset.model_validate(payload)


def test_a_blank_attribution_is_recorded_as_absent() -> None:
    payload = build_coverage().model_dump()
    payload["attribution"] = "  Open Climate Service  "

    assert CoverageDataset.model_validate(payload).attribution == "Open Climate Service"

    payload["attribution"] = "   "
    assert CoverageDataset.model_validate(payload).attribution is None


def test_the_licence_and_attribution_default_to_absent() -> None:
    coverage = build_coverage()

    assert coverage.license is None
    assert coverage.attribution is None


def test_a_record_is_live_unless_a_deletion_marked_it() -> None:
    record = build_coverage()

    assert record.lifecycle is DatasetLifecycle.LIVE
    assert record.is_deleting is False


def test_a_deleting_record_round_trips_through_the_dataset_union() -> None:
    marked = build_feature().model_copy(update={"lifecycle": DatasetLifecycle.DELETING})

    restored = DATASET_ADAPTER.validate_json(DATASET_ADAPTER.dump_json(marked))

    assert restored.lifecycle is DatasetLifecycle.DELETING
    assert restored.is_deleting is True
    assert DATASET_ADAPTER.dump_python(marked, mode="json")["lifecycle"] == "deleting"


def test_an_unknown_lifecycle_is_refused() -> None:
    payload = DATASET_ADAPTER.dump_python(build_feature(), mode="json") | {"lifecycle": "half-deleted"}

    with pytest.raises(ValidationError):
        DATASET_ADAPTER.validate_python(payload)


@pytest.mark.parametrize(
    "storage_key",
    ["/raster/temperature", "raster/../../escape", "raster//temperature", ""],
    ids=["absolute", "climbs-out", "empty-segment", "empty"],
)
def test_a_storage_key_that_is_not_a_relative_object_key_is_refused(storage_key: str) -> None:
    payload = build_coverage().model_dump()
    payload["storage_key"] = storage_key

    with pytest.raises(ValidationError, match="storage key"):
        CoverageDataset.model_validate(payload)
