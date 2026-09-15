"""Extension points a plugin implements to add a storage scheme to the service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pluginkit import Extension, ExtensionPoint, PluginManager

from ocs_storage_exploration.storage.errors import BackendNotSupportedError
from ocs_storage_exploration.storage.schemas import BackendDescription

if TYPE_CHECKING:
    from ocs_storage_exploration.settings import Settings
    from ocs_storage_exploration.storage.protocols import StorageBackend

PROJECT_NAME: Final[str] = "ocs-storage-exploration"
ENTRY_POINT_GROUP: Final[str] = "ocs_storage_exploration.plugins"
INACTIVE_BACKEND_STATUS: Final[str] = "registered, not the active backend"

extension_point = ExtensionPoint(PROJECT_NAME)
extension = Extension(PROJECT_NAME)


class StorageBackendSpecs:
    """The contract a plugin fulfils to provide the backend of one or more storage schemes."""

    @staticmethod
    @extension_point(firstresult=True)
    def storage_backend(settings: Settings, scheme: str) -> StorageBackend | None:
        """Build the backend serving the scheme, or None when this plugin does not provide it."""

    @staticmethod
    @extension_point(firstresult=True)
    def storage_backend_description(settings: Settings, scheme: str) -> BackendDescription | None:
        """Describe the scheme without building a client or reading a credential, or None when not provided."""

    @staticmethod
    @extension_point
    def storage_schemes() -> list[str]:
        """List every scheme this plugin provides."""
        # An extension point is a declaration; the manager reads its signature and never calls it.
        raise NotImplementedError


def inactive_backend_description(settings: Settings, scheme: str) -> BackendDescription:
    """Describe a provided but inactive scheme, touching neither its directory nor its credentials."""
    return BackendDescription(
        scheme=scheme,
        root="",
        base_prefix=settings.base_prefix,
        available=False,
        supports_parquet_filesystem=False,
        details={"status": INACTIVE_BACKEND_STATUS},
    )


def provided_schemes(plugin_manager: PluginManager) -> tuple[str, ...]:
    """Return every scheme the registered plugins provide, deduplicated and sorted."""
    # storage_schemes is a collecting extension point, so one list comes back per plugin.
    collected = plugin_manager.caller(StorageBackendSpecs.storage_schemes)()
    return tuple(sorted({scheme for schemes in collected for scheme in schemes}))


def backend_for_scheme(plugin_manager: PluginManager, settings: Settings, scheme: str) -> StorageBackend:
    """Ask the plugins for the backend of a scheme, raising BackendNotSupportedError when none provides it."""
    backend = plugin_manager.caller(StorageBackendSpecs.storage_backend)(settings=settings, scheme=scheme)
    if backend is None:
        raise BackendNotSupportedError(f"no plugin provides the storage scheme {scheme!r}")
    return backend


def description_for_scheme(plugin_manager: PluginManager, settings: Settings, scheme: str) -> BackendDescription:
    """Ask the plugins to describe a scheme, falling back to the generic inactive description."""
    describe = plugin_manager.caller(StorageBackendSpecs.storage_backend_description)
    description = describe(settings=settings, scheme=scheme)
    if description is None:
        return inactive_backend_description(settings, scheme)
    return description
