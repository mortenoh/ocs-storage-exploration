"""Resolves local ingest paths and globs, refusing anything outside the configured ingest roots."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

from ocs_storage_exploration.storage.errors import IngestPathError

# A pattern carrying one of these is expanded as a glob; anything else is taken as a literal path.
GLOB_CHARACTERS: Final[re.Pattern[str]] = re.compile(r"[*?\[]")


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
        matches = _expand_pattern(pattern, base)
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


def _expand_pattern(pattern: str, base: Path) -> list[Path]:
    """Expand one pattern into the sorted set of existing files it names."""
    if not pattern or pattern.strip() != pattern:
        raise IngestPathError(f"ingest path {pattern!r} is blank or padded with whitespace")
    candidate = Path(pattern)
    if not GLOB_CHARACTERS.search(pattern):
        # A literal path is resolved rather than globbed, so a missing file is reported as missing
        # instead of as a pattern that matched nothing.
        resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
        return [resolved] if resolved.is_file() else []
    if candidate.is_absolute():
        root = Path(candidate.anchor)
        relative = candidate.relative_to(root)
    else:
        root = base
        relative = candidate
    return sorted(match.resolve() for match in root.glob(str(relative)) if match.is_file())


def _assert_inside_roots(path: Path, roots: Sequence[Path], pattern: str) -> None:
    """Refuse a resolved path that lies outside every configured ingest root."""
    if any(path.is_relative_to(root) for root in roots):
        return
    # The path is already resolved, so a `..` segment or a symbolic link out of a root is caught here
    # rather than by looking for `..` in the text the caller sent.
    raise IngestPathError(f"ingest path {pattern!r} resolves outside {_render_roots(roots)}")


def _render_roots(roots: Sequence[Path]) -> str:
    """Render the configured ingest roots for an error message."""
    return "the ingest roots " + ", ".join(repr(str(root)) for root in roots)
