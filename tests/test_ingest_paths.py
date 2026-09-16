"""Tests of the ingest path resolver: globs, ordering and the roots a path may not escape."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy
import pytest
from fastapi.testclient import TestClient

from ocs_storage_exploration.storage.errors import IngestPathError
from ocs_storage_exploration.storage.paths import (
    relative_to_working_directory,
    resolve_ingest_path,
    resolve_ingest_paths,
    resolve_ingest_roots,
)
from tests.ingest_helpers import SAMPLES_DIRECTORY, ramp, write_zarr_store


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    root = tmp_path / "samples"
    (root / "nested").mkdir(parents=True)
    (root / "one.tif").write_bytes(b"one")
    (root / "two.tif").write_bytes(b"two")
    (root / "nested" / "three.tif").write_bytes(b"three")
    (tmp_path / "outside.tif").write_bytes(b"outside")
    write_zarr_store(
        root / "cube.zarr",
        values=ramp(2, 2).reshape(1, 2, 2),
        y_values=numpy.array([2.5, 1.5]),
        x_values=numpy.array([10.5, 11.5]),
        timestamps=[datetime(2024, 1, 1)],
    )
    return root


def test_a_glob_resolves_to_the_files_it_names(sample_tree: Path, tmp_path: Path) -> None:
    resolved = resolve_ingest_paths(["samples/*.tif"], roots=[sample_tree], working_directory=tmp_path)
    assert [path.name for path in resolved] == ["one.tif", "two.tif"]


def test_a_literal_path_resolves_to_one_file(sample_tree: Path, tmp_path: Path) -> None:
    resolved = resolve_ingest_path("samples/nested/three.tif", roots=[sample_tree], working_directory=tmp_path)
    assert resolved == (sample_tree / "nested" / "three.tif").resolve()


def test_a_pattern_matching_nothing_is_refused(sample_tree: Path, tmp_path: Path) -> None:
    with pytest.raises(IngestPathError, match="no readable file matches"):
        resolve_ingest_paths(["samples/*.nc"], roots=[sample_tree], working_directory=tmp_path)
    with pytest.raises(IngestPathError, match="no readable file matches"):
        resolve_ingest_paths(["samples/missing.tif"], roots=[sample_tree], working_directory=tmp_path)


def test_a_relative_escape_is_refused(sample_tree: Path, tmp_path: Path) -> None:
    with pytest.raises(IngestPathError, match="resolves outside"):
        resolve_ingest_paths(["samples/../outside.tif"], roots=[sample_tree], working_directory=tmp_path)


def test_an_absolute_path_outside_the_roots_is_refused(sample_tree: Path, tmp_path: Path) -> None:
    with pytest.raises(IngestPathError, match="resolves outside"):
        resolve_ingest_paths([str(tmp_path / "outside.tif")], roots=[sample_tree], working_directory=tmp_path)


def test_an_absolute_path_inside_the_roots_is_allowed(sample_tree: Path, tmp_path: Path) -> None:
    resolved = resolve_ingest_paths([str(sample_tree / "one.tif")], roots=[sample_tree], working_directory=tmp_path)
    assert resolved == [(sample_tree / "one.tif").resolve()]


def test_a_directory_is_not_a_file(sample_tree: Path, tmp_path: Path) -> None:
    with pytest.raises(IngestPathError, match="no readable file matches"):
        resolve_ingest_paths(["samples/nested"], roots=[sample_tree], working_directory=tmp_path)


def test_a_zarr_directory_store_resolves_as_a_literal_path(sample_tree: Path, tmp_path: Path) -> None:
    resolved = resolve_ingest_path("samples/cube.zarr", roots=[sample_tree], working_directory=tmp_path)
    assert resolved == (sample_tree / "cube.zarr").resolve()
    assert resolved.is_dir()


def test_a_zarr_directory_store_resolves_through_a_glob(sample_tree: Path, tmp_path: Path) -> None:
    resolved = resolve_ingest_paths(["samples/*.zarr"], roots=[sample_tree], working_directory=tmp_path)
    assert [path.name for path in resolved] == ["cube.zarr"]


def test_without_a_root_nothing_may_be_read(sample_tree: Path, tmp_path: Path) -> None:
    with pytest.raises(IngestPathError, match="no ingest root"):
        resolve_ingest_paths(["samples/one.tif"], roots=[], working_directory=tmp_path)


def test_duplicate_matches_are_kept_once(sample_tree: Path, tmp_path: Path) -> None:
    resolved = resolve_ingest_paths(
        ["samples/one.tif", "samples/*.tif"],
        roots=[sample_tree],
        working_directory=tmp_path,
    )
    assert [path.name for path in resolved] == ["one.tif", "two.tif"]


def test_roots_are_resolved_against_the_working_directory(tmp_path: Path) -> None:
    roots = resolve_ingest_roots([Path("samples"), Path("data")], working_directory=tmp_path)
    assert roots == (tmp_path / "samples", tmp_path / "data")


def test_a_path_is_reported_relative_to_the_working_directory(sample_tree: Path, tmp_path: Path) -> None:
    rendered = relative_to_working_directory(sample_tree / "one.tif", working_directory=tmp_path)
    assert rendered == "samples/one.tif"


def test_the_api_refuses_a_raster_path_outside_the_ingest_roots(ingest_client: TestClient, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.tif"
    outside.write_bytes(b"outside")
    response = ingest_client.post(
        "/api/v1/raster/escaped/ingest",
        json={"files": [str(outside)], "variable": "rain", "timestamp": "2024-01-01T00:00:00Z"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "IngestPathError"


def test_the_api_refuses_a_vector_path_that_climbs_out(ingest_client: TestClient) -> None:
    response = ingest_client.post(
        "/api/v1/vector/escaped/ingest",
        json={"path": f"{SAMPLES_DIRECTORY}/../pyproject.toml", "identifier_property": "id"},
    )
    assert response.status_code == 400, response.text
    assert "resolves outside" in response.json()["detail"]
