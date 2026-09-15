"""Plugins providing the filesystem, memory and S3 backends built into the service."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ocs_storage_exploration.storage.addresses import StorageScheme
from ocs_storage_exploration.storage.backends.filesystem import FilesystemStorageBackend
from ocs_storage_exploration.storage.backends.memory import MemoryStorageBackend
from ocs_storage_exploration.storage.backends.s3 import S3StorageBackend
from ocs_storage_exploration.storage.plugins import extension, inactive_backend_description

if TYPE_CHECKING:
    from ocs_storage_exploration.settings import Settings
    from ocs_storage_exploration.storage.protocols import StorageBackend
    from ocs_storage_exploration.storage.schemas import BackendDescription


class FilesystemBackendPlugin:
    """Provides the file scheme, backed by one local directory."""

    @extension
    def storage_schemes(self) -> list[str]:
        """Report the file scheme."""
        return [StorageScheme.FILE]

    @extension
    def storage_backend(self, settings: Settings, scheme: str) -> StorageBackend | None:
        """Build the filesystem backend, or None for another scheme."""
        if scheme != StorageScheme.FILE:
            return None
        return FilesystemStorageBackend.from_settings(settings)

    @extension
    def storage_backend_description(self, settings: Settings, scheme: str) -> BackendDescription | None:
        """Describe the file scheme without creating its directory, or None for another scheme."""
        if scheme != StorageScheme.FILE:
            return None
        return inactive_backend_description(settings, scheme)


class MemoryBackendPlugin:
    """Provides the memory scheme, backed by stores living in the process."""

    @extension
    def storage_schemes(self) -> list[str]:
        """Report the memory scheme."""
        return [StorageScheme.MEMORY]

    @extension
    def storage_backend(self, settings: Settings, scheme: str) -> StorageBackend | None:
        """Build the memory backend, or None for another scheme."""
        if scheme != StorageScheme.MEMORY:
            return None
        return MemoryStorageBackend.from_settings(settings)

    @extension
    def storage_backend_description(self, settings: Settings, scheme: str) -> BackendDescription | None:
        """Describe the memory scheme without allocating a store, or None for another scheme."""
        if scheme != StorageScheme.MEMORY:
            return None
        return inactive_backend_description(settings, scheme)


class S3BackendPlugin:
    """Provides the s3 scheme, backed by one bucket of an S3 compatible object store."""

    @extension
    def storage_schemes(self) -> list[str]:
        """Report the s3 scheme."""
        return [StorageScheme.S3]

    @extension
    def storage_backend(self, settings: Settings, scheme: str) -> StorageBackend | None:
        """Build the S3 backend, or None for another scheme."""
        if scheme != StorageScheme.S3:
            return None
        return S3StorageBackend.from_settings(settings)

    @extension
    def storage_backend_description(self, settings: Settings, scheme: str) -> BackendDescription | None:
        """Describe the s3 scheme without reading a credential, or None for another scheme."""
        if scheme != StorageScheme.S3:
            return None
        return inactive_backend_description(settings, scheme)
