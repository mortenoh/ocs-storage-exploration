"""Dataset catalog storing one JSON record per dataset in the backend object store."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import obstore
from obstore.exceptions import PreconditionError
from obstore.store import ObjectStore
from pydantic import TypeAdapter

from ocs_storage_exploration.storage.addresses import StorageAddress
from ocs_storage_exploration.storage.errors import DatasetNotFoundError, PublicationConflictError
from ocs_storage_exploration.storage.keys import (
    CATALOG_PREFIX,
    catalog_record_key,
    dataset_identifier_from_catalog_key,
)
from ocs_storage_exploration.storage.models import Dataset, ItemType
from ocs_storage_exploration.storage.protocols import StorageBackend

if TYPE_CHECKING:
    from obstore import PutResult

DATASET_ADAPTER: TypeAdapter[Dataset] = TypeAdapter(Dataset)


class ObjectCatalog:
    """Reads and writes dataset records as JSON objects, with compare-and-swap on known records."""

    def __init__(self, backend: StorageBackend) -> None:
        """Bind the catalog to one backend and start with no remembered etag."""
        self._backend = backend
        self._known_etags: dict[str, str] = {}

    @property
    def backend(self) -> StorageBackend:
        """Backend the records are stored in."""
        return self._backend

    def record_address(self, identifier: str) -> StorageAddress:
        """Return the address of the catalog record of one dataset."""
        return self._backend.address(catalog_record_key(identifier))

    def put(self, dataset: Dataset) -> None:
        """Write a dataset record, using compare-and-swap when this catalog already read it."""
        identifier = dataset.dataset_identifier
        key = self.record_address(identifier).key
        payload = DATASET_ADAPTER.dump_json(dataset)
        store = self._backend.object_store()
        known_etag = self._known_etags.get(identifier)
        if known_etag is None:
            result = obstore.put(store, key, payload)
        else:
            result = self._conditional_put(store, key, payload, known_etag)
        self._remember_etag(identifier, result.get("e_tag"))

    def get(self, identifier: str) -> Dataset | None:
        """Read a dataset record, or None when it does not exist."""
        key = self.record_address(identifier).key
        store = self._backend.object_store()
        try:
            result = obstore.get(store, key)
            etag = result.meta.get("e_tag")
            payload = bytes(result.bytes())
        except FileNotFoundError:
            # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
            self._known_etags.pop(identifier, None)
            return None
        self._remember_etag(identifier, etag)
        return DATASET_ADAPTER.validate_json(payload)

    def require(self, identifier: str) -> Dataset:
        """Read a dataset record or raise DatasetNotFoundError."""
        dataset = self.get(identifier)
        if dataset is None:
            raise DatasetNotFoundError(f"no dataset record for {identifier!r}")
        return dataset

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
        self._known_etags.pop(identifier, None)

    def iter_identifiers(self) -> Iterator[str]:
        """Iterate over the identifiers of every known dataset in key order."""
        for key in self._backend.list_keys(self._backend.address(CATALOG_PREFIX)):
            if key.endswith(".json"):
                yield dataset_identifier_from_catalog_key(key)

    def forget_etags(self) -> None:
        """Drop every remembered etag so the next write overwrites instead of comparing."""
        self._known_etags.clear()

    def _remember_etag(self, identifier: str, etag: str | None) -> None:
        """Record or drop the etag last seen for one dataset record."""
        if etag is None:
            self._known_etags.pop(identifier, None)
        else:
            self._known_etags[identifier] = etag

    def _conditional_put(self, store: ObjectStore, key: str, payload: bytes, etag: str) -> PutResult:
        """Write a record only while it still carries the etag this catalog last saw."""
        try:
            return obstore.put(store, key, payload, mode={"e_tag": etag})
        except PreconditionError as error:
            raise PublicationConflictError(f"dataset record {key!r} changed since it was read") from error
        except FileNotFoundError as error:
            raise PublicationConflictError(f"dataset record {key!r} was deleted since it was read") from error
        except NotImplementedError:
            self._assert_unchanged(store, key, etag)
            return obstore.put(store, key, payload)

    def _assert_unchanged(self, store: ObjectStore, key: str, etag: str) -> None:
        """Emulate compare-and-swap for stores without conditional writes, which is not atomic."""
        try:
            current = obstore.head(store, key)
        except FileNotFoundError as error:
            raise PublicationConflictError(f"dataset record {key!r} was deleted since it was read") from error
        if current.get("e_tag") != etag:
            raise PublicationConflictError(f"dataset record {key!r} changed since it was read")
