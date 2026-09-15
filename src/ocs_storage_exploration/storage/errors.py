"""Storage error hierarchy with the HTTP status code each failure maps to."""

from __future__ import annotations

from typing import ClassVar


class StorageError(Exception):
    """Base class for every storage failure, carrying the HTTP status code to report."""

    status_code: ClassVar[int] = 500

    def __init__(self, message: str) -> None:
        """Store the human readable message of the failure."""
        super().__init__(message)
        self.message = message


class StorageAddressError(StorageError):
    """Raised when a storage address or URI is malformed or unsafe."""

    status_code: ClassVar[int] = 400


class BackendNotSupportedError(StorageError):
    """Raised when a backend does not implement the requested operation yet."""

    status_code: ClassVar[int] = 501


class BackendUnavailableError(StorageError):
    """Raised when the object store could not be reached before the configured timeouts and retries ran out."""

    status_code: ClassVar[int] = 503


class CrsError(StorageError):
    """Raised when a coordinate reference system cannot be read by pyproj."""

    status_code: ClassVar[int] = 400


class DatasetNotFoundError(StorageError):
    """Raised when no catalog record exists for the requested dataset identifier."""

    status_code: ClassVar[int] = 404


class DatasetAlreadyExistsError(StorageError):
    """Raised when creating a dataset that already has a catalog record."""

    status_code: ClassVar[int] = 409


class ItemTypeMismatchError(StorageError):
    """Raised when a dataset is addressed through the wrong item type endpoint."""

    status_code: ClassVar[int] = 409


class NothingToPublishError(StorageError):
    """Raised when a publication is requested but no committed content exists."""

    status_code: ClassVar[int] = 409


class PublicationConflictError(StorageError):
    """Raised when a publication loses a compare-and-swap against a concurrent writer."""

    status_code: ClassVar[int] = 409


class PublicationSelectorError(StorageError):
    """Raised when a publication names the selector of the other item type."""

    status_code: ClassVar[int] = 422


class SnapshotNotFoundError(StorageError):
    """Raised when a requested snapshot is absent from the repository ancestry."""

    status_code: ClassVar[int] = 404


class RasterContractError(StorageError):
    """Raised when raster input violates the grid or cube contract."""

    status_code: ClassVar[int] = 422


class IngestPathError(StorageError):
    """Raised when an ingest path escapes the configured ingest roots or matches no file."""

    status_code: ClassVar[int] = 400


class FeatureIdentityError(StorageError):
    """Raised when feature identifiers are missing, null or duplicated."""

    status_code: ClassVar[int] = 422


class VectorInputError(StorageError):
    """Raised when vector input is structurally wrong in a way the feature models do not catch."""

    status_code: ClassVar[int] = 422


class FeatureCountGuardError(StorageError):
    """Raised when an unqualified feature read would exceed the configured guard."""

    status_code: ClassVar[int] = 413


class QuerySizeGuardError(StorageError):
    """Raised when a raster query would read more cells than the configured guard."""

    status_code: ClassVar[int] = 413


class SelectableColumnError(StorageError):
    """Raised when a query references a column that was not declared selectable."""

    status_code: ClassVar[int] = 400


class StorageTimeoutError(StorageError):
    """Raised when a storage operation outlived the timeout the async facade bounds it with."""

    status_code: ClassVar[int] = 504
