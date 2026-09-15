"""Key layout for catalog records, raster repositories and vector collections."""

from __future__ import annotations

import re
from typing import Final

from ocs_storage_exploration.storage.errors import StorageAddressError

CATALOG_PREFIX: Final[str] = "catalog/datasets"
RASTER_PREFIX: Final[str] = "raster"
VECTOR_PREFIX: Final[str] = "vector"
VECTOR_POINTER_NAME: Final[str] = "current.json"
VECTOR_DATA_NAME: Final[str] = "data.parquet"
VECTOR_VERSION_DIGITS: Final[int] = 5
MAXIMUM_VECTOR_VERSION: Final[int] = 10**VECTOR_VERSION_DIGITS - 1

DATASET_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
VECTOR_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^v(\d{5})$")


def validate_dataset_identifier(identifier: str) -> str:
    """Validate a dataset identifier and return it unchanged."""
    if not DATASET_IDENTIFIER_PATTERN.match(identifier):
        raise StorageAddressError(
            f"dataset identifier must match {DATASET_IDENTIFIER_PATTERN.pattern}: {identifier!r}",
        )
    return identifier


def catalog_record_key(identifier: str) -> str:
    """Return the catalog record key for a dataset identifier."""
    return f"{CATALOG_PREFIX}/{validate_dataset_identifier(identifier)}.json"


def dataset_identifier_from_catalog_key(key: str) -> str:
    """Return the dataset identifier encoded in a catalog record key."""
    name = key.rsplit("/", maxsplit=1)[-1]
    if not name.endswith(".json"):
        raise StorageAddressError(f"not a catalog record key: {key!r}")
    return validate_dataset_identifier(name[: -len(".json")])


def raster_prefix(identifier: str) -> str:
    """Return the key prefix holding the Icechunk repository of a raster dataset."""
    return f"{RASTER_PREFIX}/{validate_dataset_identifier(identifier)}"


def vector_prefix(identifier: str) -> str:
    """Return the key prefix holding every version of a vector collection."""
    return f"{VECTOR_PREFIX}/{validate_dataset_identifier(identifier)}"


def vector_pointer_key(identifier: str) -> str:
    """Return the key of the published-version pointer of a vector collection."""
    return f"{vector_prefix(identifier)}/{VECTOR_POINTER_NAME}"


def format_vector_version(version: int) -> str:
    """Format a vector version number as a zero-padded directory name."""
    if version < 1 or version > MAXIMUM_VECTOR_VERSION:
        raise StorageAddressError(f"vector version out of range: {version}")
    return f"v{version:0{VECTOR_VERSION_DIGITS}d}"


def parse_vector_version(name: str) -> int:
    """Parse a zero-padded vector version directory name into its version number."""
    match = VECTOR_VERSION_PATTERN.match(name)
    if match is None:
        raise StorageAddressError(f"not a vector version name: {name!r}")
    version = int(match.group(1))
    if version < 1:
        raise StorageAddressError(f"vector version out of range: {version}")
    return version


def vector_version_prefix(identifier: str, version: int) -> str:
    """Return the key prefix of one version of a vector collection."""
    return f"{vector_prefix(identifier)}/versions/{format_vector_version(version)}"


def vector_data_key(identifier: str, version: int) -> str:
    """Return the GeoParquet object key of one version of a vector collection."""
    return f"{vector_version_prefix(identifier, version)}/{VECTOR_DATA_NAME}"
