"""Capability mixins for storage backends.

`ReadOnlyStorageMixin` and `WriteOnlyStorageMixin` supply default
``NotImplementedError`` stubs for the `Storage` ABC methods that a
read-only or write-only backend cannot fulfil. Concrete backends
inherit the mixin first (MRO) and then override only the methods they
actually implement.
"""

from __future__ import annotations

import builtins
from typing import IO, Any, NoReturn

READ_ONLY_MSG = "This operation is not supported by read-only storage."
WRITE_ONLY_MSG = "This operation is not supported by write-only storage."
NO_TRANSACTIONS_MSG = "Transactions are not supported by this storage backend."


class TransactionNotSupportedMixin:
    """Explicit error stubs for transaction methods on non-transactional backends.

    Backends without transaction support (e.g. `StorageHTTP`, `StorageNull`)
    inherit this to raise a clear `NotImplementedError` instead of a
    confusing `AttributeError`, for the lifecycle and the reclaim methods alike.
    """

    def in_transaction(self) -> bool:
        """Always False for non-transactional backends."""
        return False

    def transaction_begin(self, transaction_id: int = 0) -> NoReturn:
        """Not supported (non-transactional backend)."""
        del transaction_id
        raise NotImplementedError(NO_TRANSACTIONS_MSG)

    def transaction_commit(self) -> NoReturn:
        """Not supported (non-transactional backend)."""
        raise NotImplementedError(NO_TRANSACTIONS_MSG)

    def transaction_revert(self, transaction: Any) -> NoReturn:
        """Not supported (non-transactional backend)."""
        del transaction
        raise NotImplementedError(NO_TRANSACTIONS_MSG)

    def transaction_discard(self) -> NoReturn:
        """Not supported (non-transactional backend)."""
        raise NotImplementedError(NO_TRANSACTIONS_MSG)

    def hidden_objects(self) -> NoReturn:
        """Not supported (non-transactional backend)."""
        raise NotImplementedError(NO_TRANSACTIONS_MSG)

    def find_transaction_footmarks(self, models: Any, *, listing: Any = None) -> NoReturn:
        """Not supported (non-transactional backend)."""
        del models, listing
        raise NotImplementedError(NO_TRANSACTIONS_MSG)

    def transaction_forget(self, models: Any, *, paths: Any = None) -> NoReturn:
        """Not supported (non-transactional backend)."""
        del models, paths
        raise NotImplementedError(NO_TRANSACTIONS_MSG)

    def find_orphaned_footmarks(self, known: Any, *, listing: Any = None) -> NoReturn:
        """Not supported (non-transactional backend)."""
        del known, listing
        raise NotImplementedError(NO_TRANSACTIONS_MSG)

    def transaction_forget_orphans(self, known: Any, *, orphans: Any = None) -> NoReturn:
        """Not supported (non-transactional backend)."""
        del known, orphans
        raise NotImplementedError(NO_TRANSACTIONS_MSG)


class ReadOnlyStorageMixin:
    """Default `NotImplementedError` stubs for write/delete/metadata/copy/move.

    Backends inheriting this mixin (e.g. `StorageHTTP`) still define the
    read-side methods (`list`, `get`, `get_stream`, `get_metadata`,
    `exists`); a backend without a listing API makes `list` raise
    `NotImplementedError` → [STORAGE.md](STORAGE.md#backends).
    """

    def put(
        self,
        path: str,
        data: bytes,
        metadata: dict[str, str] | None = None,
        acl: str | None = None,
    ) -> None:
        """Not supported (read-only storage)."""
        raise NotImplementedError(READ_ONLY_MSG)

    def put_stream(
        self,
        path: str,
        stream: IO[bytes],
        metadata: dict[str, str] | None = None,
        acl: str | None = None,
    ) -> None:
        """Not supported (read-only storage)."""
        raise NotImplementedError(READ_ONLY_MSG)

    def add_metadata(
        self,
        path: str,
        metadata: dict[str, str],
        acl: str | None = None,
    ) -> None:
        """Not supported (read-only storage)."""
        raise NotImplementedError(READ_ONLY_MSG)

    def delete(self, path: str) -> None:
        """Not supported (read-only storage)."""
        raise NotImplementedError(READ_ONLY_MSG)

    def delete_metadata(self, path: str) -> None:
        """Not supported (read-only storage)."""
        raise NotImplementedError(READ_ONLY_MSG)

    def delete_metadata_fields(self, path: str, keys: builtins.list[str]) -> None:
        """Not supported (read-only storage)."""
        raise NotImplementedError(READ_ONLY_MSG)

    def copy(self, path_src: str, path_dst: str) -> None:
        """Not supported (read-only storage)."""
        raise NotImplementedError(READ_ONLY_MSG)

    def move(self, path_src: str, path_dst: str) -> None:
        """Not supported (read-only storage)."""
        raise NotImplementedError(READ_ONLY_MSG)

    def format(self) -> None:
        """Not supported (read-only storage)."""
        raise NotImplementedError(READ_ONLY_MSG)

    def mkdir(self, path: str) -> None:
        """Not supported (read-only storage)."""
        raise NotImplementedError(READ_ONLY_MSG)

    def rmdir(self, path: str) -> None:
        """Not supported (read-only storage)."""
        raise NotImplementedError(READ_ONLY_MSG)


class WriteOnlyStorageMixin:
    """Default `NotImplementedError` stubs for list/get/exists/copy/move/mkdir.

    Backends inheriting this mixin (e.g. `StorageNull`) only need to
    implement the write-side methods (`put`, `put_stream`, `delete`,
    `add_metadata`, `delete_metadata`, `delete_metadata_fields`,
    `format`, `rmdir`).
    """

    def list(self) -> builtins.list[str]:
        """Not supported (write-only storage)."""
        raise NotImplementedError(WRITE_ONLY_MSG)

    def get(self, path: str) -> bytes:
        """Not supported (write-only storage)."""
        raise NotImplementedError(WRITE_ONLY_MSG)

    def get_metadata(self, path: str) -> dict[str, Any]:
        """Not supported (write-only storage)."""
        raise NotImplementedError(WRITE_ONLY_MSG)

    def exists(self, path: str) -> bool:
        """Not supported (write-only storage)."""
        raise NotImplementedError(WRITE_ONLY_MSG)

    def get_stream(self, path: str) -> IO[bytes]:
        """Not supported (write-only storage)."""
        raise NotImplementedError(WRITE_ONLY_MSG)

    def copy(self, path_src: str, path_dst: str) -> None:
        """Not supported (write-only storage)."""
        raise NotImplementedError(WRITE_ONLY_MSG)

    def move(self, path_src: str, path_dst: str) -> None:
        """Not supported (write-only storage)."""
        raise NotImplementedError(WRITE_ONLY_MSG)

    def mkdir(self, path: str) -> None:
        """Not supported (write-only storage)."""
        raise NotImplementedError(WRITE_ONLY_MSG)
