"""Storage schemes and immutable URI addresses for objects held by a backend."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

from ocs_storage_exploration.storage.errors import StorageAddressError

FORBIDDEN_KEY_SEGMENTS: Final[frozenset[str]] = frozenset({"", ".", ".."})
LOCAL_AUTHORITIES: Final[frozenset[str]] = frozenset({"", "localhost"})


class StorageScheme(StrEnum):
    """URI scheme identifying which backend family owns an address."""

    FILE = "file"
    MEMORY = "memory"
    S3 = "s3"


def parse_storage_scheme(value: str) -> StorageScheme:
    """Parse a scheme name, raising StorageAddressError for anything unsupported."""
    try:
        return StorageScheme(value)
    except ValueError as error:
        raise StorageAddressError(f"unsupported storage scheme: {value!r}") from error


def validate_object_key(key: str) -> str:
    """Validate an object key and return it unchanged."""
    if not key:
        raise StorageAddressError("object key must not be empty")
    if "\\" in key:
        raise StorageAddressError(f"object key must not contain a backslash: {key!r}")
    for segment in key.split("/"):
        if segment in FORBIDDEN_KEY_SEGMENTS:
            raise StorageAddressError(f"object key has an empty or relative segment: {key!r}")
    return key


def join_key_parts(*parts: str) -> str:
    """Join key parts with forward slashes and validate the result."""
    joined = "/".join(part.strip("/") for part in parts if part)
    return validate_object_key(joined)


@dataclass(frozen=True, slots=True)
class StorageAddress:
    """Immutable address of a single object: a scheme, a backend root and an object key."""

    scheme: StorageScheme
    root: str
    key: str

    def __post_init__(self) -> None:
        """Validate the root and the key as soon as the address is built."""
        if not self.root:
            raise StorageAddressError("storage root must not be empty")
        if "\\" in self.root:
            raise StorageAddressError(f"storage root must not contain a backslash: {self.root!r}")
        if "@" in self.root:
            raise StorageAddressError("storage root must not carry credentials")
        if self.scheme is StorageScheme.FILE:
            if not self.root.startswith("/"):
                raise StorageAddressError(f"file storage root must be absolute: {self.root!r}")
        elif "/" in self.root or ":" in self.root:
            raise StorageAddressError(f"storage root must be a plain authority: {self.root!r}")
        validate_object_key(self.key)

    @classmethod
    def from_uri(cls, uri: str) -> StorageAddress:
        """Parse a storage URI, folding the root of a file URI into the leading path."""
        if "\\" in uri:
            raise StorageAddressError(f"storage uri must not contain a backslash: {uri!r}")
        parts = urlsplit(uri)
        if parts.query or parts.fragment:
            raise StorageAddressError(f"storage uri must not carry a query or fragment: {uri!r}")
        if "@" in parts.netloc:
            raise StorageAddressError("storage uri must not carry credentials")
        scheme = parse_storage_scheme(parts.scheme)
        if not parts.path.startswith("/"):
            raise StorageAddressError(f"storage uri must have an absolute path: {uri!r}")
        if scheme is StorageScheme.FILE:
            if parts.netloc not in LOCAL_AUTHORITIES:
                raise StorageAddressError(f"file uri must not have a remote authority: {uri!r}")
            return cls(scheme=scheme, root="/", key=parts.path[1:])
        if not parts.netloc:
            raise StorageAddressError(f"storage uri must name a root: {uri!r}")
        return cls(scheme=scheme, root=parts.netloc, key=parts.path[1:])

    def as_uri(self) -> str:
        """Render the address as a URI that never contains credentials."""
        if self.scheme is StorageScheme.FILE:
            return f"file://{self.root.rstrip('/')}/{self.key}"
        return f"{self.scheme.value}://{self.root}/{self.key}"

    def joined(self, *parts: str) -> StorageAddress:
        """Return a new address with the given parts appended to the key."""
        return replace(self, key=join_key_parts(self.key, *parts))

    def parent(self) -> StorageAddress:
        """Return the address of the containing prefix."""
        head, separator, _ = self.key.rpartition("/")
        if not separator:
            raise StorageAddressError(f"address has no parent prefix: {self.as_uri()}")
        return replace(self, key=head)

    def __str__(self) -> str:
        """Render the address as its URI."""
        return self.as_uri()
