"""Command line entry point running the service with uvicorn."""

from __future__ import annotations

import uvicorn

from ocs_storage_exploration.settings import get_settings


def main() -> None:
    """Run the storage exploration service."""
    settings = get_settings()
    uvicorn.run(
        "ocs_storage_exploration.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=settings.reload,
    )


if __name__ == "__main__":
    main()
