"""Awaitable dataset catalog, reading and writing the same records as the sync one through obstore's async API."""

from __future__ import annotations

from collections.abc import AsyncIterator

from ocs_storage_exploration.storage.addresses import StorageAddress
from ocs_storage_exploration.storage.catalog import (
    RECORD_LABEL,
    already_exists,
    assert_single_write_mode,
    encode_record,
    entry_from_payload,
    identifiers_from_keys,
    no_such_record,
)
from ocs_storage_exploration.storage.errors import PublicationConflictError
from ocs_storage_exploration.storage.keys import CATALOG_PREFIX, catalog_record_key
from ocs_storage_exploration.storage.objects import (
    create_object_async,
    delete_objects_async,
    list_object_keys_async,
    object_exists_async,
    put_object_async,
    read_object_async,
    replace_object_async,
)
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.schemas import CatalogEntry, Dataset, ItemType


class AsyncObjectCatalog:
    """Reads and writes dataset records as awaitables, with the compare-and-swap semantics of ObjectCatalog.

    obstore is the one layer of this service that is natively asynchronous, so the catalog is the one
    component that does not need a worker thread: every call here is an awaitable obstore request on the
    event loop. The key layout, the record encoding and the failure mapping are shared with
    ``ObjectCatalog`` rather than restated, so the two can never drift apart.
    """

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

    async def put(self, dataset: Dataset, *, revision: str | None = None, create: bool = False) -> None:
        """Write a dataset record: as a conditional create, as a compare-and-swap, or as a plain overwrite."""
        assert_single_write_mode(create=create, revision=revision)
        key = self.record_address(dataset.dataset_identifier).key
        payload = encode_record(dataset)
        store = self._backend.object_store()
        if create:
            try:
                await create_object_async(store, key, payload, label=RECORD_LABEL)
            except PublicationConflictError as error:
                raise already_exists(dataset.dataset_identifier) from error
            return
        if revision is not None:
            await replace_object_async(store, key, payload, revision, label=RECORD_LABEL)
            return
        await put_object_async(store, key, payload)

    async def get(self, identifier: str) -> Dataset | None:
        """Read a dataset record, or None when it does not exist."""
        entry = await self.get_entry(identifier)
        return None if entry is None else entry.record

    async def get_entry(self, identifier: str) -> CatalogEntry | None:
        """Read a dataset record with the revision it was read at, or None when it does not exist."""
        key = self.record_address(identifier).key
        payload = await read_object_async(self._backend.object_store(), key)
        return None if payload is None else entry_from_payload(payload, key)

    async def require(self, identifier: str) -> Dataset:
        """Read a dataset record or raise DatasetNotFoundError."""
        return (await self.require_entry(identifier)).record

    async def require_entry(self, identifier: str) -> CatalogEntry:
        """Read a dataset record with its revision or raise DatasetNotFoundError."""
        entry = await self.get_entry(identifier)
        if entry is None:
            raise no_such_record(identifier)
        return entry

    async def list_datasets(self, item_type: ItemType | None = None) -> list[Dataset]:
        """List dataset records, optionally filtered by item type."""
        datasets: list[Dataset] = []
        async for identifier in self.iter_identifiers():
            dataset = await self.get(identifier)
            if dataset is None:
                continue
            if item_type is None or dataset.item_type is item_type:
                datasets.append(dataset)
        return datasets

    async def delete(self, identifier: str) -> None:
        """Delete a dataset record, raising DatasetNotFoundError when it is absent."""
        address = self.record_address(identifier)
        store = self._backend.object_store()
        if not await object_exists_async(store, address.key):
            raise no_such_record(identifier)
        await delete_objects_async(store, address.key)

    async def iter_identifiers(self) -> AsyncIterator[str]:
        """Iterate over the identifiers of every known dataset in key order."""
        prefix = self._backend.address(CATALOG_PREFIX).key
        for identifier in identifiers_from_keys(await list_object_keys_async(self._backend.object_store(), prefix)):
            yield identifier
