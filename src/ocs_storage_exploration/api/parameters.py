"""Query parameter types and parsers shared by the API routers."""

from __future__ import annotations

import math
from typing import Annotated

from fastapi import Query
from pydantic import ValidationError

from ocs_storage_exploration.storage.errors import StorageAddressError
from ocs_storage_exploration.storage.schemas import BoundingBox
from ocs_storage_exploration.storage.vector import require_crs

BoundingBoxQuery = Annotated[
    str | None,
    Query(alias="bbox", description="Envelope as minimum_x,minimum_y,maximum_x,maximum_y"),
]
BoundingBoxCrsQuery = Annotated[
    str | None,
    Query(alias="bbox-crs", description="Coordinate reference system of the bbox parameter"),
]
ColumnsQuery = Annotated[
    str | None,
    Query(alias="columns", description="Comma separated list of columns to return"),
]
WhereQuery = Annotated[
    list[str] | None,
    Query(alias="where", description="Repeatable column:value equality clause"),
]


def parse_bbox(value: str | None) -> BoundingBox | None:
    """Parse a comma separated bbox query parameter into a finite, correctly ordered bounding box."""
    if value is None or not value.strip():
        return None
    parts = value.split(",")
    if len(parts) != 4:
        raise StorageAddressError(f"bbox must have four comma separated numbers: {value!r}")
    try:
        numbers = tuple(float(part) for part in parts)
    except ValueError as error:
        raise StorageAddressError(f"bbox must contain only numbers: {value!r}") from error
    if not all(math.isfinite(number) for number in numbers):
        raise StorageAddressError(f"bbox must contain only finite numbers: {value!r}")
    try:
        # The model refuses an envelope whose maximum is not greater than its minimum.
        return BoundingBox.from_sequence((numbers[0], numbers[1], numbers[2], numbers[3]))
    except ValidationError as error:
        raise StorageAddressError(f"bbox is not a valid envelope: {value!r}") from error


def parse_crs(value: str | None, *, parameter: str) -> str | None:
    """Validate a coordinate reference system query parameter, returning None when it is absent."""
    if value is None or not value.strip():
        return None
    require_crs(value, label=parameter)
    return value


def parse_columns(value: str | None) -> tuple[str, ...]:
    """Parse a comma separated column list, dropping blanks and keeping the first occurrence."""
    if value is None:
        return ()
    selected: list[str] = []
    for part in value.split(","):
        column = part.strip()
        if column and column not in selected:
            selected.append(column)
    return tuple(selected)
