"""Facade over the pluginkit manager that assembles the storage backend plugins."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from pluginkit import PluginManager

from ocs_storage_exploration.storage.backends.plugins import (
    FilesystemBackendPlugin,
    MemoryBackendPlugin,
    S3BackendPlugin,
)
from ocs_storage_exploration.storage.plugins import (
    ENTRY_POINT_GROUP,
    PROJECT_NAME,
    StorageBackendSpecs,
    backend_for_scheme,
    provided_schemes,
)

if TYPE_CHECKING:
    from ocs_storage_exploration.settings import Settings
    from ocs_storage_exploration.storage.protocols import StorageBackend


def build_plugin_manager(*, load_entry_points: bool = True) -> PluginManager:
    """Build a plugin manager holding the three built-in backends and the installed external plugins."""
    plugin_manager = PluginManager(PROJECT_NAME)
    plugin_manager.add_extension_points(StorageBackendSpecs)
    # The built-ins are registered first, and a firstresult extension point takes the first
    # non-None answer, so a plugin claiming a built-in scheme never displaces the built-in.
    plugin_manager.register(FilesystemBackendPlugin(), name="filesystem")
    plugin_manager.register(MemoryBackendPlugin(), name="memory")
    plugin_manager.register(S3BackendPlugin(), name="s3")
    if load_entry_points:
        plugin_manager.load_entrypoints(ENTRY_POINT_GROUP)
    return plugin_manager


@lru_cache(maxsize=1)
def default_plugin_manager() -> PluginManager:
    """Return the process wide plugin manager, built once and shared by every storage service."""
    return build_plugin_manager()


def registered_schemes() -> tuple[str, ...]:
    """Return every scheme the registered plugins provide."""
    return provided_schemes(default_plugin_manager())


def build_backend(settings: Settings) -> StorageBackend:
    """Build the storage backend named by the settings."""
    return backend_for_scheme(default_plugin_manager(), settings, settings.backend)
