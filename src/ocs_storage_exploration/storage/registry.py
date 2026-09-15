"""Registry mapping storage schemes to backend factories, plus dotted-path loading."""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ocs_storage_exploration.storage.addresses import StorageScheme
from ocs_storage_exploration.storage.backends.filesystem import FilesystemStorageBackend
from ocs_storage_exploration.storage.backends.memory import MemoryStorageBackend
from ocs_storage_exploration.storage.backends.s3 import S3StorageBackend
from ocs_storage_exploration.storage.errors import BackendNotSupportedError, StorageError
from ocs_storage_exploration.storage.protocols import StorageBackend

if TYPE_CHECKING:
    from ocs_storage_exploration.settings import Settings

BackendFactory = Callable[["Settings"], StorageBackend]

_BACKEND_FACTORIES: dict[StorageScheme, BackendFactory] = {}


def register_backend(scheme: StorageScheme, factory: BackendFactory) -> None:
    """Register the factory that builds the backend for one storage scheme."""
    _BACKEND_FACTORIES[scheme] = factory


def registered_schemes() -> tuple[StorageScheme, ...]:
    """Return every scheme that currently has a registered factory."""
    return tuple(sorted(_BACKEND_FACTORIES, key=lambda scheme: scheme.value))


def build_backend(settings: Settings) -> StorageBackend:
    """Build the storage backend named by the settings."""
    factory = _BACKEND_FACTORIES.get(settings.backend)
    if factory is None:
        raise BackendNotSupportedError(f"no backend registered for scheme {settings.backend.value!r}")
    return factory(settings)


def build_backend_from_dotted_path(dotted_path: str, parameters: Mapping[str, Any] | None = None) -> StorageBackend:
    """Load a backend class from a dotted path and instantiate it with the parameters it accepts."""
    module_name, separator, attribute_name = dotted_path.rpartition(".")
    if not separator:
        raise StorageError(f"backend path must be a dotted path to a class: {dotted_path!r}")
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise StorageError(f"cannot import backend module {module_name!r}") from error
    backend_class = getattr(module, attribute_name, None)
    if backend_class is None:
        raise StorageError(f"module {module_name!r} has no attribute {attribute_name!r}")
    supplied = dict(parameters or {})
    signature = inspect.signature(backend_class)
    accepts_variable_keywords = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
    )
    keyword_arguments = (
        supplied if accepts_variable_keywords else {k: v for k, v in supplied.items() if k in signature.parameters}
    )
    instance: Any = backend_class(**keyword_arguments)
    if not isinstance(instance, StorageBackend):
        raise StorageError(f"{dotted_path!r} does not implement the StorageBackend protocol")
    return instance


register_backend(StorageScheme.FILE, FilesystemStorageBackend.from_settings)
register_backend(StorageScheme.MEMORY, MemoryStorageBackend.from_settings)
register_backend(StorageScheme.S3, S3StorageBackend.from_settings)
