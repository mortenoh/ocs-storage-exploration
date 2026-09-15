"""Tests for the plugin seam: the extension points, the facade and external discovery."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pluginkit import PluginManager

from ocs_storage_exploration.main import create_app
from ocs_storage_exploration.settings import Settings
from ocs_storage_exploration.storage.addresses import StorageScheme
from ocs_storage_exploration.storage.backends import (
    FilesystemStorageBackend,
    MemoryStorageBackend,
    build_plugin_manager,
    default_plugin_manager,
)
from ocs_storage_exploration.storage.errors import BackendNotSupportedError
from ocs_storage_exploration.storage.plugins import (
    ENTRY_POINT_GROUP,
    StorageBackendSpecs,
    backend_for_scheme,
    description_for_scheme,
    extension,
    provided_schemes,
)
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.service import StorageService

# The example plugin is a separate package that is deliberately not installed, so that its
# entry point does not add a null scheme to every development checkout. Importing it from
# its source tree is what lets the tests register it and load it through a stubbed entry point.
EXAMPLE_SOURCE = Path(__file__).resolve().parent.parent / "examples" / "plugins" / "ocs-storage-null" / "src"
if str(EXAMPLE_SOURCE) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_SOURCE))

from ocs_storage_null import NULL_SCHEME, NULL_STATUS, NullBackendPlugin, NullStorageBackend  # noqa: E402


class FileClaimingPlugin:
    """A plugin that tries to take the file scheme away from the built-in backend."""

    @extension
    def storage_schemes(self) -> list[str]:
        return [StorageScheme.FILE]

    @extension
    def storage_backend(self, settings: Settings, scheme: str) -> StorageBackend | None:
        if scheme != StorageScheme.FILE:
            return None
        return MemoryStorageBackend.from_settings(settings)


@pytest.fixture
def local_settings(tmp_path: Path) -> Settings:
    return Settings(backend=StorageScheme.FILE, data_directory=tmp_path / "data", base_prefix="ocs")


@pytest.fixture
def isolated_manager() -> PluginManager:
    return build_plugin_manager(load_entry_points=False)


@pytest.fixture
def null_plugin_registered() -> Iterator[PluginManager]:
    plugin_manager = default_plugin_manager()
    plugin_manager.register(NullBackendPlugin(), name=NULL_SCHEME)
    try:
        yield plugin_manager
    finally:
        plugin_manager.unregister(NULL_SCHEME)


def test_the_built_in_plugins_provide_the_built_in_schemes(isolated_manager: PluginManager) -> None:
    assert isolated_manager.plugin_names() == ["filesystem", "memory", "s3"]
    assert provided_schemes(isolated_manager) == (StorageScheme.FILE, StorageScheme.MEMORY, StorageScheme.S3)


def test_a_scheme_no_plugin_provides_is_refused(isolated_manager: PluginManager, local_settings: Settings) -> None:
    with pytest.raises(BackendNotSupportedError):
        backend_for_scheme(isolated_manager, local_settings, "gs")


def test_the_first_registered_plugin_wins_a_contested_scheme(
    isolated_manager: PluginManager,
    local_settings: Settings,
) -> None:
    isolated_manager.register(FileClaimingPlugin(), name="claimant")

    backend = backend_for_scheme(isolated_manager, local_settings, StorageScheme.FILE)

    assert isinstance(backend, FilesystemStorageBackend)


def test_an_in_process_plugin_adds_its_scheme(isolated_manager: PluginManager, local_settings: Settings) -> None:
    isolated_manager.register(NullBackendPlugin(), name=NULL_SCHEME)

    assert NULL_SCHEME in provided_schemes(isolated_manager)
    backend = backend_for_scheme(isolated_manager, local_settings, NULL_SCHEME)
    assert isinstance(backend, NullStorageBackend)
    assert backend.address("catalog").as_uri() == "null://null/ocs/catalog"


def test_an_in_process_plugin_describes_its_scheme(
    isolated_manager: PluginManager,
    local_settings: Settings,
) -> None:
    isolated_manager.register(NullBackendPlugin(), name=NULL_SCHEME)

    description = description_for_scheme(isolated_manager, local_settings, NULL_SCHEME)

    assert description.scheme == NULL_SCHEME
    assert description.available is False
    assert description.details["status"] == NULL_STATUS


def test_the_null_backend_refuses_every_storage_operation(local_settings: Settings) -> None:
    backend = NullStorageBackend.from_settings(local_settings)

    with pytest.raises(BackendNotSupportedError):
        backend.object_store()
    with pytest.raises(BackendNotSupportedError):
        backend.icechunk_storage(backend.address("raster/one"))


def test_a_service_reports_the_scheme_of_an_in_process_plugin(
    isolated_manager: PluginManager,
    local_settings: Settings,
) -> None:
    isolated_manager.register(NullBackendPlugin(), name=NULL_SCHEME)

    service = StorageService.from_settings(local_settings, isolated_manager)

    assert service.plugin_manager is isolated_manager
    schemes = [description.scheme for description in service.describe_backends()]
    assert schemes[0] == StorageScheme.FILE
    assert NULL_SCHEME in schemes


def test_the_backends_endpoint_lists_the_scheme_of_a_registered_plugin(
    null_plugin_registered: PluginManager,
    local_settings: Settings,
) -> None:
    with TestClient(create_app(settings=local_settings)) as client:
        items = client.get("/api/v1/backends").json()["items"]

    listed = {item["scheme"]: item for item in items}
    assert listed[NULL_SCHEME]["available"] is False
    assert listed[NULL_SCHEME]["details"]["status"] == NULL_STATUS
    assert listed[StorageScheme.FILE]["available"] is True


def test_an_entry_point_is_discovered_without_installing_the_distribution(
    monkeypatch: pytest.MonkeyPatch,
    local_settings: Settings,
) -> None:
    advertised = EntryPoint(name=NULL_SCHEME, value="ocs_storage_null:plugin", group=ENTRY_POINT_GROUP)

    def stubbed_entry_points(*, group: str) -> tuple[EntryPoint, ...]:
        return (advertised,) if group == ENTRY_POINT_GROUP else ()

    monkeypatch.setattr("pluginkit.manager.entry_points", stubbed_entry_points)

    plugin_manager = build_plugin_manager()

    assert plugin_manager.plugin_names() == ["filesystem", "memory", "s3", NULL_SCHEME]
    assert NULL_SCHEME in provided_schemes(plugin_manager)
    assert isinstance(backend_for_scheme(plugin_manager, local_settings, NULL_SCHEME), NullStorageBackend)


def test_the_extension_points_are_the_declared_contract(isolated_manager: PluginManager) -> None:
    assert isolated_manager.caller(StorageBackendSpecs.storage_backend).name == "storage_backend"
    assert isolated_manager.caller(StorageBackendSpecs.storage_backend_description).name == (
        "storage_backend_description"
    )
    assert isolated_manager.caller(StorageBackendSpecs.storage_schemes).name == "storage_schemes"
