"""External plugin adding a null storage scheme to the OCS storage exploration service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import icechunk
import pyarrow.fs
from obstore.store import ObjectStore

from ocs_storage_exploration.storage.backends.base import BaseStorageBackend
from ocs_storage_exploration.storage.errors import BackendNotSupportedError
from ocs_storage_exploration.storage.plugins import extension
from ocs_storage_exploration.storage.schemas import BackendDescription

if TYPE_CHECKING:
    from ocs_storage_exploration.settings import Settings
    from ocs_storage_exploration.storage.addresses import StorageAddress
    from ocs_storage_exploration.storage.protocols import StorageBackend

NULL_SCHEME: Final[str] = "null"
NULL_ROOT: Final[str] = "null"
NULL_STATUS: Final[str] = "example plugin; every operation is refused"


class NullStorageBackend(BaseStorageBackend):
    """Backend of the null scheme: it has a valid address space and no storage behind it."""

    def __init__(self, base_prefix: str = "ocs") -> None:
        """Bind the backend to the null scheme and the given base prefix."""
        super().__init__(scheme=NULL_SCHEME, root=NULL_ROOT, base_prefix=base_prefix)

    @classmethod
    def from_settings(cls, settings: Settings) -> NullStorageBackend:
        """Build the null backend from the service settings."""
        return cls(base_prefix=settings.base_prefix)

    @property
    def available(self) -> bool:
        """Report that the null backend cannot serve requests."""
        return False

    @property
    def supports_parquet_filesystem(self) -> bool:
        """Report that there is no pyarrow filesystem behind the null scheme."""
        return False

    def icechunk_storage(self, address: StorageAddress) -> icechunk.Storage:
        """Refuse to resolve an address into Icechunk storage."""
        raise BackendNotSupportedError(f"the null backend stores nothing: {address}")

    def object_store(self) -> ObjectStore:
        """Refuse to hand out an object store."""
        raise BackendNotSupportedError("the null backend stores nothing")

    def parquet_filesystem(self) -> pyarrow.fs.FileSystem | None:
        """Report that Parquet input and output has no filesystem here."""
        return None

    def parquet_path(self, address: StorageAddress) -> str:
        """Render an address as the key it would have had."""
        return address.key

    def description_details(self) -> dict[str, str]:
        """Describe what the null scheme is for."""
        return {"status": NULL_STATUS}


class NullBackendPlugin:
    """Provides the null scheme to the storage exploration service."""

    @extension
    def storage_schemes(self) -> list[str]:
        """Report the null scheme."""
        return [NULL_SCHEME]

    @extension
    def storage_backend(self, settings: Settings, scheme: str) -> StorageBackend | None:
        """Build the null backend, or None for another scheme."""
        if scheme != NULL_SCHEME:
            return None
        return NullStorageBackend.from_settings(settings)

    @extension
    def storage_backend_description(self, settings: Settings, scheme: str) -> BackendDescription | None:
        """Describe the null scheme without building it, or None for another scheme."""
        if scheme != NULL_SCHEME:
            return None
        return BackendDescription(
            scheme=NULL_SCHEME,
            root="",
            base_prefix=settings.base_prefix,
            available=False,
            supports_parquet_filesystem=False,
            details={"status": NULL_STATUS},
        )


plugin = NullBackendPlugin()
"""The object the entry point resolves to; the host registers it under the name null."""
