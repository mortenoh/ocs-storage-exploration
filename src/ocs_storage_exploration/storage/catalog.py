"""Dataset catalog storing one JSON record per dataset in the backend object store."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final

import obstore
from pydantic import TypeAdapter

from ocs_storage_exploration.storage.addresses import StorageAddress
from ocs_storage_exploration.storage.errors import (
    BackendNotSupportedError,
    DatasetAlreadyExistsError,
    DatasetNotFoundError,
    PublicationConflictError,
)
from ocs_storage_exploration.storage.keys import (
    CATALOG_PREFIX,
    catalog_record_key,
    dataset_identifier_from_catalog_key,
)
from ocs_storage_exploration.storage.objects import create_object, replace_object
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.schemas import CatalogEntry, Dataset, ItemType

RECORD_LABEL: Final[str] = "dataset record"

DATASET_ADAPTER: TypeAdapter[Dataset] = TypeAdapter(Dataset)


class ObjectCatalog:
    """Reads and writes dataset records as JSON objects, remembering nothing between calls."""

    def __init__(self, backend: StorageBackend) -> None:
        """Bind the catalog to one backend."""
        self._backend = backend

    @property
    def backend(self) -> StorageBackend:
        """Backend the records are stored in."""
        return self._backend

    def record_address(self, identifier: str) -> StorageAddress:
        """Return the address of the catalog record of one dataset."""
        return self._backend.address(catalog_record_key(identifier))

    def put(self, dataset: Dataset, *, revision: str | None = None, create: bool = False) -> None:
        """Write a dataset record: as a conditional create, as a compare-and-swap, or as a plain overwrite.

        Passing ``create`` refuses a record another writer created first. Passing the ``revision`` of a
        ``CatalogEntry`` refuses a record that changed since it was read. Passing neither overwrites
        whatever is stored, which is the caller declaring that it does not care who wrote it last.
        """
        if create and revision is not None:
            raise PublicationConflictError("a catalog write is either a create or a compare-and-swap, not both")
        key = self.record_address(dataset.dataset_identifier).key
        payload = DATASET_ADAPTER.dump_json(dataset)
        store = self._backend.object_store()
        if create:
            try:
                create_object(store, key, payload, label=RECORD_LABEL)
            except PublicationConflictError as error:
                raise DatasetAlreadyExistsError(
                    f"dataset {dataset.dataset_identifier!r} already has a catalog record",
                ) from error
            return
        if revision is not None:
            replace_object(store, key, payload, revision, label=RECORD_LABEL)
            return
        obstore.put(store, key, payload)

    def get(self, identifier: str) -> Dataset | None:
        """Read a dataset record, or None when it does not exist."""
        entry = self.get_entry(identifier)
        return None if entry is None else entry.record

    def get_entry(self, identifier: str) -> CatalogEntry | None:
        """Read a dataset record with the revision it was read at, or None when it does not exist."""
        key = self.record_address(identifier).key
        try:
            result = obstore.get(self._backend.object_store(), key)
            revision = result.meta.get("e_tag")
            payload = bytes(result.bytes())
        except FileNotFoundError:
            # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
            return None
        if revision is None:
            raise BackendNotSupportedError(
                f"the object store reports no etag for {RECORD_LABEL} {key!r}, so it cannot be updated safely",
            )
        return CatalogEntry(record=DATASET_ADAPTER.validate_json(payload), revision=revision)

    def require(self, identifier: str) -> Dataset:
        """Read a dataset record or raise DatasetNotFoundError."""
        return self.require_entry(identifier).record

    def require_entry(self, identifier: str) -> CatalogEntry:
        """Read a dataset record with its revision or raise DatasetNotFoundError."""
        entry = self.get_entry(identifier)
        if entry is None:
            raise DatasetNotFoundError(f"no dataset record for {identifier!r}")
        return entry

    def list_datasets(self, item_type: ItemType | None = None) -> list[Dataset]:
        """List dataset records, optionally filtered by item type."""
        datasets: list[Dataset] = []
        for identifier in self.iter_identifiers():
            dataset = self.get(identifier)
            if dataset is None:
                continue
            if item_type is None or dataset.item_type is item_type:
                datasets.append(dataset)
        return datasets

    def delete(self, identifier: str) -> None:
        """Delete a dataset record, raising DatasetNotFoundError when it is absent."""
        address = self.record_address(identifier)
        if not self._backend.exists(address):
            raise DatasetNotFoundError(f"no dataset record for {identifier!r}")
        obstore.delete(self._backend.object_store(), address.key)

    def iter_identifiers(self) -> Iterator[str]:
        """Iterate over the identifiers of every known dataset in key order."""
        for key in self._backend.list_keys(self._backend.address(CATALOG_PREFIX)):
            if key.endswith(".json"):
                yield dataset_identifier_from_catalog_key(key)
