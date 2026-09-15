FROM ghcr.io/astral-sh/uv:0.12-python3.13-trixie-slim

# Compile Python bytecode for faster startup (.venv only)
ENV UV_COMPILE_BYTECODE=1
# Use copy mode to avoid hardlink issues across the uv cache mount
ENV UV_LINK_MODE=copy
# Prevent Python from writing .pyc files at runtime
ENV PYTHONDONTWRITEBYTECODE=1

# curl is needed by the healthcheck below. Every dependency ships a manylinux wheel, but rasterio's
# links against the system expat rather than bundling it, and this base image does not carry one.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends curl libexpat1

RUN groupadd --gid 999 ocs && \
    useradd --create-home --shell /usr/sbin/nologin --uid 999 --gid 999 ocs

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY README.md LICENSE ./
COPY src/ src/

# No extras to name: rioxarray became a runtime dependency when the ingest endpoints started reading
# GeoTIFF and COG files, so the image that serves them has to carry it.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

RUN mkdir -p /app/data && chown -R ocs:ocs /app/data

ENV PATH="/app/.venv/bin:$PATH"
ENV OCS_STORAGE_HOST=0.0.0.0
ENV OCS_STORAGE_PORT=8000
ENV OCS_STORAGE_DATA_DIRECTORY=/app/data

USER ocs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${OCS_STORAGE_PORT}/health || exit 1

# exec form (JSON) so the server is PID 1 and receives signals directly, with no shell
# process between init and uvicorn. The entry point reads the host and the port from the
# OCS_STORAGE_ environment variables set above.
CMD ["ocs-storage-exploration"]
