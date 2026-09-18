"""Key layout for catalog records, raster repositories and vector collections."""

from __future__ import annotations

import re
import uuid
from typing import Final

from ocs_storage_exploration.storage.addresses import join_key_parts
from ocs_storage_exploration.storage.errors import StorageAddressError

CATALOG_PREFIX: Final[str] = "catalog/datasets"
RASTER_PREFIX: Final[str] = "raster"
VECTOR_PREFIX: Final[str] = "vector"
VECTOR_VERSIONS_NAME: Final[str] = "versions"
VECTOR_POINTER_NAME: Final[str] = "current.json"
VECTOR_DATA_NAME: Final[str] = "data.parquet"
VECTOR_RESERVATION_NAME: Final[str] = "reservation.json"
VECTOR_METADATA_NAME: Final[str] = "metadata.json"
VECTOR_VERSION_DIGITS: Final[int] = 5
MAXIMUM_VECTOR_VERSION: Final[int] = 10**VECTOR_VERSION_DIGITS - 1

DATASET_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
GENERATION_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{32}$")
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


def mint_generation_token() -> str:
    """Mint the token naming a fresh storage generation of a dataset, which no other writer can guess."""
    return uuid.uuid4().hex


def validate_generation_token(generation: str) -> str:
    """Validate a storage generation token and return it unchanged."""
    if not GENERATION_TOKEN_PATTERN.match(generation):
        raise StorageAddressError(
            f"storage generation must match {GENERATION_TOKEN_PATTERN.pattern}: {generation!r}",
        )
    return generation


def dataset_generation_prefix(engine_prefix: str, identifier: str, generation: str) -> str:
    """Return the key prefix holding one storage generation of a dataset below the prefix of an engine.

    Every creation of a dataset mints a generation of its own, so the objects of a dataset that was
    deleted and written again under the same identifier never share a prefix with the ones it
    replaced. The catalog record is then the only thing that says where the live data is, and a
    deletion that sweeps the prefix its own record names can no longer reach a later generation.
    """
    return f"{engine_prefix}/{validate_dataset_identifier(identifier)}/{validate_generation_token(generation)}"


def raster_generation_prefix(identifier: str, generation: str) -> str:
    """Return the key prefix holding the Icechunk repository of one storage generation of a coverage."""
    return dataset_generation_prefix(RASTER_PREFIX, identifier, generation)


def vector_generation_prefix(identifier: str, generation: str) -> str:
    """Return the key prefix holding one storage generation of a vector collection."""
    return dataset_generation_prefix(VECTOR_PREFIX, identifier, generation)


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


def vector_pointer_key(storage_prefix: str) -> str:
    """Return the key of the published-version pointer below the storage prefix of a vector collection."""
    return join_key_parts(storage_prefix, VECTOR_POINTER_NAME)


def vector_versions_prefix(storage_prefix: str) -> str:
    """Return the key prefix holding every version directory below the storage prefix of a collection."""
    return join_key_parts(storage_prefix, VECTOR_VERSIONS_NAME)


def vector_version_prefix(storage_prefix: str, version: int) -> str:
    """Return the key prefix of one version below the storage prefix of a vector collection."""
    return join_key_parts(vector_versions_prefix(storage_prefix), format_vector_version(version))


def vector_data_key(storage_prefix: str, version: int) -> str:
    """Return the GeoParquet object key of one version below the storage prefix of a collection."""
    return join_key_parts(vector_version_prefix(storage_prefix, version), VECTOR_DATA_NAME)


def vector_reservation_key(storage_prefix: str, version: int) -> str:
    """Return the object key claiming one version number of a collection before it is written."""
    return join_key_parts(vector_version_prefix(storage_prefix, version), VECTOR_RESERVATION_NAME)


def vector_version_metadata_key(storage_prefix: str, version: int) -> str:
    """Return the object key of the metadata sidecar of one version of a vector collection."""
    return join_key_parts(vector_version_prefix(storage_prefix, version), VECTOR_METADATA_NAME)
