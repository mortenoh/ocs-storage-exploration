"""Ingests every sample dataset into the configured backend in process, then prints what to look at."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.errors import StorageError
from ocs_storage_exploration.storage.raster.ingest import build_raster_ingest_plan, ingest_raster_files
from ocs_storage_exploration.storage.service import StorageService
from ocs_storage_exploration.storage.vector.ingest import build_vector_ingest_plan, ingest_vector_file

SAMPLE_DIRECTORY_NAME: Final[str] = "samples"
DOWNLOAD_DIRECTORY_NAME: Final[str] = "downloaded"
WORLDPOP_TIMESTAMP: Final[datetime] = datetime(2026, 1, 1, tzinfo=UTC)
SERVICE_URL: Final[str] = "http://127.0.0.1:8000"
MISSING_SAMPLE_HINT: Final[str] = "run `make samples` once while online"


@dataclass(frozen=True, slots=True)
class RasterDemo:
    """One coverage the demo ingests from the sample files."""

    dataset_identifier: str
    files: tuple[str, ...]
    variable: str
    title: str
    license: str
    attribution: str
    timestamp: datetime | None = None
    publish: bool = True


@dataclass(frozen=True, slots=True)
class VectorDemo:
    """One collection the demo ingests from the sample files."""

    dataset_identifier: str
    path: str
    identifier_property: str
    title: str
    license: str
    attribution: str
    selectable_columns: tuple[str, ...] = field(default=())
    publish: bool = True


@dataclass(frozen=True, slots=True)
class DemoOutcome:
    """One line of the summary table: what was written, why it was skipped or how it failed."""

    dataset_identifier: str
    kind: str
    detail: str
    published: bool = False
    skipped: bool = False
    failed: bool = False


def default_sample_directory() -> Path:
    """Return the sample directory of the deployment, resolved against the working directory."""
    return Path(SAMPLE_DIRECTORY_NAME).resolve()


def raster_demos(sample_directory: Path) -> tuple[RasterDemo, ...]:
    """Return every coverage the demo ingests, reading its files from the given sample directory."""
    download_directory = sample_directory / DOWNLOAD_DIRECTORY_NAME
    return (
        RasterDemo(
            dataset_identifier="chirps3-sle-daily",
            files=(str(download_directory / "chirps3" / "chirps3-*.tif"),),
            variable="precipitation",
            title="CHIRPS v3.0 daily rainfall over Sierra Leone",
            license="CC0-1.0",
            attribution="Climate Hazards Center, UC Santa Barbara",
        ),
        RasterDemo(
            dataset_identifier="worldpop-sle-2026",
            files=(str(sample_directory / "sle_pop_2026_CN_1km_R2025A_UA_v1.tif"),),
            variable="population",
            title="WorldPop constrained population of Sierra Leone, 2026",
            license="CC-BY-4.0",
            attribution="WorldPop, University of Southampton",
            timestamp=WORLDPOP_TIMESTAMP,
        ),
    )


def vector_demos(sample_directory: Path) -> tuple[VectorDemo, ...]:
    """Return every collection the demo ingests, reading its files from the given sample directory."""
    download_directory = sample_directory / DOWNLOAD_DIRECTORY_NAME
    return (
        VectorDemo(
            dataset_identifier="sle-districts",
            path=str(sample_directory / "sierra_leone_districts.geojson"),
            identifier_property="id",
            title="Sierra Leone districts, DHIS2 organisation units",
            license="BSD-3-Clause",
            attribution="DHIS2 demo database, via the Open Climate Service test data",
            selectable_columns=("level", "name", "parentName"),
        ),
        VectorDemo(
            dataset_identifier="sle-adm2-geoboundaries",
            path=str(download_directory / "geoboundaries-sle-adm2.geojson"),
            identifier_property="shapeID",
            title="Sierra Leone ADM2 areas, geoBoundaries",
            license="CC-BY-4.0",
            attribution="geoBoundaries, William and Mary geoLab",
            selectable_columns=("shapeName", "shapeGroup", "shapeType"),
        ),
        VectorDemo(
            dataset_identifier="ne-lakes",
            path=str(sample_directory / "ne_110m_lakes.geojson"),
            identifier_property="id",
            title="Natural Earth lakes at 1:110m",
            license="CC0-1.0",
            attribution="Natural Earth",
            selectable_columns=("name", "featureclass"),
            # Left unpublished on purpose, so the demo also shows a draft: STAC does not advertise it.
            publish=False,
        ),
    )


def ingest_raster_demo(service: StorageService, demo: RasterDemo) -> DemoOutcome:
    """Run one coverage ingest of the demo, reporting what it wrote or why it was skipped."""
    missing = missing_inputs(demo.files)
    if missing:
        return DemoOutcome(demo.dataset_identifier, "coverage", missing, skipped=True)
    plan = build_raster_ingest_plan(
        files=demo.files,
        variable=demo.variable,
        roots=service.settings.ingest_roots,
        timestamp=demo.timestamp,
        title=demo.title,
        license=demo.license,
        attribution=demo.attribution,
        # The demo is meant to be run again after a change, so it replaces what it wrote last time.
        overwrite=True,
        publish=demo.publish,
    )
    result = ingest_raster_files(service.raster, demo.dataset_identifier, plan)
    count = result.timestep_count
    span = f"{plan.timestamps[0].date()} to {plan.timestamps[-1].date()}"
    detail = f"{count} timestep{'' if count == 1 else 's'}, {span}, variable {demo.variable}"
    return DemoOutcome(demo.dataset_identifier, "coverage", detail, published=result.published)


def ingest_vector_demo(service: StorageService, demo: VectorDemo) -> DemoOutcome:
    """Run one collection ingest of the demo, reporting what it wrote or why it was skipped."""
    missing = missing_inputs((demo.path,))
    if missing:
        return DemoOutcome(demo.dataset_identifier, "collection", missing, skipped=True)
    plan = build_vector_ingest_plan(
        path=demo.path,
        roots=service.settings.ingest_roots,
        identifier_property=demo.identifier_property,
        selectable_columns=demo.selectable_columns,
        title=demo.title,
        license=demo.license,
        attribution=demo.attribution,
        publish=demo.publish,
    )
    result = ingest_vector_file(service.vector, demo.dataset_identifier, plan)
    detail = f"version {result.version}, {result.feature_count} features, id {demo.identifier_property}"
    return DemoOutcome(demo.dataset_identifier, "collection", detail, published=result.published)


def run_demos(service: StorageService, *, sample_directory: Path) -> list[DemoOutcome]:
    """Ingest every demo dataset into the service, one outcome per dataset, a failure included as one."""
    outcomes: list[DemoOutcome] = []
    for raster_demo in raster_demos(sample_directory):
        try:
            outcome = ingest_raster_demo(service, raster_demo)
        except StorageError as error:
            outcome = failed_outcome(raster_demo.dataset_identifier, "coverage", error)
        outcomes.append(outcome)
        print(f"[{'failed' if outcome.failed else 'done'}] {outcome.dataset_identifier}")
    for vector_demo in vector_demos(sample_directory):
        try:
            outcome = ingest_vector_demo(service, vector_demo)
        except StorageError as error:
            outcome = failed_outcome(vector_demo.dataset_identifier, "collection", error)
        outcomes.append(outcome)
        print(f"[{'failed' if outcome.failed else 'done'}] {outcome.dataset_identifier}")
    return outcomes


def failed_outcome(dataset_identifier: str, kind: str, error: StorageError) -> DemoOutcome:
    """Report a dataset the storage layer refused, so the run finishes the rest and still exits non-zero."""
    return DemoOutcome(dataset_identifier, kind, f"{type(error).__name__}: {error.message}", failed=True)


def outcome_state(outcome: DemoOutcome) -> str:
    """Name the state one outcome is in, for the progress line and the summary table."""
    if outcome.failed:
        return "failed"
    if outcome.skipped:
        return "skipped"
    return "published" if outcome.published else "draft"


def missing_inputs(patterns: Sequence[str]) -> str:
    """Report the sample inputs a pattern names that are not on disk, as a message or an empty string."""
    for pattern in patterns:
        path = Path(pattern)
        if not path.parent.is_dir() or not any(path.parent.glob(path.name)):
            return f"{path.name} is missing, {MISSING_SAMPLE_HINT}"
    return ""


def example_urls() -> tuple[str, ...]:
    """Return the example URLs the summary points at, one per line."""
    return (
        "/health",
        "/api/v1/backends",
        "/api/v1/datasets",
        "/api/v1/raster/chirps3-sle-daily/query?bbox=-13.3,7.9,-12.0,9.0",
        "/api/v1/raster/chirps3-sle-daily/query?start=2024-01-01T00:00:00&end=2024-01-07T00:00:00",
        "/api/v1/raster/chirps3-sle-daily/versions",
        "/api/v1/raster/worldpop-sle-2026/query",
        "/api/v1/vector/sle-districts/features?bbox=-13.3,7.9,-12.0,9.0",
        "/api/v1/vector/sle-districts/features?where=level:2&columns=name,level",
        "/api/v1/vector/sle-adm2-geoboundaries/features?limit=3",
        "/stac/collections",
        "/stac/collections/chirps3-sle-daily",
    )


def print_summary(outcomes: Sequence[DemoOutcome], *, settings: Settings) -> None:
    """Print the summary table and the URLs worth opening once the service is running."""
    width = max(len(outcome.dataset_identifier) for outcome in outcomes)
    print()
    print(f">>> Ingested into the {settings.backend} backend")
    print(f"{'dataset':<{width}}  {'kind':<10}  {'state':<9}  detail")
    print(f"{'-' * width}  {'-' * 10}  {'-' * 9}  {'-' * 52}")
    for outcome in outcomes:
        print(
            f"{outcome.dataset_identifier:<{width}}  {outcome.kind:<10}  {outcome_state(outcome):<9}  {outcome.detail}"
        )
    print()
    print(f">>> Open these against the running service, {SERVICE_URL} after `make run`")
    for url in example_urls():
        print(f"  {url}")
    print()


def main() -> int:
    """Ingest every sample into the configured backend, print the summary and report any failure."""
    settings = Settings()
    print(f">>> Backend {settings.backend}, ingest roots {[str(root) for root in settings.ingest_roots]}")
    service = StorageService.from_settings(settings)
    outcomes = run_demos(service, sample_directory=default_sample_directory())
    print_summary(outcomes, settings=settings)
    failures = [outcome for outcome in outcomes if outcome.failed]
    for failure in failures:
        print(f"ERROR: {failure.dataset_identifier}: {failure.detail}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
