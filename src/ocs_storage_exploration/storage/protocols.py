"""Protocols for the two pluggable storage seams: the backend and the dataset catalog."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Protocol, runtime_checkable

import icechunk
import pyarrow.fs
from obstore.store import ObjectStore

from ocs_storage_exploration.storage.addresses import StorageAddress
from ocs_storage_exploration.storage.schemas import BackendDescription, CatalogEntry, Dataset, ItemType


@runtime_checkable
class StorageBackend(Protocol):
    """Resolves one settings block into Icechunk, obstore and pyarrow handles."""

    @property
    def scheme(self) -> str:
        """URI scheme this backend serves."""
        ...

    @property
    def root(self) -> str:
        """Backend root: an absolute directory, a namespace or a bucket name."""
        ...

    @property
    def base_prefix(self) -> str:
        """Key prefix every address of this backend starts with."""
        ...

    def address(self, *parts: str) -> StorageAddress:
        """Build an address below the base prefix of this backend."""
        ...

    def icechunk_storage(self, address: StorageAddress) -> icechunk.Storage:
        """Resolve an address into the Icechunk storage of a repository."""
        ...

    def repository_config(self) -> icechunk.RepositoryConfig | None:
        """Return the Icechunk repository configuration this backend imposes, or None to keep the defaults."""
        ...

    def object_store(self) -> ObjectStore:
        """Return the obstore store used for raw object operations."""
        ...

    def parquet_filesystem(self) -> pyarrow.fs.FileSystem | None:
        """Return the pyarrow filesystem for Parquet input and output, or None when buffering is required."""
        ...

    def parquet_path(self, address: StorageAddress) -> str:
        """Render an address as the path the pyarrow filesystem expects."""
        ...

    def exists(self, address: StorageAddress) -> bool:
        """Report whether a single object exists at the address."""
        ...

    def list_keys(self, address: StorageAddress) -> list[str]:
        """List every object key at or below the address."""
        ...

    def delete_prefix(self, address: StorageAddress) -> int:
        """Delete every object at or below the address and return how many were removed."""
        ...

    def describe(self) -> BackendDescription:
        """Describe the backend without exposing any secret."""
        ...


@runtime_checkable
class Catalog(Protocol):
    """Stores the record that makes a dataset exist."""

    def put(self, dataset: Dataset, *, revision: str | None = None, create: bool = False) -> None:
        """Write a dataset record as a conditional create, as a compare-and-swap, or as a plain overwrite."""
        ...

    def get(self, identifier: str) -> Dataset | None:
        """Read a dataset record, or None when it does not exist."""
        ...

    def get_entry(self, identifier: str) -> CatalogEntry | None:
        """Read a dataset record with the revision it was read at, or None when it does not exist."""
        ...

    def require(self, identifier: str) -> Dataset:
        """Read a dataset record or raise DatasetNotFoundError."""
        ...

    def require_entry(self, identifier: str) -> CatalogEntry:
        """Read a dataset record with its revision or raise DatasetNotFoundError."""
        ...

    def list_datasets(self, item_type: ItemType | None = None) -> list[Dataset]:
        """List dataset records, optionally filtered by item type."""
        ...

    def delete(self, identifier: str) -> None:
        """Delete a dataset record."""
        ...

    def iter_identifiers(self) -> Iterator[str]:
        """Iterate over the identifiers of every known dataset."""
        ...


@runtime_checkable
class AsyncCatalog(Protocol):
    """Stores the record that makes a dataset exist, one awaitable per call."""

    async def put(self, dataset: Dataset, *, revision: str | None = None, create: bool = False) -> None:
        """Write a dataset record as a conditional create, as a compare-and-swap, or as a plain overwrite."""
        ...

    async def get(self, identifier: str) -> Dataset | None:
        """Read a dataset record, or None when it does not exist."""
        ...

    async def get_entry(self, identifier: str) -> CatalogEntry | None:
        """Read a dataset record with the revision it was read at, or None when it does not exist."""
        ...

    async def require(self, identifier: str) -> Dataset:
        """Read a dataset record or raise DatasetNotFoundError."""
        ...

    async def require_entry(self, identifier: str) -> CatalogEntry:
        """Read a dataset record with its revision or raise DatasetNotFoundError."""
        ...

    async def list_datasets(self, item_type: ItemType | None = None) -> list[Dataset]:
        """List dataset records, optionally filtered by item type."""
        ...

    async def delete(self, identifier: str) -> None:
        """Delete a dataset record."""
        ...

    def iter_identifiers(self) -> AsyncIterator[str]:
        """Iterate over the identifiers of every known dataset."""
        ...
