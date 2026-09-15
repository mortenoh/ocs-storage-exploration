"""Equality and prefix query clauses translated into Parquet row group filters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import pyarrow
import pyarrow.types

from ocs_storage_exploration.storage.errors import SelectableColumnError

WhereClause = Mapping[str, str | int | float | bool]

CLAUSE_SEPARATOR: Final[str] = ":"
PREFIX_MARKER: Final[str] = "*"
PREFIX_UPPER_BOUND: Final[str] = "￿"
TRUE_LITERALS: Final[frozenset[str]] = frozenset({"true", "yes", "1"})
FALSE_LITERALS: Final[frozenset[str]] = frozenset({"false", "no", "0"})


def parse_where(raw: Sequence[str]) -> WhereClause:
    """Parse repeated column:value clauses, keeping a trailing asterisk as the prefix match marker."""
    clause: dict[str, str | int | float | bool] = {}
    for entry in raw:
        column, separator, value = entry.partition(CLAUSE_SEPARATOR)
        column = column.strip()
        if not separator or not column or not value:
            raise SelectableColumnError(f"where clause must be spelled column:value: {entry!r}")
        if column in clause:
            raise SelectableColumnError(f"where clause repeats column {column!r}")
        clause[column] = value
    return clause


def is_prefix_value(value: str | int | float | bool) -> bool:
    """Report whether a clause value asks for a prefix match by ending in an asterisk."""
    return isinstance(value, str) and len(value) > 1 and value.endswith(PREFIX_MARKER)


def build_prefix_filter(column: str, prefix: str) -> list[tuple[str, str, Any]]:
    """Return the half-open range filter that matches every value starting with the prefix."""
    if not prefix:
        raise SelectableColumnError(f"prefix clause on column {column!r} must not be empty")
    return [(column, ">=", prefix), (column, "<", prefix + PREFIX_UPPER_BOUND)]


def coerce_clause_value(column: str, value: str | int | float | bool, target_type: pyarrow.DataType) -> Any:
    """Coerce a clause value to the Arrow type of the column it filters."""
    if pyarrow.types.is_boolean(target_type) and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in TRUE_LITERALS:
            return True
        if lowered in FALSE_LITERALS:
            return False
        raise SelectableColumnError(f"value {value!r} is not a boolean for column {column!r}")
    try:
        return pyarrow.scalar(value).cast(target_type).as_py()
    except (pyarrow.ArrowInvalid, pyarrow.ArrowNotImplementedError, pyarrow.ArrowTypeError, ValueError) as error:
        raise SelectableColumnError(
            f"value {value!r} does not fit column {column!r} of type {target_type}",
        ) from error


def build_filters(
    clause: WhereClause,
    *,
    selectable_columns: Sequence[str],
    schema: pyarrow.Schema,
) -> list[tuple[str, str, Any]] | None:
    """Translate a where clause into a Parquet filter list, refusing undeclared or unknown columns."""
    if not clause:
        return None
    declared = tuple(selectable_columns)
    filters: list[tuple[str, str, Any]] = []
    for column, value in clause.items():
        if column not in declared:
            raise SelectableColumnError(
                f"column {column!r} is not selectable; declared selectable columns are {list(declared)}",
            )
        if column not in schema.names:
            raise SelectableColumnError(f"column {column!r} is not present in the stored schema")
        field_type = schema.field(column).type
        if is_prefix_value(value):
            if not (pyarrow.types.is_string(field_type) or pyarrow.types.is_large_string(field_type)):
                raise SelectableColumnError(
                    f"prefix clause is only supported on text columns, and {column!r} is {field_type}",
                )
            filters.extend(build_prefix_filter(column, str(value)[: -len(PREFIX_MARKER)]))
            continue
        filters.append((column, "=", coerce_clause_value(column, value, field_type)))
    return filters
