"""Storage abstract base class and shared types.

This module is the canonical home of the `Storage` ABC and the small
value-types every backend reuses (`StorageException`, `StorageType`,
`CHUNK_SIZE`, `REQUEST_TIMEOUT`).

Transaction-related types (`TransactionModel`, `FootmarkMixin`) live in
`transaction.py`.

The public surface is re-exported from `txstore.__init__`
so callers should keep importing from there.
"""

from __future__ import annotations

import builtins
import io
import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import IO, Any, cast

from txstore.hexdigest import ChecksumOpen
from txstore.metrics import StorageMetrics

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

CHUNK_SIZE = 1024 * 1024
REQUEST_TIMEOUT = 30


class StorageType(Enum):
    """Enum for storage types."""

    null = 0
    fs = 1
    s3 = 2
    http = 3

    def __str__(self) -> str:  # noqa: D105
        return self.name


class StorageException(Exception):
    """Custom exception for storage-related errors."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class StorageNotFoundException(StorageException):
    """Raised when ``get`` or ``get_stream`` targets a path that does not exist."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class Storage(ABC):
    """Abstract base class for storage backends.

    Defines the read/write/metadata/directory contract that every backend
    must implement. Transaction-related behaviour is opt-in via the
    `FootmarkMixin` from `transaction.py`; backends that do not support
    transactions simply do not inherit it.

    Attributes:
        metrics (StorageMetrics): Counters for read/write/delete bytes & objects.
    """

    def __init__(self) -> None:
        """Initialize per-instance metrics."""
        self.metrics = StorageMetrics()

    @abstractmethod
    def get_type(self) -> StorageType:
        """Return the storage backend identifier."""

    @abstractmethod
    def list(self) -> builtins.list[str]:
        """List all (regular, non-hidden) objects in the storage."""

    @abstractmethod
    def get(self, path: str) -> bytes:
        """Return the full content of the object at `path`."""

    @abstractmethod
    def get_metadata(self, path: str) -> dict[str, Any]:
        """Return the metadata of the object at `path`."""

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Return True iff the object at `path` exists."""

    @abstractmethod
    def get_stream(self, path: str) -> IO[bytes]:
        """Return an open binary stream for the object at `path`."""

    def put(
        self,
        path: str,
        data: bytes,
        metadata: dict[str, str] | None = None,
        acl: str | None = None,
    ) -> None:
        """Write `data` to `path`; a `put_stream` of the bytes."""
        self.put_stream(path, io.BytesIO(data), metadata=metadata, acl=acl)

    @abstractmethod
    def put_stream(
        self,
        path: str,
        stream: IO[bytes],
        metadata: dict[str, str] | None = None,
        acl: str | None = None,
    ) -> None:
        """Write the contents of `stream` to `path`."""

    @abstractmethod
    def add_metadata(
        self,
        path: str,
        metadata: dict[str, str],
        acl: str | None = None,
    ) -> None:
        """Set or merge metadata onto the object at `path`.

        Args:
            path: Object path (relative).
            metadata: Key-value pairs to associate with the object.
            acl: Optional ACL string. Backends that do not support ACLs
                must accept and ignore this argument.
        """

    @abstractmethod
    def delete(self, path: str) -> None:
        """Delete the object at `path`."""

    @abstractmethod
    def delete_metadata(self, path: str) -> None:
        """Remove all metadata from the object at `path`."""

    @abstractmethod
    def delete_metadata_fields(self, path: str, keys: builtins.list[str]) -> None:
        """Remove the named metadata `keys` from the object at `path`."""

    @abstractmethod
    def copy(self, path_src: str, path_dst: str) -> None:
        """Copy an object inside the storage.

        A backend with directories creates a missing parent of `path_dst` as it does for `put`
        → [STORAGE.md](STORAGE.md#storage-abi).

        Args:
            path_src: Source path.
            path_dst: Destination path.
        """

    @abstractmethod
    def move(self, path_src: str, path_dst: str) -> None:
        """Move an object inside the storage; a missing parent is handled as by `copy`.

        Paths that name one object make it a no-op → [STORAGE.md](STORAGE.md#storage-abi).
        """

    @abstractmethod
    def format(self) -> None:
        """Reset the storage (destructive)."""

    @abstractmethod
    def mkdir(self, path: str) -> None:
        """Create a directory at `path`."""

    @abstractmethod
    def rmdir(self, path: str) -> None:
        """Remove the directory at `path`."""

    def transfer(
        self,
        path_src: str,
        storage_dst: Storage,
        path_dst: str | None = None,
        acl: str | None = None,
    ) -> dict[str, str]:
        """Stream an object from this storage to another, computing digests.

        Args:
            path_src: Source path in this storage.
            storage_dst: Destination storage instance.
            path_dst: Destination path. Defaults to `path_src`.
            acl: Optional ACL for the destination object.

        Returns:
            Digests of the transferred object (`MD5Sum`, `SHA1`, `SHA256`,
            `SHA512`, `Size`).
        """
        if path_dst is None:
            path_dst = path_src

        with self.get_stream(path_src) as fsrc:
            fchecksums = ChecksumOpen(fsrc)
            # Every `put_stream` only calls `read`, which `ChecksumOpen` provides.
            storage_dst.put_stream(path_dst, cast(IO[bytes], fchecksums), acl=acl)

        # The destination counts its own write in `put_stream`.
        self.metrics.read_bytes_inc(fchecksums.size)

        return fchecksums.get_digests()
