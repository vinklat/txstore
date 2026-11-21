"""Tests for txstore/_streams.py: `GuardedStream`, the body every backend's `get_stream` returns.

Backend wiring lives in `test_storage_<backend>.py` → STORAGE.md#storage-abi.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import IO, Any, cast

import pytest

from txstore import StorageException, StorageMetrics
from txstore._streams import GuardedStream


class _Unreliable(io.BytesIO):
    """A seekable body whose reads and seeks fail with `error` once `broken` is set."""

    def __init__(self, data: bytes, error: Exception) -> None:
        super().__init__(data)
        self.error = error
        self.broken = False
        self.name = "body.bin"

    def read(self, size: int | None = -1) -> bytes:
        if self.broken:
            raise self.error
        return super().read(size)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if self.broken:
            raise self.error
        return super().seek(offset, whence)

    def tell(self) -> int:
        if self.broken:
            raise self.error
        return super().tell()

    def readline(self, size: int | None = -1) -> bytes:
        if self.broken:
            raise self.error
        return super().readline(size)


def _guard(inner: IO[bytes], metrics: StorageMetrics | None = None) -> GuardedStream:
    return GuardedStream(
        inner, errors=(OSError,), path="a.bin", metrics=metrics or StorageMetrics()
    )


@pytest.mark.parametrize("size", [-1, None], ids=["negative", "none"])
def test_guarded_stream_reads_to_the_end(size: int | None) -> None:
    stream = _guard(io.BytesIO(b"hello"))
    assert stream.readable()
    assert stream.read(size) == b"hello"


def test_guarded_stream_reads_into_a_buffer() -> None:
    stream = _guard(io.BytesIO(b"hello"))
    buffer = bytearray(3)
    assert stream.readinto(buffer) == 3
    assert buffer == b"hel"


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.read(1),
        lambda s: s.readline(),
        lambda s: s.seek(0),
        lambda s: s.tell(),
    ],
    ids=["read", "readline", "seek", "tell"],
)
def test_guarded_stream_maps_a_listed_error(call: Callable[[GuardedStream], object]) -> None:
    metrics = StorageMetrics()
    inner = _Unreliable(b"hello", OSError("io"))
    stream = _guard(inner, metrics)
    inner.broken = True
    with pytest.raises(StorageException, match="Error reading a.bin: io") as excinfo:
        call(stream)
    assert excinfo.value.__cause__ is inner.error
    assert metrics.read_errors_total == 1


class _CountingRaw(io.RawIOBase):
    """A raw file that counts the reads it serves."""

    def __init__(self, data: bytes) -> None:
        self._body = io.BytesIO(data)
        self.reads = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        self.reads += 1
        return self._body.readinto(buffer)


def test_guarded_stream_reads_a_line_through_the_buffered_body() -> None:
    """A long line costs a few reads on an `fs` file, not one per byte → STORAGE.md."""
    raw = _CountingRaw(b"x" * 1000 + b"\nrest")
    stream = _guard(cast(IO[bytes], io.BufferedReader(raw)))
    assert stream.readline() == b"x" * 1000 + b"\n"
    assert raw.reads < 3
    assert list(stream) == [b"rest"]


def test_guarded_stream_reads_a_line_from_a_body_without_readline() -> None:
    class _ReadOnly:
        def __init__(self) -> None:
            self._body = io.BytesIO(b"one\ntwo")

        def read(self, size: int = -1) -> bytes:
            return self._body.read(size)

        def close(self) -> None:
            self._body.close()

    stream = _guard(cast(IO[bytes], _ReadOnly()))
    assert stream.readline() == b"one\n"
    assert stream.readline(None) == b"two"


def test_guarded_stream_lets_an_unlisted_error_through() -> None:
    inner = _Unreliable(b"hello", ValueError("caller bug"))
    stream = _guard(inner)
    inner.broken = True
    with pytest.raises(ValueError, match="caller bug"):
        stream.read(1)


def test_guarded_stream_seeks_a_seekable_body_and_keeps_its_name() -> None:
    stream = _guard(_Unreliable(b"hello", OSError("io")))
    assert stream.seek(2) == 2
    assert stream.tell() == 2
    assert stream.read() == b"llo"
    assert stream.name == "body.bin"


def test_guarded_stream_has_no_name_when_the_body_has_none() -> None:
    assert not hasattr(_guard(io.BytesIO(b"")), "name")


def test_guarded_stream_close_closes_the_body_once() -> None:
    inner = io.BytesIO(b"hello")
    with _guard(inner) as stream:
        pass
    assert inner.closed
    assert stream.closed
    stream.close()
