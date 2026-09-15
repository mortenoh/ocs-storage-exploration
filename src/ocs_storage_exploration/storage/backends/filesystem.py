"""Filesystem backend resolving addresses to local Icechunk, obstore and pyarrow handles."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import icechunk
import pyarrow.fs
from obstore.store import LocalStore, ObjectStore

from ocs_storage_exploration.storage.addresses import StorageAddress, StorageScheme
from ocs_storage_exploration.storage.backends.base import BaseStorageBackend

if TYPE_CHECKING:
    from ocs_storage_exploration.settings import Settings


class FilesystemStorageBackend(BaseStorageBackend):
    """Stores every dataset below one local directory."""

    def __init__(self, directory: Path | str, base_prefix: str = "ocs") -> None:
        """Create the backend directory and the handles rooted at it."""
        resolved = Path(directory).expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        super().__init__(scheme=StorageScheme.FILE, root=str(resolved), base_prefix=base_prefix)
        self._store = LocalStore(str(resolved), mkdir=True)
        self._filesystem = pyarrow.fs.LocalFileSystem()

    @classmethod
    def from_settings(cls, settings: Settings) -> FilesystemStorageBackend:
        """Build a filesystem backend from the service settings."""
        return cls(directory=settings.data_directory, base_prefix=settings.base_prefix)

    def icechunk_storage(self, address: StorageAddress) -> icechunk.Storage:
        """Resolve an address into local filesystem Icechunk storage."""
        path = Path(self.root) / address.key
        path.parent.mkdir(parents=True, exist_ok=True)
        return icechunk.local_filesystem_storage(str(path))

    def object_store(self) -> ObjectStore:
        """Return the obstore store rooted at the backend directory."""
        return self._store

    def parquet_filesystem(self) -> pyarrow.fs.FileSystem | None:
        """Return the local pyarrow filesystem."""
        return self._filesystem

    def parquet_path(self, address: StorageAddress) -> str:
        """Render an address as an absolute local path."""
        return str(Path(self.root) / address.key)

    def description_details(self) -> dict[str, str]:
        """Describe the backend directory."""
        return {"directory": self.root}
