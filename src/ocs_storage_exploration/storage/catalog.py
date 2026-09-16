"""Dataset catalog storing one JSON record per dataset in the backend object store."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Final

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
from ocs_storage_exploration.storage.objects import (
    ObjectPayload,
    create_object,
    delete_objects,
    put_object,
    read_object,
    replace_object,
)
from ocs_storage_exploration.storage.protocols import StorageBackend
from ocs_storage_exploration.storage.schemas import CatalogEntry, Dataset, ItemType

RECORD_LABEL: Final[str] = "dataset record"

DATASET_ADAPTER: TypeAdapter[Dataset] = TypeAdapter(Dataset)


def encode_record(dataset: Dataset) -> bytes:
    """Render a dataset record as the JSON bytes stored under its key."""
    return DATASET_ADAPTER.dump_json(dataset)


def assert_single_write_mode(*, create: bool, revision: str | None) -> None:
    """Refuse a write that asks to be both a conditional create and a compare-and-swap."""
    if create and revision is not None:
        raise PublicationConflictError("a catalog write is either a create or a compare-and-swap, not both")


def already_exists(identifier: str) -> DatasetAlreadyExistsError:
    """Build the failure reported when a record was created by another writer first."""
    return DatasetAlreadyExistsError(f"dataset {identifier!r} already has a catalog record")


def no_such_record(identifier: str) -> DatasetNotFoundError:
    """Build the failure reported when no catalog record exists for an identifier."""
    return DatasetNotFoundError(f"no dataset record for {identifier!r}")


def entry_from_payload(payload: ObjectPayload, key: str) -> CatalogEntry:
    """Build the catalog entry of a record that was read, refusing a store that reports no etag."""
    if payload.revision is None:
        raise BackendNotSupportedError(
            f"the object store reports no etag for {RECORD_LABEL} {key!r}, so it cannot be updated safely",
        )
    return CatalogEntry(record=DATASET_ADAPTER.validate_json(payload.payload), revision=payload.revision)


def identifiers_from_keys(keys: Iterable[str]) -> Iterator[str]:
    """Read the dataset identifier out of every catalog record key, skipping anything else."""
    for key in keys:
        if key.endswith(".json"):
            yield dataset_identifier_from_catalog_key(key)


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
        assert_single_write_mode(create=create, revision=revision)
        key = self.record_address(dataset.dataset_identifier).key
        payload = encode_record(dataset)
        store = self._backend.object_store()
        if create:
            try:
                create_object(store, key, payload, label=RECORD_LABEL)
            except PublicationConflictError as error:
                raise already_exists(dataset.dataset_identifier) from error
            return
        if revision is not None:
            replace_object(store, key, payload, revision, label=RECORD_LABEL)
            return
        put_object(store, key, payload)

    def get(self, identifier: str) -> Dataset | None:
        """Read a dataset record, or None when it does not exist."""
        entry = self.get_entry(identifier)
        return None if entry is None else entry.record

    def get_entry(self, identifier: str) -> CatalogEntry | None:
        """Read a dataset record with the revision it was read at, or None when it does not exist."""
        key = self.record_address(identifier).key
        payload = read_object(self._backend.object_store(), key)
        return None if payload is None else entry_from_payload(payload, key)

    def require(self, identifier: str) -> Dataset:
        """Read a dataset record or raise DatasetNotFoundError."""
        return self.require_entry(identifier).record

    def require_entry(self, identifier: str) -> CatalogEntry:
        """Read a dataset record with its revision or raise DatasetNotFoundError."""
        entry = self.get_entry(identifier)
        if entry is None:
            raise no_such_record(identifier)
        return entry

    def list_datasets(self, item_type: ItemType | None = None) -> list[Dataset]:
        """List dataset records, optionally filtered by item type, skipping datasets being deleted."""
        datasets: list[Dataset] = []
        for identifier in self.iter_identifiers():
            dataset = self.get(identifier)
            if dataset is None or dataset.is_deleting:
                # A deletion marks its record before it sweeps; from here on the dataset is gone.
                continue
            if item_type is None or dataset.item_type is item_type:
                datasets.append(dataset)
        return datasets

    def delete(self, identifier: str) -> None:
        """Delete a dataset record, raising DatasetNotFoundError when it is absent."""
        address = self.record_address(identifier)
        if not self._backend.exists(address):
            raise no_such_record(identifier)
        delete_objects(self._backend.object_store(), address.key)

    def iter_identifiers(self) -> Iterator[str]:
        """Iterate over the identifiers of every known dataset in key order."""
        return identifiers_from_keys(self._backend.list_keys(self._backend.address(CATALOG_PREFIX)))
