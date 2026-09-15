"""Service layer composing the backend, the catalog and the raster and vector engines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never

from pluginkit import PluginManager

from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import ItemTypeMismatchError
from ocs_storage_exploration.storage.plugins import (
    backend_for_scheme,
    description_for_scheme,
    provided_schemes,
)
from ocs_storage_exploration.storage.protocols import Catalog, StorageBackend
from ocs_storage_exploration.storage.raster.repository import RasterRepository
from ocs_storage_exploration.storage.registry import default_plugin_manager
from ocs_storage_exploration.storage.schemas import (
    BackendDescription,
    CoverageDataset,
    Dataset,
    FeatureDataset,
    ItemType,
)
from ocs_storage_exploration.storage.vector.collection import VectorCollectionStore

if TYPE_CHECKING:
    from ocs_storage_exploration.settings import Settings


@dataclass(frozen=True, slots=True)
class StorageService:
    """Holds the settings, the plugins, the backend, the catalog and both storage engines of one application."""

    settings: Settings
    plugin_manager: PluginManager
    backend: StorageBackend
    catalog: Catalog
    raster: RasterRepository
    vector: VectorCollectionStore

    @classmethod
    def from_settings(cls, settings: Settings, plugin_manager: PluginManager | None = None) -> StorageService:
        """Build the plugin manager, the backend, the catalog and both engines from one settings block."""
        plugins = plugin_manager if plugin_manager is not None else default_plugin_manager()
        backend = backend_for_scheme(plugins, settings, settings.backend)
        catalog = ObjectCatalog(backend)
        return cls(
            settings=settings,
            plugin_manager=plugins,
            backend=backend,
            catalog=catalog,
            raster=RasterRepository(backend, catalog, settings),
            vector=VectorCollectionStore(backend, catalog, settings),
        )

    def describe_backends(self) -> list[BackendDescription]:
        """Describe the active backend, then every other scheme the plugins provide as inactive."""
        descriptions = [self.backend.describe()]
        for scheme in provided_schemes(self.plugin_manager):
            if scheme == self.backend.scheme:
                continue
            # An inactive scheme is described by its own plugin, so no directory is created,
            # no client is built and no credential is read to answer the request.
            descriptions.append(description_for_scheme(self.plugin_manager, self.settings, scheme))
        return descriptions

    def list_datasets(self, item_type: ItemType | None = None) -> list[Dataset]:
        """List every dataset record, optionally filtered by item type."""
        return self.catalog.list_datasets(item_type)

    def get_dataset(self, dataset_identifier: str) -> Dataset:
        """Read one dataset record or raise DatasetNotFoundError."""
        return self.catalog.require(dataset_identifier)

    def require_coverage(self, dataset_identifier: str) -> CoverageDataset:
        """Read the record of a coverage, refusing a dataset of another item type."""
        record = self.catalog.require(dataset_identifier)
        if not isinstance(record, CoverageDataset):
            raise ItemTypeMismatchError(f"dataset {dataset_identifier!r} is not a coverage")
        return record

    def require_collection(self, dataset_identifier: str) -> FeatureDataset:
        """Read the record of a vector collection, refusing a dataset of another item type."""
        record = self.catalog.require(dataset_identifier)
        if not isinstance(record, FeatureDataset):
            raise ItemTypeMismatchError(f"dataset {dataset_identifier!r} is not a vector collection")
        return record

    def delete_dataset(self, dataset_identifier: str) -> Dataset:
        """Delete a dataset through the engine its item type names and return the record that was deleted."""
        record = self.catalog.require(dataset_identifier)
        match record:
            case CoverageDataset():
                self.raster.delete(record.dataset_identifier)
            case FeatureDataset():
                self.vector.delete(record.dataset_identifier)
            case _:  # pragma: no cover - the dataset union has no other member
                assert_never(record)
        return record
