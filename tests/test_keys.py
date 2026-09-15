"""Tests for the object key layout."""

from __future__ import annotations

import pytest

from ocs_storage_exploration.storage.errors import StorageAddressError
from ocs_storage_exploration.storage.keys import (
    catalog_record_key,
    dataset_identifier_from_catalog_key,
    format_vector_version,
    parse_vector_version,
    raster_prefix,
    validate_dataset_identifier,
    vector_data_key,
    vector_pointer_key,
    vector_prefix,
    vector_version_prefix,
)


@pytest.mark.parametrize("identifier", ["a", "one", "one-two", "one_two", "0abc", "a" * 128])
def test_valid_identifiers_are_accepted(identifier: str) -> None:
    assert validate_dataset_identifier(identifier) == identifier


@pytest.mark.parametrize(
    "identifier",
    ["", "-leading", "_leading", "Upper", "with space", "with.dot", "with/slash", "a" * 129],
)
def test_invalid_identifiers_are_rejected(identifier: str) -> None:
    with pytest.raises(StorageAddressError):
        validate_dataset_identifier(identifier)


def test_catalog_keys_round_trip() -> None:
    key = catalog_record_key("temperature")

    assert key == "catalog/datasets/temperature.json"
    assert dataset_identifier_from_catalog_key(key) == "temperature"


def test_catalog_key_parsing_rejects_other_objects() -> None:
    with pytest.raises(StorageAddressError):
        dataset_identifier_from_catalog_key("catalog/datasets/temperature.parquet")


def test_raster_and_vector_prefixes() -> None:
    assert raster_prefix("temperature") == "raster/temperature"
    assert vector_prefix("districts") == "vector/districts"
    assert vector_pointer_key("districts") == "vector/districts/current.json"


def test_vector_versions_are_zero_padded() -> None:
    assert format_vector_version(1) == "v00001"
    assert format_vector_version(42) == "v00042"
    assert vector_version_prefix("districts", 1) == "vector/districts/versions/v00001"
    assert vector_data_key("districts", 7) == "vector/districts/versions/v00007/data.parquet"


def test_vector_versions_sort_lexicographically() -> None:
    names = [format_vector_version(version) for version in (10, 2, 1)]

    assert sorted(names) == ["v00001", "v00002", "v00010"]


def test_vector_version_round_trip() -> None:
    assert parse_vector_version(format_vector_version(123)) == 123


@pytest.mark.parametrize("name", ["v1", "v000001", "00001", "vabcde", "v00000"])
def test_invalid_vector_version_names_are_rejected(name: str) -> None:
    with pytest.raises(StorageAddressError):
        parse_vector_version(name)


@pytest.mark.parametrize("version", [0, -1, 100_000])
def test_out_of_range_vector_versions_are_rejected(version: int) -> None:
    with pytest.raises(StorageAddressError):
        format_vector_version(version)
