"""Tests for the object key layout."""

from __future__ import annotations

import pytest

from ocs_storage_exploration.storage.errors import StorageAddressError
from ocs_storage_exploration.storage.keys import (
    catalog_record_key,
    dataset_generation_prefix,
    dataset_identifier_from_catalog_key,
    format_vector_version,
    mint_generation_token,
    parse_vector_version,
    raster_generation_prefix,
    validate_dataset_identifier,
    validate_generation_token,
    vector_data_key,
    vector_generation_prefix,
    vector_pointer_key,
    vector_reservation_key,
    vector_version_metadata_key,
    vector_version_prefix,
    vector_versions_prefix,
)

GENERATION = "0123456789abcdef0123456789abcdef"
PREFIX = f"vector/districts/{GENERATION}"


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
    assert raster_generation_prefix("temperature", GENERATION) == f"raster/temperature/{GENERATION}"
    assert vector_generation_prefix("districts", GENERATION) == PREFIX
    assert dataset_generation_prefix("vector", "districts", GENERATION) == PREFIX


def test_a_minted_generation_token_is_key_safe_and_never_repeats() -> None:
    minted = {mint_generation_token() for _ in range(64)}

    assert len(minted) == 64
    for generation in minted:
        assert validate_generation_token(generation) == generation
        assert vector_generation_prefix("districts", generation) == f"vector/districts/{generation}"


@pytest.mark.parametrize(
    "generation",
    ["", "0123456789ABCDEF0123456789abcdef", "0123456789abcdef", "with/slash", "0123456789abcdef0123456789abcdefg"],
)
def test_invalid_generation_tokens_are_rejected(generation: str) -> None:
    with pytest.raises(StorageAddressError):
        validate_generation_token(generation)


def test_every_vector_object_lives_below_the_storage_prefix_of_its_generation() -> None:
    assert vector_pointer_key(PREFIX) == f"{PREFIX}/current.json"
    assert vector_versions_prefix(PREFIX) == f"{PREFIX}/versions"
    assert vector_reservation_key(PREFIX, 3) == f"{PREFIX}/versions/v00003/reservation.json"
    assert vector_version_metadata_key(PREFIX, 3) == f"{PREFIX}/versions/v00003/metadata.json"


def test_a_record_written_before_generations_keeps_resolving_from_its_storage_key() -> None:
    # A record stored before this layout holds "vector/{identifier}"; every key still derives from it.
    assert vector_data_key("vector/districts", 1) == "vector/districts/versions/v00001/data.parquet"
    assert vector_pointer_key("vector/districts") == "vector/districts/current.json"


def test_vector_versions_are_zero_padded() -> None:
    assert format_vector_version(1) == "v00001"
    assert format_vector_version(42) == "v00042"
    assert vector_version_prefix(PREFIX, 1) == f"{PREFIX}/versions/v00001"
    assert vector_data_key(PREFIX, 7) == f"{PREFIX}/versions/v00007/data.parquet"


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
