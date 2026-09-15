"""Tests asserting the HTTP status code of every storage error."""

from __future__ import annotations

import pytest

from ocs_storage_exploration.storage import errors

EXPECTED_STATUS_CODES = {
    "StorageError": 500,
    "StorageAddressError": 400,
    "BackendNotSupportedError": 501,
    "BackendUnavailableError": 503,
    "CrsError": 400,
    "DatasetNotFoundError": 404,
    "DatasetAlreadyExistsError": 409,
    "ItemTypeMismatchError": 409,
    "NothingToPublishError": 409,
    "PublicationConflictError": 409,
    "PublicationSelectorError": 422,
    "SnapshotNotFoundError": 404,
    "RasterContractError": 422,
    "FeatureIdentityError": 422,
    "VectorInputError": 422,
    "FeatureCountGuardError": 413,
    "QuerySizeGuardError": 413,
    "SelectableColumnError": 400,
    "StorageTimeoutError": 504,
}


def all_error_classes() -> list[type[errors.StorageError]]:
    discovered: list[type[errors.StorageError]] = [errors.StorageError]
    discovered.extend(sorted(errors.StorageError.__subclasses__(), key=lambda error: error.__name__))
    return discovered


def test_every_error_class_is_covered_by_the_sweep() -> None:
    assert {error.__name__ for error in all_error_classes()} == set(EXPECTED_STATUS_CODES)


@pytest.mark.parametrize("error_class", all_error_classes(), ids=lambda error: error.__name__)
def test_status_codes_are_declared(error_class: type[errors.StorageError]) -> None:
    error = error_class("something went wrong")

    assert error.status_code == EXPECTED_STATUS_CODES[error_class.__name__]
    assert error.message == "something went wrong"
    assert str(error) == "something went wrong"
    assert isinstance(error, errors.StorageError)
