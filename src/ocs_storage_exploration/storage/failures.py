"""Translation of the transport failures of obstore, Icechunk and pyarrow into one storage error."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import icechunk
from obstore.exceptions import GenericError

from ocs_storage_exploration.storage.errors import BackendUnavailableError


@contextmanager
def backend_transport_failures(operation: str) -> Generator[None]:
    """Report a failure to reach the object store as BackendUnavailableError, keeping the original message.

    The three libraries spell the same outage three ways: obstore raises its fallback ``GenericError``
    once the retry budget is spent, Icechunk raises ``icechunk.StorageError``, and pyarrow raises a
    builtin ``OSError``. A missing object is not an outage, so ``FileNotFoundError`` passes through
    for the callers that read it as "absent".
    """
    try:
        yield
    except GenericError as error:
        raise BackendUnavailableError(f"{operation} could not reach the object store: {error}") from error
    except icechunk.StorageError as error:
        raise BackendUnavailableError(f"{operation} could not reach the object store: {error}") from error
    except FileNotFoundError:
        raise
    except OSError as error:
        raise BackendUnavailableError(f"{operation} could not reach the object store: {error}") from error
