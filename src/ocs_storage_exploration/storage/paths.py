"""Resolves local ingest paths and globs, refusing anything outside the configured ingest roots."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

from ocs_storage_exploration.storage.errors import IngestPathError

# A pattern carrying one of these is expanded as a glob; anything else is taken as a literal path.
GLOB_CHARACTERS: Final[re.Pattern[str]] = re.compile(r"[*?\[]")

# A store with one of these suffixes is a directory the readers open as one dataset, so it is
# ingestible even though it is not a file. A directory with any other name stays refused.
DIRECTORY_STORE_SUFFIXES: Final[frozenset[str]] = frozenset({".zarr"})


def resolve_ingest_roots(roots: Sequence[Path], *, working_directory: Path | None = None) -> tuple[Path, ...]:
    """Return the ingest roots as absolute directories, resolved against the working directory."""
    base = (working_directory if working_directory is not None else Path.cwd()).resolve()
    resolved: list[Path] = []
    for root in roots:
        candidate = (root if root.is_absolute() else base / root).resolve()
        if candidate not in resolved:
            resolved.append(candidate)
    return tuple(resolved)


def resolve_ingest_path(
    pattern: str,
    *,
    roots: Sequence[Path],
    working_directory: Path | None = None,
) -> Path:
    """Resolve one ingest pattern that must name exactly one readable file."""
    matches = resolve_ingest_paths([pattern], roots=roots, working_directory=working_directory)
    if len(matches) != 1:
        raise IngestPathError(f"path {pattern!r} matches {len(matches)} files, but exactly one is required")
    return matches[0]


def resolve_ingest_paths(
    patterns: Iterable[str],
    *,
    roots: Sequence[Path],
    working_directory: Path | None = None,
) -> list[Path]:
    """Expand ingest patterns against the working directory, keeping only readable files under an ingest root."""
    base = (working_directory if working_directory is not None else Path.cwd()).resolve()
    allowed = resolve_ingest_roots(roots, working_directory=base)
    if not allowed:
        raise IngestPathError("no ingest root is configured, so no file may be read")
    resolved: list[Path] = []
    for pattern in patterns:
        matches = _expand_pattern(pattern, base, allowed)
        if not matches:
            raise IngestPathError(f"no readable file matches {pattern!r} below {_render_roots(allowed)}")
        for match in matches:
            _assert_inside_roots(match, allowed, pattern)
            if match not in resolved:
                resolved.append(match)
    return resolved


def relative_to_working_directory(path: Path, *, working_directory: Path | None = None) -> str:
    """Render a resolved path relative to the working directory when it lies below it."""
    base = (working_directory if working_directory is not None else Path.cwd()).resolve()
    if path.is_relative_to(base):
        return str(path.relative_to(base))
    return str(path)


def _expand_pattern(pattern: str, base: Path, roots: Sequence[Path]) -> list[Path]:
    """Expand one pattern into the sorted set of existing files it names, without traversing outside the roots."""
    if not pattern or pattern.strip() != pattern:
        raise IngestPathError(f"ingest path {pattern!r} is blank or padded with whitespace")
    candidate = Path(pattern)
    anchor = Path(candidate.anchor) if candidate.is_absolute() else base
    relative = candidate.relative_to(candidate.anchor) if candidate.is_absolute() else candidate
    if not GLOB_CHARACTERS.search(pattern):
        # A literal path is resolved rather than globbed, so a missing file is reported as missing
        # instead of as a pattern that matched nothing.
        resolved = (anchor / relative).resolve()
        _assert_inside_roots(resolved, roots, pattern)
        return [resolved] if _is_ingestible(resolved) else []
    search_root, expansion = _split_at_first_wildcard(anchor, relative)
    # The walk is refused where it would start rather than once it is done: expanding first and
    # checking the matches afterwards lets `/**/*` read every directory of the machine before the
    # first refusal, which is the whole cost the ingest roots exist to avoid.
    _assert_inside_roots(search_root, roots, pattern)
    return sorted(match.resolve() for match in search_root.glob(expansion) if _is_ingestible(match))


def _split_at_first_wildcard(anchor: Path, relative: Path) -> tuple[Path, str]:
    """Split a glob into the resolved literal directory it starts from and the part left to expand."""
    parts = relative.parts
    literal_count = 0
    while literal_count < len(parts) and not GLOB_CHARACTERS.search(parts[literal_count]):
        literal_count += 1
    # The caller only reaches this for a pattern that carries a wildcard, so the expansion is never empty.
    return anchor.joinpath(*parts[:literal_count]).resolve(), str(Path(*parts[literal_count:]))


def _is_ingestible(path: Path) -> bool:
    """Return whether a path names a readable file or one of the directory stores a reader opens whole."""
    if path.is_file():
        return True
    return path.is_dir() and path.suffix.lower() in DIRECTORY_STORE_SUFFIXES


def _assert_inside_roots(path: Path, roots: Sequence[Path], pattern: str) -> None:
    """Refuse a resolved path that lies outside every configured ingest root."""
    if any(path.is_relative_to(root) for root in roots):
        return
    # The path is already resolved, so a `..` segment or a symbolic link out of a root is caught here
    # rather than by looking for `..` in the text the caller sent. Every match is checked as well as
    # the directory the expansion started from, because a link inside a root still points wherever it
    # points: `Path.glob` refuses to recurse through one, but it follows one the pattern names.
    raise IngestPathError(f"ingest path {pattern!r} resolves outside {_render_roots(roots)}")


def _render_roots(roots: Sequence[Path]) -> str:
    """Render the configured ingest roots for an error message."""
    return "the ingest roots " + ", ".join(repr(str(root)) for root in roots)
