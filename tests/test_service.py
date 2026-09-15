"""Tests for the service layer that composes the backend, the catalog and both engines."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime

import geopandas
import pytest
from pydantic import SecretStr

from ocs_storage_exploration.settings import ObjectStorageSettings, Settings
from ocs_storage_exploration.storage.addresses import StorageScheme
from ocs_storage_exploration.storage.errors import DatasetNotFoundError, ItemTypeMismatchError
from ocs_storage_exploration.storage.keys import raster_prefix, vector_prefix
from ocs_storage_exploration.storage.models import (
    BoundingBox,
    CoverageDataset,
    FeatureDataset,
    GridSpecification,
    ItemType,
)
from ocs_storage_exploration.storage.raster import TimeStep, build_synthetic_cube, build_timestamps
from ocs_storage_exploration.storage.registry import registered_schemes
from ocs_storage_exploration.storage.service import INACTIVE_BACKEND_STATUS, StorageService

COVERAGE = "coverage-one"
COLLECTION = "collection-one"
VARIABLE = "temperature"
SECRET_VALUE = "rustfsadminsecret"


def build_grid() -> GridSpecification:
    return GridSpecification(
        shape=(4, 6),
        bbox=BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=12.0, maximum_y=8.0),
        crs="EPSG:4326",
    )


@pytest.fixture
def populated(storage_service: StorageService, sample_features: geopandas.GeoDataFrame) -> StorageService:
    grid = build_grid()
    cube = build_synthetic_cube(
        grid,
        variable=VARIABLE,
        timestamps=build_timestamps(datetime(2020, 1, 1), 2, TimeStep.DAY),
    )
    storage_service.raster.create(COVERAGE, grid, cube, title="Coverage one")
    storage_service.vector.write(COLLECTION, sample_features, identifier_property="id", title="Collection one")
    return storage_service


def test_from_settings_binds_one_backend_and_catalog_to_both_engines(storage_service: StorageService) -> None:
    assert storage_service.raster.backend is storage_service.backend
    assert storage_service.vector.backend is storage_service.backend
    assert storage_service.raster.catalog is storage_service.catalog
    assert storage_service.vector.catalog is storage_service.catalog
    assert storage_service.backend.scheme is storage_service.settings.backend


def test_the_service_is_frozen(storage_service: StorageService) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(storage_service, "settings", storage_service.settings)


def test_describe_backends_reports_the_active_backend_first(storage_service: StorageService) -> None:
    descriptions = storage_service.describe_backends()

    assert [description.scheme for description in descriptions][0] is storage_service.backend.scheme
    assert len(descriptions) == len(registered_schemes())
    assert descriptions[0].available is True
    assert all(description.available is False for description in descriptions[1:])
    assert all(description.details["status"] == INACTIVE_BACKEND_STATUS for description in descriptions[1:])


def test_describe_backends_never_touches_the_credentials_of_an_inactive_scheme(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "s3": ObjectStorageSettings(
                bucket="ocs-exploration",
                access_key_id="rustfsadmin",
                secret_access_key=SecretStr(SECRET_VALUE),
            ),
        },
    )
    service = StorageService.from_settings(configured)

    descriptions = service.describe_backends()
    inactive = next(description for description in descriptions if description.scheme is StorageScheme.S3)

    assert inactive.root == ""
    assert SECRET_VALUE not in str(inactive.model_dump())


def test_list_datasets_filters_by_item_type(populated: StorageService) -> None:
    assert {record.dataset_identifier for record in populated.list_datasets()} == {COVERAGE, COLLECTION}
    assert [record.dataset_identifier for record in populated.list_datasets(ItemType.COVERAGE)] == [COVERAGE]
    assert [record.dataset_identifier for record in populated.list_datasets(ItemType.FEATURE)] == [COLLECTION]


def test_get_dataset_reads_one_record(populated: StorageService) -> None:
    assert populated.get_dataset(COVERAGE).title == "Coverage one"

    with pytest.raises(DatasetNotFoundError):
        populated.get_dataset("absent")


def test_require_coverage_and_require_collection_refuse_the_other_item_type(populated: StorageService) -> None:
    assert isinstance(populated.require_coverage(COVERAGE), CoverageDataset)
    assert isinstance(populated.require_collection(COLLECTION), FeatureDataset)

    with pytest.raises(ItemTypeMismatchError):
        populated.require_coverage(COLLECTION)
    with pytest.raises(ItemTypeMismatchError):
        populated.require_collection(COVERAGE)


def test_delete_dataset_routes_a_coverage_to_the_raster_engine(populated: StorageService) -> None:
    deleted = populated.delete_dataset(COVERAGE)

    assert isinstance(deleted, CoverageDataset)
    assert populated.backend.list_keys(populated.backend.address(raster_prefix(COVERAGE))) == []
    assert [record.dataset_identifier for record in populated.list_datasets()] == [COLLECTION]


def test_delete_dataset_routes_a_collection_to_the_vector_engine(populated: StorageService) -> None:
    deleted = populated.delete_dataset(COLLECTION)

    assert isinstance(deleted, FeatureDataset)
    assert populated.backend.list_keys(populated.backend.address(vector_prefix(COLLECTION))) == []
    assert [record.dataset_identifier for record in populated.list_datasets()] == [COVERAGE]


def test_delete_dataset_reports_an_unknown_identifier(populated: StorageService) -> None:
    with pytest.raises(DatasetNotFoundError):
        populated.delete_dataset("absent")
