"""Object reads, writes and listings, sync and async, shared by the dataset catalog and the vector engine."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import obstore
from obstore.exceptions import AlreadyExistsError, PreconditionError
from obstore.store import LocalStore, ObjectStore

from ocs_storage_exploration.storage.errors import BackendNotSupportedError, PublicationConflictError
from ocs_storage_exploration.storage.failures import backend_transport_failures

if TYPE_CHECKING:
    from obstore import PutResult


@dataclass(frozen=True, slots=True)
class ObjectPayload:
    """Bytes of one object together with the revision they were read at."""

    payload: bytes
    revision: str | None


def created_by_another_writer(key: str, label: str) -> PublicationConflictError:
    """Build the conflict reported when a concurrent writer created an object first."""
    return PublicationConflictError(f"{label} {key!r} was created by a concurrent publication")


def changed_since_read(key: str, label: str) -> PublicationConflictError:
    """Build the conflict reported when an object no longer carries the etag it was read at."""
    return PublicationConflictError(f"{label} {key!r} changed since it was read")


def deleted_since_read(key: str, label: str) -> PublicationConflictError:
    """Build the conflict reported when an object was deleted between the read and the write."""
    return PublicationConflictError(f"{label} {key!r} was deleted since it was read")


def unsupported_conditional_put(store: ObjectStore, key: str, label: str) -> BackendNotSupportedError:
    """Build the refusal reported when a store other than LocalStore has no conditional put."""
    return BackendNotSupportedError(
        f"{type(store).__name__} reports no conditional put, so {label} {key!r} cannot be written safely",
    )


def select_keys_below(keys: Iterable[str], prefix: str) -> list[str]:
    """Keep the keys at or below a prefix, in key order."""
    return sorted(key for key in keys if key == prefix or key.startswith(f"{prefix}/"))


def create_object(store: ObjectStore, key: str, payload: bytes, *, label: str) -> PutResult:
    """Create an object, refusing to overwrite one a concurrent writer created first."""
    try:
        with backend_transport_failures(f"creating {label} {key!r}"):
            return obstore.put(store, key, payload, mode="create")
    except AlreadyExistsError as error:
        raise created_by_another_writer(key, label) from error


async def create_object_async(store: ObjectStore, key: str, payload: bytes, *, label: str) -> PutResult:
    """Create an object without blocking the event loop, refusing one a concurrent writer created first."""
    try:
        with backend_transport_failures(f"creating {label} {key!r}"):
            return await obstore.put_async(store, key, payload, mode="create")
    except AlreadyExistsError as error:
        raise created_by_another_writer(key, label) from error


def create_object_if_absent(store: ObjectStore, key: str, payload: bytes) -> PutResult | None:
    """Create an object and report None rather than raising when a concurrent writer created it first.

    Unlike the conditional replace below, create mode is implemented by every store this service
    uses, obstore's ``LocalStore`` included, so a claim made this way is atomic on all of them.
    """
    try:
        with backend_transport_failures(f"creating {key!r}"):
            return obstore.put(store, key, payload, mode="create")
    except AlreadyExistsError:
        return None


def replace_object(store: ObjectStore, key: str, payload: bytes, etag: str, *, label: str) -> PutResult:
    """Replace an object only while it still carries the etag the caller last read."""
    try:
        with backend_transport_failures(f"replacing {label} {key!r}"):
            return obstore.put(store, key, payload, mode={"e_tag": etag})
    except PreconditionError as error:
        raise changed_since_read(key, label) from error
    except FileNotFoundError as error:
        # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
        raise deleted_since_read(key, label) from error
    except NotImplementedError as error:
        # Only obstore's LocalStore lacks conditional put; every other store must use the real one.
        if not isinstance(store, LocalStore):
            raise unsupported_conditional_put(store, key, label) from error
        assert_object_unchanged(store, key, etag, label=label)
        return put_object(store, key, payload)


async def replace_object_async(store: ObjectStore, key: str, payload: bytes, etag: str, *, label: str) -> PutResult:
    """Replace an object without blocking the event loop, only while it still carries the etag read."""
    try:
        with backend_transport_failures(f"replacing {label} {key!r}"):
            return await obstore.put_async(store, key, payload, mode={"e_tag": etag})
    except PreconditionError as error:
        raise changed_since_read(key, label) from error
    except FileNotFoundError as error:
        # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
        raise deleted_since_read(key, label) from error
    except NotImplementedError as error:
        # Only obstore's LocalStore lacks conditional put; every other store must use the real one.
        if not isinstance(store, LocalStore):
            raise unsupported_conditional_put(store, key, label) from error
        await assert_object_unchanged_async(store, key, etag, label=label)
        return await put_object_async(store, key, payload)


def assert_object_unchanged(store: ObjectStore, key: str, etag: str, *, label: str) -> None:
    """Emulate compare-and-swap for a store without conditional writes, which is not atomic."""
    try:
        with backend_transport_failures(f"reading {label} {key!r}"):
            current = obstore.head(store, key)
    except FileNotFoundError as error:
        # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
        raise deleted_since_read(key, label) from error
    if current.get("e_tag") != etag:
        raise changed_since_read(key, label)


async def assert_object_unchanged_async(store: ObjectStore, key: str, etag: str, *, label: str) -> None:
    """Emulate compare-and-swap without blocking the event loop, which is just as non-atomic."""
    try:
        with backend_transport_failures(f"reading {label} {key!r}"):
            current = await obstore.head_async(store, key)
    except FileNotFoundError as error:
        # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
        raise deleted_since_read(key, label) from error
    if current.get("e_tag") != etag:
        raise changed_since_read(key, label)


def put_object(store: ObjectStore, key: str, payload: bytes) -> PutResult:
    """Write an object unconditionally, overwriting whatever is stored under the key."""
    with backend_transport_failures(f"writing {key!r}"):
        return obstore.put(store, key, payload)


async def put_object_async(store: ObjectStore, key: str, payload: bytes) -> PutResult:
    """Write an object unconditionally without blocking the event loop."""
    with backend_transport_failures(f"writing {key!r}"):
        return await obstore.put_async(store, key, payload)


def read_object(store: ObjectStore, key: str) -> ObjectPayload | None:
    """Read an object with the revision it was read at, or None when it does not exist."""
    try:
        with backend_transport_failures(f"reading {key!r}"):
            result = obstore.get(store, key)
            return ObjectPayload(payload=bytes(result.bytes()), revision=result.meta.get("e_tag"))
    except FileNotFoundError:
        # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
        return None


async def read_object_async(store: ObjectStore, key: str) -> ObjectPayload | None:
    """Read an object and its revision without blocking the event loop, or None when it does not exist."""
    try:
        with backend_transport_failures(f"reading {key!r}"):
            result = await obstore.get_async(store, key)
            payload = bytes(await result.bytes_async())
    except FileNotFoundError:
        # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
        return None
    return ObjectPayload(payload=payload, revision=result.meta.get("e_tag"))


def object_exists(store: ObjectStore, key: str) -> bool:
    """Report whether a single object exists under the key."""
    try:
        with backend_transport_failures(f"reading {key!r}"):
            obstore.head(store, key)
    except FileNotFoundError:
        # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
        return False
    return True


async def object_exists_async(store: ObjectStore, key: str) -> bool:
    """Report whether a single object exists under the key, without blocking the event loop."""
    try:
        with backend_transport_failures(f"reading {key!r}"):
            await obstore.head_async(store, key)
    except FileNotFoundError:
        # obstore 0.11 reports a missing object as the builtin FileNotFoundError.
        return False
    return True


def delete_objects(store: ObjectStore, keys: str | Sequence[str]) -> None:
    """Delete one object or a batch of them."""
    with backend_transport_failures("deleting objects"):
        obstore.delete(store, keys)


async def delete_objects_async(store: ObjectStore, keys: str | Sequence[str]) -> None:
    """Delete one object or a batch of them without blocking the event loop."""
    with backend_transport_failures("deleting objects"):
        await obstore.delete_async(store, keys)


def list_object_keys(store: ObjectStore, prefix: str) -> list[str]:
    """List every object key at or below a prefix, in key order."""
    with backend_transport_failures(f"listing {prefix!r}"):
        listed = obstore.list(store, prefix).collect()
    return select_keys_below((str(item["path"]) for item in listed), prefix)


async def list_object_keys_async(store: ObjectStore, prefix: str) -> list[str]:
    """List every object key at or below a prefix without blocking the event loop, in key order."""
    keys: list[str] = []
    with backend_transport_failures(f"listing {prefix!r}"):
        async for chunk in obstore.list(store, prefix):
            keys.extend(str(item["path"]) for item in chunk)
    return select_keys_below(keys, prefix)
