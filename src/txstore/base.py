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


class StorageNotSupportedException(StorageException, NotImplementedError):
    """Raised when a backend lacks the operation; also a `NotImplementedError`.

    → [EXCEPTIONS.md](EXCEPTIONS.md#storage-exceptions).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class StorageTooLargeException(StorageException):
    """Raised when an object is larger than the `max_size` a read is capped at.

    → [STORAGE.md](STORAGE.md#storage-abi).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


def _check_max_size(max_size: int | None) -> None:
    """Refuse a `max_size` other than None or an integer of 0 or more.

    → [VALIDATION.md](VALIDATION.md#read-size-cap).

    Raises:
        ValueError: If `max_size` is a bool, not an integer, or negative.
    """
    if max_size is None:
        return
    if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size < 0:
        raise ValueError(f"max_size must be None or an integer of 0 or more, not {max_size!r}")


class _CappedReader:
    """Reader that refuses a body larger than `max_size` without reading on.

    It pulls at most `max_size + 1` bytes from `stream`: the extra byte proves the body too large.
    """

    def __init__(
        self, stream: IO[bytes], max_size: int | None, path: str, metrics: StorageMetrics
    ) -> None:
        self._stream = stream
        self._max_size = max_size
        self._path = path
        self._metrics = metrics
        self._size = 0
        # The refusal, raised again to a destination that reads on after swallowing it.
        self._error: StorageTooLargeException | None = None

    def read(self, size: int = -1) -> bytes:
        """Read up to `size` bytes, or to the end when `size` is negative, never past the cap.

        Without a cap the read passes straight through, so `fs` sizes one read by the file. With
        one, a read to the end goes in `CHUNK_SIZE` pieces: a single read of `max_size + 1` bytes
        would allocate them up front however small the body is.

        Raises:
            StorageTooLargeException: Once the body is found larger than `max_size`.
        """
        if self._error is not None:
            raise self._error
        if self._max_size is None:
            return self._stream.read(size)
        if size < 0:
            buffer = io.BytesIO()
            while chunk := self.read(CHUNK_SIZE):
                buffer.write(chunk)
            return buffer.getvalue()
        remaining = self._max_size + 1 - self._size
        data = self._stream.read(remaining if size < 0 else min(size, remaining))
        self._size += len(data)
        if self._size > self._max_size:
            self._metrics.read_errors_inc()
            self._error = StorageTooLargeException(
                f"{self._path} is larger than {self._max_size} bytes"
            )
            raise self._error
        return data


class GuardedStream(io.RawIOBase):
    """Read-only view of a backend body stream that raises backend read errors as storage errors.

    Every backend's `get_stream` returns one → [STORAGE.md](STORAGE.md#storage-abi): an error of
    `errors` raised while the body is read → `StorageException` chained from it, with one read
    error counted. `seek`, `tell`, and `fileno` reach the wrapped stream only when it seeks; any
    other stream raises `io.UnsupportedOperation`, as `io` streams do.
    """

    def __init__(
        self,
        inner: IO[bytes],
        *,
        errors: tuple[type[BaseException], ...],
        path: str,
        metrics: StorageMetrics,
    ) -> None:
        """Wrap `inner`, the body of the object at `path`.

        Args:
            inner: The backend's own body stream; closed with this one.
            errors: Backend error classes a read raises as `StorageException`.
            path: Object path named in the message.
            metrics: Metrics of the storage that opened the stream.
        """
        super().__init__()
        self._inner = inner
        self._errors = errors
        self._path = path
        self._metrics = metrics

    @property
    def name(self) -> Any:
        """Name of the wrapped stream (an `fs` file's path).

        Raises:
            AttributeError: If the wrapped stream has none, so `hasattr` reports it absent.
        """
        return self._inner.name

    def _fail(self, exc: BaseException) -> StorageException:
        """Count a read error and return the storage error to raise from `exc`."""
        self._metrics.read_errors_inc()
        return StorageException(f"Error reading {self._path}: {exc}")

    def readable(self) -> bool:
        """Always True: the body is readable."""
        return True

    def read(self, size: int | None = -1) -> bytes:
        """Read up to `size` bytes, or to the end when `size` is negative or None.

        Raises:
            StorageException: If the backend fails while the body is read.
        """
        try:
            if size is None or size < 0:
                return self._inner.read()
            return self._inner.read(size)
        except self._errors as exc:
            raise self._fail(exc) from exc

    def readline(self, size: int | None = -1) -> bytes:
        """Read one line, through the wrapped stream's own `readline` when it has one.

        A buffered `fs` file then reads a line in one call; `io`'s default reads a byte at a time.

        Raises:
            StorageException: If the backend fails while the line is read.
        """
        readline = getattr(self._inner, "readline", None)
        if readline is None:
            return super().readline(size)
        try:
            line: bytes = readline(-1 if size is None else size)
        except self._errors as exc:
            raise self._fail(exc) from exc
        return line

    def readinto(self, buffer: Any) -> int:
        """Read into a writable `buffer`, returning the byte count."""
        view = memoryview(buffer).cast("B")
        data = self.read(len(view))
        view[: len(data)] = data
        return len(data)

    def seekable(self) -> bool:
        """True iff the wrapped stream seeks (an `fs` file)."""
        seekable = getattr(self._inner, "seekable", None)
        return bool(seekable is not None and seekable())

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Seek the wrapped stream.

        Raises:
            io.UnsupportedOperation: If the wrapped stream does not seek.
            StorageException: If the backend fails the seek.
        """
        if not self.seekable():
            raise io.UnsupportedOperation("seek")
        try:
            return self._inner.seek(offset, whence)
        except self._errors as exc:
            raise self._fail(exc) from exc

    def tell(self) -> int:
        """Position in the wrapped stream.

        Raises:
            io.UnsupportedOperation: If the wrapped stream does not seek.
            StorageException: If the backend fails the call.
        """
        if not self.seekable():
            raise io.UnsupportedOperation("tell")
        try:
            return self._inner.tell()
        except self._errors as exc:
            raise self._fail(exc) from exc

    def fileno(self) -> int:
        """Descriptor of the wrapped stream.

        Raises:
            io.UnsupportedOperation: If the wrapped stream does not seek.
        """
        if not self.seekable():
            raise io.UnsupportedOperation("fileno")
        return self._inner.fileno()

    def close(self) -> None:
        """Close the wrapped stream, then this one."""
        if not self.closed:
            try:
                self._inner.close()
            finally:
                super().close()


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

    def get(self, path: str, *, max_size: int | None = None) -> bytes:
        """Return the full content of the object at `path`, read through `get_stream`.

        Args:
            path: Object path.
            max_size: Largest size accepted, in bytes; None reads any size
                → [STORAGE.md](STORAGE.md#storage-abi).

        Raises:
            ValueError: If `max_size` is invalid → [VALIDATION.md](VALIDATION.md#read-size-cap).
            StorageNotFoundException: If the object is missing.
            StorageTooLargeException: If the object is larger than `max_size`; at most
                `max_size + 1` bytes are read.
            StorageException: If the object cannot be opened or read.
        """
        _check_max_size(max_size)
        with self.get_stream(path) as stream:
            data = _CappedReader(stream, max_size, path, self.metrics).read()
        self.metrics.read_bytes_inc(len(data))
        return data

    def get_digests(self, path: str, *, max_size: int | None = None) -> dict[str, str]:
        """Read the object at `path` only to digest it; no sink storage is needed.

        Args:
            path: Object path.
            max_size: Largest size accepted, in bytes; None reads any size
                → [STORAGE.md](STORAGE.md#digests).

        Raises:
            ValueError: If `max_size` is invalid → [VALIDATION.md](VALIDATION.md#read-size-cap).
            StorageNotFoundException: If the object is missing.
            StorageTooLargeException: If the object is larger than `max_size`.
            StorageException: If the object cannot be opened or read.

        Returns:
            Digests of the object, as `transfer` returns them.
        """
        _check_max_size(max_size)
        with self.get_stream(path) as stream:
            capped = _CappedReader(stream, max_size, path, self.metrics)
            fchecksums = ChecksumOpen(cast(IO[bytes], capped))
            while fchecksums.read(CHUNK_SIZE):
                pass
        self.metrics.read_bytes_inc(fchecksums.size)
        return fchecksums.get_digests()

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
        *,
        max_size: int | None = None,
    ) -> dict[str, str]:
        """Stream an object from this storage to another, computing digests.

        Args:
            path_src: Source path in this storage.
            storage_dst: Destination storage instance.
            path_dst: Destination path. Defaults to `path_src`.
            acl: Optional ACL for the destination object.
            max_size: Largest size accepted, in bytes; None transfers any size
                → [STORAGE.md](STORAGE.md#storage-abi).

        Raises:
            ValueError: If `max_size` is invalid → [VALIDATION.md](VALIDATION.md#read-size-cap).
            StorageTooLargeException: If the source is larger than `max_size`; the destination
                keeps no object.

        Returns:
            Digests of the transferred object (`MD5Sum`, `SHA1`, `SHA256`,
            `SHA512`, `Size`).
        """
        _check_max_size(max_size)
        if path_dst is None:
            path_dst = path_src

        with self.get_stream(path_src) as fsrc:
            capped = _CappedReader(fsrc, max_size, path_src, self.metrics)
            fchecksums = ChecksumOpen(cast(IO[bytes], capped))
            # Every `put_stream` only calls `read`, which `ChecksumOpen` provides, and lets the
            # refusal propagate unchanged → [STORAGE.md](STORAGE.md#storage-abi).
            storage_dst.put_stream(path_dst, cast(IO[bytes], fchecksums), acl=acl)

        # The destination counts its own write in `put_stream`.
        self.metrics.read_bytes_inc(fchecksums.size)

        return fchecksums.get_digests()
