"""Tests for the vector query predicate parsing and Parquet filter construction."""

from __future__ import annotations

import pyarrow
import pytest

from ocs_storage_exploration.storage.errors import SelectableColumnError
from ocs_storage_exploration.storage.vector.predicates import (
    PREFIX_UPPER_BOUND,
    build_filters,
    build_prefix_filter,
    is_prefix_value,
    parse_where,
)

SCHEMA = pyarrow.schema(
    [
        pyarrow.field("id", pyarrow.string()),
        pyarrow.field("level", pyarrow.int64()),
        pyarrow.field("path", pyarrow.string()),
        pyarrow.field("ratio", pyarrow.float64()),
        pyarrow.field("active", pyarrow.bool_()),
    ],
)
SELECTABLE = ("level", "path", "ratio", "active")


def test_parse_where_reads_repeated_clauses() -> None:
    assert parse_where(["level:2", "path:/root/a"]) == {"level": "2", "path": "/root/a"}


def test_parse_where_keeps_the_prefix_marker() -> None:
    clause = parse_where(["path:/root/a*"])

    assert clause == {"path": "/root/a*"}
    assert is_prefix_value(clause["path"])


def test_parse_where_accepts_a_value_containing_a_colon() -> None:
    assert parse_where(["path:/root:a"]) == {"path": "/root:a"}


@pytest.mark.parametrize("entry", ["level", "level:", ":2", "  :2", ""])
def test_parse_where_refuses_malformed_clauses(entry: str) -> None:
    with pytest.raises(SelectableColumnError):
        parse_where([entry])


def test_parse_where_refuses_a_repeated_column() -> None:
    with pytest.raises(SelectableColumnError):
        parse_where(["level:1", "level:2"])


def test_build_filters_returns_none_for_an_empty_clause() -> None:
    assert build_filters({}, selectable_columns=SELECTABLE, schema=SCHEMA) is None


def test_build_filters_coerces_values_to_the_column_type() -> None:
    filters = build_filters(
        {"level": "2", "ratio": "1.5", "active": "true"},
        selectable_columns=SELECTABLE,
        schema=SCHEMA,
    )

    assert filters == [("level", "=", 2), ("ratio", "=", 1.5), ("active", "=", True)]


def test_build_filters_keeps_text_values_as_they_are() -> None:
    assert build_filters({"path": "/root/a"}, selectable_columns=SELECTABLE, schema=SCHEMA) == [
        ("path", "=", "/root/a"),
    ]


def test_build_filters_expands_a_prefix_clause() -> None:
    filters = build_filters({"path": "/root/a*"}, selectable_columns=SELECTABLE, schema=SCHEMA)

    assert filters == [("path", ">=", "/root/a"), ("path", "<", "/root/a" + PREFIX_UPPER_BOUND)]


def test_build_filters_refuses_an_undeclared_column() -> None:
    with pytest.raises(SelectableColumnError) as failure:
        build_filters({"id": "square-0"}, selectable_columns=SELECTABLE, schema=SCHEMA)

    assert "'id'" in failure.value.message
    assert "level" in failure.value.message


def test_build_filters_refuses_a_column_missing_from_the_schema() -> None:
    with pytest.raises(SelectableColumnError) as failure:
        build_filters({"missing": "1"}, selectable_columns=("missing",), schema=SCHEMA)

    assert "missing" in failure.value.message


def test_build_filters_refuses_a_value_that_does_not_fit_the_column() -> None:
    with pytest.raises(SelectableColumnError):
        build_filters({"level": "not-a-number"}, selectable_columns=SELECTABLE, schema=SCHEMA)


def test_build_filters_refuses_a_prefix_clause_on_a_numeric_column() -> None:
    with pytest.raises(SelectableColumnError):
        build_filters({"level": "12*"}, selectable_columns=SELECTABLE, schema=SCHEMA)


def test_build_filters_refuses_a_value_that_is_not_a_boolean() -> None:
    with pytest.raises(SelectableColumnError):
        build_filters({"active": "maybe"}, selectable_columns=SELECTABLE, schema=SCHEMA)


def test_build_prefix_filter_builds_a_half_open_range() -> None:
    assert build_prefix_filter("path", "/root") == [
        ("path", ">=", "/root"),
        ("path", "<", "/root" + PREFIX_UPPER_BOUND),
    ]


def test_build_prefix_filter_refuses_an_empty_prefix() -> None:
    with pytest.raises(SelectableColumnError):
        build_prefix_filter("path", "")


def test_is_prefix_value_only_reacts_to_a_trailing_asterisk() -> None:
    assert is_prefix_value("/root/a*")
    assert not is_prefix_value("/root/a")
    assert not is_prefix_value("*")
    assert not is_prefix_value(2)
