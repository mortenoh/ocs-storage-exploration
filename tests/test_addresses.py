"""Tests for storage schemes and URI addresses."""

from __future__ import annotations

import pytest

from ocs_storage_exploration.storage.addresses import (
    StorageAddress,
    StorageScheme,
    join_key_parts,
    parse_storage_scheme,
    validate_object_key,
)
from ocs_storage_exploration.storage.errors import StorageAddressError


def test_scheme_values_are_uri_schemes() -> None:
    assert [scheme.value for scheme in StorageScheme] == ["file", "memory", "s3"]
    assert parse_storage_scheme("s3") is StorageScheme.S3


def test_malformed_scheme_is_rejected() -> None:
    for value in ["", "9gs", "GS", "g s", "gs:"]:
        with pytest.raises(StorageAddressError):
            parse_storage_scheme(value)


def test_a_well_formed_unknown_scheme_is_accepted_for_a_plugin_to_claim() -> None:
    # A scheme the service does not build itself may still be provided by a plugin;
    # build_backend is where an unprovided scheme is refused.
    assert parse_storage_scheme("gs") == "gs"


@pytest.mark.parametrize(
    "key",
    ["", "a//b", "a/./b", "a/../b", "..", "/leading", "trailing/", "a\\b"],
)
def test_invalid_keys_are_rejected(key: str) -> None:
    with pytest.raises(StorageAddressError):
        validate_object_key(key)


def test_valid_key_is_returned_unchanged() -> None:
    assert validate_object_key("ocs/catalog/datasets/one.json") == "ocs/catalog/datasets/one.json"


def test_join_key_parts_normalises_separators() -> None:
    assert join_key_parts("ocs/", "/raster", "one") == "ocs/raster/one"


def test_object_storage_address_round_trips_through_a_uri() -> None:
    address = StorageAddress(scheme=StorageScheme.S3, root="bucket", key="ocs/catalog/datasets/one.json")

    assert address.as_uri() == "s3://bucket/ocs/catalog/datasets/one.json"
    assert StorageAddress.from_uri(address.as_uri()) == address


def test_memory_address_round_trips_through_a_uri() -> None:
    address = StorageAddress(scheme=StorageScheme.MEMORY, root="memory", key="ocs/vector/one/current.json")

    assert StorageAddress.from_uri(address.as_uri()) == address


def test_file_address_folds_its_root_into_the_path() -> None:
    address = StorageAddress(scheme=StorageScheme.FILE, root="/srv/data", key="ocs/raster/one")

    assert address.as_uri() == "file:///srv/data/ocs/raster/one"
    parsed = StorageAddress.from_uri(address.as_uri())
    assert parsed.root == "/"
    assert parsed.key == "srv/data/ocs/raster/one"
    assert parsed.as_uri() == address.as_uri()


def test_credentials_in_a_uri_are_rejected() -> None:
    with pytest.raises(StorageAddressError):
        StorageAddress.from_uri("s3://key:secret@bucket/ocs/one.json")


@pytest.mark.parametrize(
    "uri",
    [
        "s3://bucket/ocs/../escape.json",
        "s3://bucket/",
        "s3:///ocs/one.json",
        "s3://bucket/ocs/one.json?versionId=2",
        "s3://bucket/ocs/one.json#fragment",
        "file://remote-host/ocs/one.json",
        "s3://bucket/ocs\\one.json",
    ],
)
def test_invalid_uris_are_rejected(uri: str) -> None:
    with pytest.raises(StorageAddressError):
        StorageAddress.from_uri(uri)


def test_address_requires_a_root() -> None:
    with pytest.raises(StorageAddressError):
        StorageAddress(scheme=StorageScheme.S3, root="", key="ocs/one.json")


def test_file_address_requires_an_absolute_root() -> None:
    with pytest.raises(StorageAddressError):
        StorageAddress(scheme=StorageScheme.FILE, root="relative", key="ocs/one.json")


def test_object_storage_root_must_be_a_plain_authority() -> None:
    with pytest.raises(StorageAddressError):
        StorageAddress(scheme=StorageScheme.S3, root="bucket/extra", key="ocs/one.json")


def test_root_must_not_carry_credentials() -> None:
    with pytest.raises(StorageAddressError):
        StorageAddress(scheme=StorageScheme.S3, root="key:secret@bucket", key="ocs/one.json")


def test_joined_and_parent_walk_the_key() -> None:
    address = StorageAddress(scheme=StorageScheme.S3, root="bucket", key="ocs/vector/one")

    child = address.joined("versions", "v00001", "data.parquet")
    assert child.key == "ocs/vector/one/versions/v00001/data.parquet"
    assert child.parent().key == "ocs/vector/one/versions/v00001"
    assert child.scheme is address.scheme
    assert child.root == address.root


def test_parent_of_a_single_segment_key_is_rejected() -> None:
    with pytest.raises(StorageAddressError):
        StorageAddress(scheme=StorageScheme.S3, root="bucket", key="ocs").parent()


def test_address_is_hashable_and_prints_as_a_uri() -> None:
    address = StorageAddress(scheme=StorageScheme.S3, root="bucket", key="ocs/one.json")

    assert str(address) == address.as_uri()
    assert len({address, StorageAddress(scheme=StorageScheme.S3, root="bucket", key="ocs/one.json")}) == 1
