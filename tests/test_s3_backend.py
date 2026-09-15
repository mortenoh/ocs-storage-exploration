"""Tests reserved for a live S3 endpoint; the first pass only asserts the honest refusal."""

from __future__ import annotations

import pytest

from ocs_storage_exploration.settings import ObjectStorageSettings, Settings
from ocs_storage_exploration.storage.addresses import StorageScheme
from ocs_storage_exploration.storage.errors import BackendNotSupportedError
from ocs_storage_exploration.storage.registry import build_backend

pytestmark = pytest.mark.s3


def test_the_s3_backend_refuses_with_a_not_implemented_status() -> None:
    settings = Settings(
        backend=StorageScheme.S3,
        s3=ObjectStorageSettings(
            bucket="ocs-exploration",
            endpoint_url="http://localhost:9000",
            allow_http=True,
            access_key_id="rustfsadmin",
        ),
    )
    backend = build_backend(settings)

    assert backend.describe().available is False
    with pytest.raises(BackendNotSupportedError) as raised:
        backend.object_store()
    assert raised.value.status_code == 501
