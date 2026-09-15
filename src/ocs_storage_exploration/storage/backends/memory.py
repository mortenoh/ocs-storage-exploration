"""In-memory backend used by tests and demonstrations, holding everything in the process."""

from __future__ import annotations

import threading
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
        # One application serves several requests on the threadpool, so the cache is guarded:
        # two threads creating the same repository would otherwise each keep their own store.
        self._storage_lock = threading.Lock()

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
        with self._storage_lock:
            storage = self._icechunk_storages.get(address.key)
            if storage is None:
                storage = icechunk.in_memory_storage()
                self._icechunk_storages[address.key] = storage
            return storage

    def object_store(self) -> ObjectStore:
        """Return the in-memory obstore store."""
        return self._store

    def delete_prefix(self, address: StorageAddress) -> int:
        """Delete every object below the address and drop the Icechunk storages cached under it."""
        removed = super().delete_prefix(address)
        # The repositories live in the storage cache rather than in the object store, so a deletion
        # that leaves them behind hands the next dataset of the same name the old history.
        prefix = address.key
        with self._storage_lock:
            dropped = [key for key in self._icechunk_storages if key == prefix or key.startswith(f"{prefix}/")]
            for key in dropped:
                del self._icechunk_storages[key]
        return removed + len(dropped)

    def parquet_filesystem(self) -> pyarrow.fs.FileSystem | None:
        """Report that no pyarrow filesystem exists for in-memory objects."""
        return None

    def parquet_path(self, address: StorageAddress) -> str:
        """Render an address as the object key used when buffering Parquet bytes."""
        return address.key

    def description_details(self) -> dict[str, str]:
        """Describe the in-memory namespace."""
        with self._storage_lock:
            repository_count = len(self._icechunk_storages)
        return {"namespace": self.root, "repositories": str(repository_count)}
