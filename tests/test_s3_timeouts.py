"""Tests that an unreachable S3 endpoint fails fast with a storage error rather than hanging."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import geopandas
import pytest
from pydantic import SecretStr

from ocs_storage_exploration.settings import ObjectStorageSettings, Settings
from ocs_storage_exploration.storage.addresses import StorageScheme
from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import BackendUnavailableError
from ocs_storage_exploration.storage.keys import vector_data_key, vector_generation_prefix
from ocs_storage_exploration.storage.raster import TimeStep, build_synthetic_cube, build_timestamps
from ocs_storage_exploration.storage.schemas import (
    BoundingBox,
    CoverageDataset,
    GridSpecification,
    TemporalExtent,
)
from ocs_storage_exploration.storage.service import StorageService

pytestmark = pytest.mark.s3

# Port 9 is the discard service; nothing listens on it, so every connection is refused at once.
UNREACHABLE_ENDPOINT = "http://127.0.0.1:9"
# Generous against the sub-second connect timeout below: the test guards against an unbounded client,
# not against a slow one.
BOUNDED_SECONDS = 10.0
COVERAGE = "unreachable-coverage"
COLLECTION = "unreachable-collection"
GENERATION = "0123456789abcdef0123456789abcdef"

BBOX = BoundingBox(minimum_x=0.0, minimum_y=0.0, maximum_x=12.0, maximum_y=8.0)


@pytest.fixture
def unreachable_service() -> StorageService:
    options = ObjectStorageSettings(
        bucket="ocs-storage-exploration",
        prefix="unreachable",
        region="us-east-1",
        endpoint_url=UNREACHABLE_ENDPOINT,
        allow_http=True,
        access_key_id="unreachable",
        secret_access_key=SecretStr("unreachable"),
        force_path_style=True,
        connect_timeout_seconds=0.5,
        request_timeout_seconds=1.0,
        max_retries=1,
        retry_backoff_seconds=0.1,
    )
    return StorageService.from_settings(
        Settings(backend=StorageScheme.S3, base_prefix="unreachable", s3=options),
    )


def assert_fails_quickly(operation: Callable[[], Any]) -> BackendUnavailableError:
    started = time.perf_counter()
    with pytest.raises(BackendUnavailableError) as failure:
        operation()
    elapsed = time.perf_counter() - started

    assert elapsed < BOUNDED_SECONDS, f"the call took {elapsed:.1f}s, so the client is not bounded"
    assert failure.value.status_code == 503
    assert failure.value.message
    return failure.value


def test_a_raster_create_gives_up_instead_of_hanging(unreachable_service: StorageService) -> None:
    grid = GridSpecification(shape=(4, 6), bbox=BBOX, crs="EPSG:4326")
    cube = build_synthetic_cube(
        grid,
        variable="temperature",
        timestamps=build_timestamps(datetime(2020, 1, 1), 1, TimeStep.DAY),
    )

    failure = assert_fails_quickly(lambda: unreachable_service.raster.create(COVERAGE, grid, cube))

    assert "could not reach the object store" in failure.message


def test_a_catalog_put_gives_up_instead_of_hanging(unreachable_service: StorageService) -> None:
    catalog = ObjectCatalog(unreachable_service.backend)
    record = CoverageDataset(
        dataset_identifier=COVERAGE,
        title="Unreachable",
        storage_key=f"raster/{COVERAGE}",
        bbox=BBOX,
        grid=GridSpecification(shape=(4, 6), bbox=BBOX, crs="EPSG:4326"),
        variables=("temperature",),
        temporal=TemporalExtent(start=datetime(2020, 1, 1, tzinfo=UTC), end=datetime(2020, 1, 2, tzinfo=UTC)),
        timestep_count=1,
    )

    assert_fails_quickly(lambda: catalog.put(record, create=True))


def test_a_parquet_write_gives_up_instead_of_hanging(
    unreachable_service: StorageService, sample_features: geopandas.GeoDataFrame
) -> None:
    address = unreachable_service.backend.address(vector_data_key(vector_generation_prefix(COLLECTION, GENERATION), 1))

    # The public write reads the catalog record first, so both the obstore path and the pyarrow path
    # of one vector write are asserted to be bounded.
    assert_fails_quickly(
        lambda: unreachable_service.vector.write(COLLECTION, sample_features, identifier_property="id"),
    )
    assert_fails_quickly(lambda: unreachable_service.vector._write_parquet(address, sample_features))
