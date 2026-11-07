"""Storage-root paths reserved for the transaction machinery (storage-local)."""

from __future__ import annotations

import os

from txstore.base import StorageException

HISTORY_FILENAME = ".transactions.json"
LOCK_FILENAME = ".transactions.lock"

# Caller objects share the storage keyspace with these two, and a key can
# arrive straight from untrusted input.
# Overwriting the lock breaks mutual exclusion (the replaced body is corrupt,
# so a waiter treats it as breakable while the holder still runs); overwriting
# or deleting the history rewrites the audit log the revert path reads.
RESERVED_PATHS = frozenset({HISTORY_FILENAME, LOCK_FILENAME})


def is_reserved_path(path: str) -> bool:
    """Return True iff *path* names a reserved storage-root object.

    Args:
        path: Relative storage path, normalized and case-folded before
            comparison so ``./.transactions.lock``, ``pool/../.transactions.lock``
            and ``.Transactions.Lock`` (one file on a case-insensitive
            filesystem) are recognised too. Leading ``/`` are dropped first:
            ``normpath`` keeps a leading ``//``, and an S3 gateway that collapses
            slashes maps ``//.transactions.lock`` onto the lock.

    Returns:
        True if the path resolves to a reserved object.
    """
    return os.path.normpath(path.lstrip("/")).casefold() in RESERVED_PATHS


def check_reserved_path(path: str) -> None:
    """Reject *path* when it names a reserved storage-root object.

    Args:
        path: Relative storage path to check.

    Raises:
        StorageException: If *path* resolves to a reserved object.
    """
    if is_reserved_path(path):
        raise StorageException(f"Refusing to write reserved storage path: {path}")
