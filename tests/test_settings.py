"""Tests for the service settings and their environment variable mapping."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from ocs_storage_exploration.settings import ObjectStorageSettings, Settings, get_settings
from ocs_storage_exploration.storage.addresses import StorageScheme


def test_defaults_use_the_filesystem_backend() -> None:
    settings = Settings()

    assert settings.backend is StorageScheme.FILE
    assert settings.data_directory == Path("data")
    assert settings.base_prefix == "ocs"
    assert settings.s3 is None
    assert settings.max_unqualified_feature_count == 50_000
    assert settings.max_query_cell_count == 50_000_000
    assert settings.max_cube_cells == 50_000_000
    assert settings.parquet_row_group_size == 65_536
    assert settings.max_concurrent_storage_operations == 16
    assert settings.storage_operation_timeout_seconds == 180.0


def test_environment_variables_use_the_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCS_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("OCS_STORAGE_BASE_PREFIX", "exploration")

    settings = Settings()

    assert settings.backend is StorageScheme.MEMORY
    assert settings.base_prefix == "exploration"


def test_nested_object_storage_settings_use_the_double_underscore_delimiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCS_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("OCS_STORAGE_S3__BUCKET", "ocs-exploration")
    monkeypatch.setenv("OCS_STORAGE_S3__ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("OCS_STORAGE_S3__ACCESS_KEY_ID", "rustfsadmin")
    monkeypatch.setenv("OCS_STORAGE_S3__SECRET_ACCESS_KEY", "supersecret")

    settings = Settings()

    assert settings.backend is StorageScheme.S3
    assert settings.s3 is not None
    assert settings.s3.bucket == "ocs-exploration"
    assert settings.s3.endpoint_url == "http://localhost:9000"
    assert settings.s3.access_key_id == "rustfsadmin"
    assert settings.s3.secret_access_key is not None
    assert settings.s3.secret_access_key.get_secret_value() == "supersecret"
    assert settings.s3.connect_timeout_seconds == 5.0
    assert settings.s3.request_timeout_seconds == 30.0
    assert settings.s3.max_retries == 3
    assert settings.s3.retry_backoff_seconds == 0.5


def test_the_client_bounds_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCS_STORAGE_MAX_CONCURRENT_STORAGE_OPERATIONS", "4")
    monkeypatch.setenv("OCS_STORAGE_STORAGE_OPERATION_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("OCS_STORAGE_S3__BUCKET", "ocs-exploration")
    monkeypatch.setenv("OCS_STORAGE_S3__CONNECT_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("OCS_STORAGE_S3__REQUEST_TIMEOUT_SECONDS", "9")
    monkeypatch.setenv("OCS_STORAGE_S3__MAX_RETRIES", "1")

    settings = Settings()

    assert settings.max_concurrent_storage_operations == 4
    assert settings.storage_operation_timeout_seconds == 12.5
    assert settings.s3 is not None
    assert settings.s3.connect_timeout_seconds == 1.5
    assert settings.s3.request_timeout_seconds == 9.0
    assert settings.s3.max_retries == 1


def test_secrets_are_hidden_when_the_settings_are_rendered() -> None:
    options = ObjectStorageSettings(
        bucket="ocs-exploration",
        access_key_id="rustfsadmin",
        secret_access_key=SecretStr("supersecret"),
        session_token=SecretStr("supertoken"),
    )

    rendered = repr(options) + options.model_dump_json()

    assert "supersecret" not in rendered
    assert "supertoken" not in rendered
    assert "rustfsadmin" in rendered


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()

    assert get_settings() is get_settings()

    get_settings.cache_clear()
