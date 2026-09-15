"""In-memory backend used by tests and demonstrations, holding everything in the process."""

from __future__ import annotations

from typing import TYPE_CHECKING

import icechunk
import pyarrow.fs
from obstore.store import MemoryStore, ObjectStore

from ocs_storage_exploration.storage.addresses import StorageAddress, StorageScheme
from ocs_storage_exploration.storage.backends.base import BaseStorageBackend

if TYPE_CHECKING:
    from ocs_storage_exploration.settings import Settings

DEFAULT_MEMORY_NAMESPACE = "memory"


class MemoryStorageBackend(BaseStorageBackend):
    """Keeps objects and Icechunk repositories in memory for the lifetime of the instance."""

    def __init__(self, base_prefix: str = "ocs", namespace: str = DEFAULT_MEMORY_NAMESPACE) -> None:
        """Create the in-memory object store and the per-key Icechunk storage cache."""
        super().__init__(scheme=StorageScheme.MEMORY, root=namespace, base_prefix=base_prefix)
        self._store = MemoryStore()
        self._icechunk_storages: dict[str, icechunk.Storage] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> MemoryStorageBackend:
        """Build an in-memory backend from the service settings."""
        return cls(base_prefix=settings.base_prefix)

    @property
    def supports_parquet_filesystem(self) -> bool:
        """Report that Parquet input and output must buffer through obstore."""
        return False

    def icechunk_storage(self, address: StorageAddress) -> icechunk.Storage:
        """Return the in-memory Icechunk storage of an address, creating it once per key."""
        storage = self._icechunk_storages.get(address.key)
        if storage is None:
            storage = icechunk.in_memory_storage()
            self._icechunk_storages[address.key] = storage
        return storage

    def object_store(self) -> ObjectStore:
        """Return the in-memory obstore store."""
        return self._store

    def parquet_filesystem(self) -> pyarrow.fs.FileSystem | None:
        """Report that no pyarrow filesystem exists for in-memory objects."""
        return None

    def parquet_path(self, address: StorageAddress) -> str:
        """Render an address as the object key used when buffering Parquet bytes."""
        return address.key

    def description_details(self) -> dict[str, str]:
        """Describe the in-memory namespace."""
        return {"namespace": self.root, "repositories": str(len(self._icechunk_storages))}
