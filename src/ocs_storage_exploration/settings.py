"""Service settings, including the object storage block the S3 backend is configured from."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from ocs_storage_exploration.storage.addresses import StorageScheme


class ObjectStorageSettings(BaseModel):
    """S3 compatible object storage configuration; secrets stay wrapped in SecretStr."""

    bucket: str
    prefix: str = "ocs"
    region: str | None = None
    endpoint_url: str | None = None
    allow_http: bool = False
    access_key_id: str | None = None
    secret_access_key: SecretStr | None = None
    session_token: SecretStr | None = None
    force_path_style: bool = True
    anonymous: bool = False


class Settings(BaseSettings):
    """Settings of the storage exploration service, read from OCS_STORAGE_ environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="OCS_STORAGE_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    backend: StorageScheme = StorageScheme.FILE
    data_directory: Path = Path("data")
    base_prefix: str = "ocs"
    s3: ObjectStorageSettings | None = None
    max_unqualified_feature_count: int = Field(default=50_000, ge=1)
    max_query_cell_count: int = Field(default=50_000_000, ge=1)
    max_cube_cells: int = Field(default=50_000_000, ge=1)
    parquet_row_group_size: int = Field(default=65_536, ge=1)
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "info"
    reload: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process wide settings, built once from the environment."""
    return Settings()
