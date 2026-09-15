"""Service layer composing the backend, the catalog and the raster and vector engines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, assert_never

from ocs_storage_exploration.storage.catalog import ObjectCatalog
from ocs_storage_exploration.storage.errors import ItemTypeMismatchError
from ocs_storage_exploration.storage.models import (
    BackendDescription,
    CoverageDataset,
    Dataset,
    FeatureDataset,
    ItemType,
)
from ocs_storage_exploration.storage.protocols import Catalog, StorageBackend
from ocs_storage_exploration.storage.raster.repository import RasterRepository
from ocs_storage_exploration.storage.registry import build_backend, registered_schemes
from ocs_storage_exploration.storage.vector.collection import VectorCollectionStore

if TYPE_CHECKING:
    from ocs_storage_exploration.settings import Settings

INACTIVE_BACKEND_STATUS: Final[str] = "registered, not the active backend"


@dataclass(frozen=True, slots=True)
class StorageService:
    """Holds the settings, the backend, the catalog and both storage engines of one application."""

    settings: Settings
    backend: StorageBackend
    catalog: Catalog
    raster: RasterRepository
    vector: VectorCollectionStore

    @classmethod
    def from_settings(cls, settings: Settings) -> StorageService:
        """Build the backend, the catalog and both engines from one settings block."""
        backend = build_backend(settings)
        catalog = ObjectCatalog(backend)
        return cls(
            settings=settings,
            backend=backend,
            catalog=catalog,
            raster=RasterRepository(backend, catalog, settings),
            vector=VectorCollectionStore(backend, catalog, settings),
        )

    def describe_backends(self) -> list[BackendDescription]:
        """Describe the active backend, then every other registered scheme as inactive."""
        descriptions = [self.backend.describe()]
        for scheme in registered_schemes():
            if scheme is self.backend.scheme:
                continue
            # An inactive scheme is described from the registry alone, so no directory is created,
            # no client is built and no credential is read to answer the request.
            descriptions.append(
                BackendDescription(
                    scheme=scheme,
                    root="",
                    base_prefix=self.settings.base_prefix,
                    available=False,
                    supports_parquet_filesystem=False,
                    details={"status": INACTIVE_BACKEND_STATUS},
                ),
            )
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
