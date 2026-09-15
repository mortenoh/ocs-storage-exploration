"""Tests for the shared query parameter parsers."""

from __future__ import annotations

import pytest

from ocs_storage_exploration.api.parameters import parse_bbox, parse_columns, parse_crs
from ocs_storage_exploration.storage.errors import CrsError, StorageAddressError
from ocs_storage_exploration.storage.schemas import BoundingBox


def test_bbox_is_parsed_into_an_envelope() -> None:
    assert parse_bbox("-10,-5,10,5") == BoundingBox(minimum_x=-10.0, minimum_y=-5.0, maximum_x=10.0, maximum_y=5.0)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_absent_bbox_is_none(value: str | None) -> None:
    assert parse_bbox(value) is None


@pytest.mark.parametrize("value", ["1,2,3", "1,2,3,4,5", "a,b,c,d", "10,0,-10,5", "1,2,3,x"])
def test_invalid_bbox_is_rejected(value: str) -> None:
    with pytest.raises(StorageAddressError):
        parse_bbox(value)


def test_columns_are_split_trimmed_and_deduplicated() -> None:
    assert parse_columns(" id , name ,, name ") == ("id", "name")
    assert parse_columns(None) == ()


def test_a_crs_is_returned_unchanged_when_pyproj_reads_it() -> None:
    assert parse_crs("EPSG:3857", parameter="bbox-crs") == "EPSG:3857"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_absent_crs_is_none(value: str | None) -> None:
    assert parse_crs(value, parameter="bbox-crs") is None


@pytest.mark.parametrize("value", ["not-a-crs", "EPSG:999999", "+proj=nonsense"])
def test_an_unreadable_crs_is_rejected(value: str) -> None:
    with pytest.raises(CrsError) as failure:
        parse_crs(value, parameter="bbox-crs")

    assert "bbox-crs" in failure.value.message
    assert value in failure.value.message
