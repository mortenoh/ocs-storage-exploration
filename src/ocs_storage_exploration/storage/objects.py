"""Conditional object writes shared by the dataset catalog and the vector pointer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import obstore
from obstore.exceptions import AlreadyExistsError, PreconditionError
from obstore.store import LocalStore, ObjectStore

from ocs_storage_exploration.storage.errors import BackendNotSupportedError, PublicationConflictError

if TYPE_CHECKING:
    from obstore import PutResult


def create_object(store: ObjectStore, key: str, payload: bytes, *, label: str) -> PutResult:
    """Create an object, refusing to overwrite one a concurrent writer created first."""
    try:
        return obstore.put(store, key, payload, mode="create")
    except AlreadyExistsError as error:
        raise PublicationConflictError(f"{label} {key!r} was created by a concurrent publication") from error


def replace_object(store: ObjectStore, key: str, payload: bytes, etag: str, *, label: str) -> PutResult:
    """Replace an object only while it still carries the etag the caller last read."""
    try:
        return obstore.put(store, key, payload, mode={"e_tag": etag})
    except PreconditionError as error:
        raise PublicationConflictError(f"{label} {key!r} changed since it was read") from error
    except FileNotFoundError as error:
        # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
        raise PublicationConflictError(f"{label} {key!r} was deleted since it was read") from error
    except NotImplementedError as error:
        # Only obstore's LocalStore lacks conditional put; every other store must use the real one.
        if not isinstance(store, LocalStore):
            raise BackendNotSupportedError(
                f"{type(store).__name__} reports no conditional put, so {label} {key!r} cannot be written safely",
            ) from error
        assert_object_unchanged(store, key, etag, label=label)
        return obstore.put(store, key, payload)


def assert_object_unchanged(store: ObjectStore, key: str, etag: str, *, label: str) -> None:
    """Emulate compare-and-swap for a store without conditional writes, which is not atomic."""
    try:
        current = obstore.head(store, key)
    except FileNotFoundError as error:
        # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
        raise PublicationConflictError(f"{label} {key!r} was deleted since it was read") from error
    if current.get("e_tag") != etag:
        raise PublicationConflictError(f"{label} {key!r} changed since it was read")
