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

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
SAMPLE_DIRECTORY: Final[Path] = REPOSITORY_ROOT / "samples"
DOWNLOAD_DIRECTORY: Final[Path] = SAMPLE_DIRECTORY / "downloaded"
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
    """One line of the summary table: what was written, or why it was skipped."""

    dataset_identifier: str
    kind: str
    detail: str
    published: bool = False
    skipped: bool = False


RASTER_DEMOS: Final[tuple[RasterDemo, ...]] = (
    RasterDemo(
        dataset_identifier="chirps3-sle-daily",
        files=(str(DOWNLOAD_DIRECTORY / "chirps3" / "chirps3-*.tif"),),
        variable="precipitation",
        title="CHIRPS v3.0 daily rainfall over Sierra Leone",
        license="CC0-1.0",
        attribution="Climate Hazards Center, UC Santa Barbara",
    ),
    RasterDemo(
        dataset_identifier="worldpop-sle-2026",
        files=(str(SAMPLE_DIRECTORY / "sle_pop_2026_CN_1km_R2025A_UA_v1.tif"),),
        variable="population",
        title="WorldPop constrained population of Sierra Leone, 2026",
        license="CC-BY-4.0",
        attribution="WorldPop, University of Southampton",
        timestamp=WORLDPOP_TIMESTAMP,
    ),
)

VECTOR_DEMOS: Final[tuple[VectorDemo, ...]] = (
    VectorDemo(
        dataset_identifier="sle-districts",
        path=str(SAMPLE_DIRECTORY / "sierra_leone_districts.geojson"),
        identifier_property="id",
        title="Sierra Leone districts, DHIS2 organisation units",
        license="BSD-3-Clause",
        attribution="DHIS2 demo database, via the Open Climate Service test data",
        selectable_columns=("level", "name", "parentName"),
    ),
    VectorDemo(
        dataset_identifier="sle-adm2-geoboundaries",
        path=str(DOWNLOAD_DIRECTORY / "geoboundaries-sle-adm2.geojson"),
        identifier_property="shapeID",
        title="Sierra Leone ADM2 areas, geoBoundaries",
        license="CC-BY-4.0",
        attribution="geoBoundaries, William and Mary geoLab",
        selectable_columns=("shapeName", "shapeGroup", "shapeType"),
    ),
    VectorDemo(
        dataset_identifier="ne-lakes",
        path=str(SAMPLE_DIRECTORY / "ne_110m_lakes.geojson"),
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
        state = "skipped" if outcome.skipped else ("published" if outcome.published else "draft")
        print(f"{outcome.dataset_identifier:<{width}}  {outcome.kind:<10}  {state:<9}  {outcome.detail}")
    print()
    print(f">>> Start the service with `make run`, then open these against {SERVICE_URL}")
    for url in example_urls():
        print(f"  {url}")
    print()


def main() -> int:
    """Ingest every sample into the configured backend and print the summary."""
    settings = Settings()
    print(f">>> Backend {settings.backend}, ingest roots {[str(root) for root in settings.ingest_roots]}")
    service = StorageService.from_settings(settings)
    outcomes: list[DemoOutcome] = []
    try:
        for raster_demo in RASTER_DEMOS:
            outcomes.append(ingest_raster_demo(service, raster_demo))
            print(f"[done] {raster_demo.dataset_identifier}")
        for vector_demo in VECTOR_DEMOS:
            outcomes.append(ingest_vector_demo(service, vector_demo))
            print(f"[done] {vector_demo.dataset_identifier}")
    except StorageError as error:
        print(f"ERROR: {type(error).__name__}: {error.message}", file=sys.stderr)
        return 1
    print_summary(outcomes, settings=settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
