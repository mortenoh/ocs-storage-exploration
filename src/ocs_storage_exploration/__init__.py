"""Prototype of a unified raster and vector storage abstraction for the Open Climate Service."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ocs-storage-exploration")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

__all__ = ["__version__"]
