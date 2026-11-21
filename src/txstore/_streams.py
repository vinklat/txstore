"""Backend body streams that raise backend read errors as storage errors.

Every backend's `get_stream` returns a `GuardedStream` → [STORAGE.md](STORAGE.md#storage-abi); the
size cap that `get`, `get_digests`, and `transfer` add on top stays in `txstore/base.py`.
"""

from __future__ import annotations

import io
from typing import IO, TYPE_CHECKING, Any

from txstore.base import StorageException

if TYPE_CHECKING:
    from txstore.metrics import StorageMetrics


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
