"""Shared address, listing and deletion behaviour for storage backends."""

from __future__ import annotations

from abc import ABC, abstractmethod

import icechunk
import obstore
import pyarrow.fs
from obstore.store import ObjectStore

from ocs_storage_exploration.storage.addresses import StorageAddress, StorageScheme, join_key_parts
from ocs_storage_exploration.storage.errors import StorageAddressError
from ocs_storage_exploration.storage.schemas import BackendDescription


class BaseStorageBackend(ABC):
    """Base class implementing the address, existence and deletion parts of a backend."""

    def __init__(self, *, scheme: StorageScheme, root: str, base_prefix: str) -> None:
        """Store the scheme, root and base prefix shared by every address of this backend."""
        cleaned_prefix = base_prefix.strip("/")
        if not cleaned_prefix:
            raise StorageAddressError("base prefix must not be empty")
        self._scheme = scheme
        self._root = root
        self._base_prefix = cleaned_prefix

    @property
    def scheme(self) -> StorageScheme:
        """URI scheme this backend serves."""
        return self._scheme

    @property
    def root(self) -> str:
        """Backend root: an absolute directory, a namespace or a bucket name."""
        return self._root

    @property
    def base_prefix(self) -> str:
        """Key prefix every address of this backend starts with."""
        return self._base_prefix

    @property
    def available(self) -> bool:
        """Report whether this backend can serve requests."""
        return True

    @property
    def supports_parquet_filesystem(self) -> bool:
        """Report whether Parquet input and output can use a pyarrow filesystem directly."""
        return True

    def address(self, *parts: str) -> StorageAddress:
        """Build an address below the base prefix of this backend."""
        return StorageAddress(scheme=self._scheme, root=self._root, key=join_key_parts(self._base_prefix, *parts))

    @abstractmethod
    def icechunk_storage(self, address: StorageAddress) -> icechunk.Storage:
        """Resolve an address into the Icechunk storage of a repository."""

    @abstractmethod
    def object_store(self) -> ObjectStore:
        """Return the obstore store used for raw object operations."""

    @abstractmethod
    def parquet_filesystem(self) -> pyarrow.fs.FileSystem | None:
        """Return the pyarrow filesystem for Parquet input and output, or None when buffering is required."""

    @abstractmethod
    def parquet_path(self, address: StorageAddress) -> str:
        """Render an address as the path the pyarrow filesystem expects."""

    def exists(self, address: StorageAddress) -> bool:
        """Report whether a single object exists at the address."""
        try:
            obstore.head(self.object_store(), address.key)
        except FileNotFoundError:
            # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
            return False
        return True

    def list_keys(self, address: StorageAddress) -> list[str]:
        """List every object key at or below the address."""
        prefix = address.key
        listed = obstore.list(self.object_store(), prefix).collect()
        keys = [str(item["path"]) for item in listed]
        return sorted(key for key in keys if key == prefix or key.startswith(f"{prefix}/"))

    def delete_prefix(self, address: StorageAddress) -> int:
        """Delete every object at or below the address and return how many were removed."""
        keys = self.list_keys(address)
        if keys:
            obstore.delete(self.object_store(), keys)
        return len(keys)

    def description_details(self) -> dict[str, str]:
        """Return backend specific description entries, which must never contain a secret."""
        return {}

    def describe(self) -> BackendDescription:
        """Describe the backend without exposing any secret."""
        return BackendDescription(
            scheme=self._scheme,
            root=self._root,
            base_prefix=self._base_prefix,
            available=self.available,
            supports_parquet_filesystem=self.supports_parquet_filesystem,
            details=self.description_details(),
        )
