"""Tests for txstore/base.py: the shared `Storage` surface every backend inherits.

Backend-specific behavior lives in `test_storage_<backend>.py`; this file covers only what
`base.py` itself owns (STORAGE.md#storage-abi).
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from typing import IO

import pytest

from txstore import (
    CHUNK_SIZE,
    REQUEST_TIMEOUT,
    Storage,
    StorageException,
    StorageNotFoundException,
    StorageNull,
    StorageType,
)


class _MemoryStorage(Storage):
    """Minimal in-memory backend: exercises `transfer` without a real backend."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        super().__init__()
        self.objects: dict[str, bytes] = dict(objects or {})
        self.acls: dict[str, str | None] = {}

    def get_type(self) -> StorageType:
        return StorageType.fs

    def list(self) -> list[str]:
        return sorted(self.objects)

    def get(self, path: str) -> bytes:
        try:
            return self.objects[path]
        except KeyError as exc:
            raise StorageNotFoundException(f"Object not found: {path}") from exc

    def get_stream(self, path: str) -> io.BytesIO:
        return io.BytesIO(self.get(path))

    def get_metadata(self, path: str) -> dict[str, str]:
        return {}

    def exists(self, path: str) -> bool:
        return path in self.objects

    def put_stream(
        self,
        path: str,
        stream: IO[bytes],
        metadata: dict[str, str] | None = None,
        acl: str | None = None,
    ) -> None:
        self.objects[path] = stream.read()
        self.acls[path] = acl
        self.metrics.write_bytes_inc(len(self.objects[path]))

    def add_metadata(self, path: str, metadata: dict[str, str], acl: str | None = None) -> None:
        return None

    def delete(self, path: str) -> None:
        del self.objects[path]

    def delete_metadata(self, path: str) -> None:
        return None

    def delete_metadata_fields(self, path: str, keys: Sequence[str]) -> None:
        return None

    def copy(self, path_src: str, path_dst: str) -> None:
        self.objects[path_dst] = self.objects[path_src]

    def move(self, path_src: str, path_dst: str) -> None:
        self.objects[path_dst] = self.objects.pop(path_src)

    def format(self) -> None:
        self.objects.clear()

    def mkdir(self, path: str) -> None:
        return None

    def rmdir(self, path: str) -> None:
        return None


# --- schemes and types --------------------------------------------------------------------------


def test_storage_type_str_is_the_member_name() -> None:
    assert str(StorageType.s3) == "s3"
    assert str(StorageType.fs) == "fs"


def test_streaming_constants_match_the_contract() -> None:
    assert CHUNK_SIZE == 1024 * 1024
    assert REQUEST_TIMEOUT == 30


# --- exception hierarchy ------------------------------------------------------------------------


def test_not_found_is_a_storage_exception() -> None:
    """The Generic exit group catches it through `StorageException`."""
    assert issubclass(StorageNotFoundException, StorageException)


def test_not_found_is_catchable_as_a_storage_exception() -> None:
    with pytest.raises(StorageException):
        _MemoryStorage().get("missing")


# --- transfer -----------------------------------------------------------------------------------


def test_transfer_streams_the_object_and_returns_every_digest() -> None:
    src = _MemoryStorage({"a.bin": b"hello"})
    dst = _MemoryStorage()

    digests = src.transfer("a.bin", dst)

    assert dst.objects == {"a.bin": b"hello"}
    assert set(digests) == {"MD5Sum", "SHA1", "SHA256", "SHA512", "Size"}
    assert digests["Size"] == "5"


def test_transfer_defaults_the_destination_path_to_the_source() -> None:
    src = _MemoryStorage({"pool/a.deb": b"x"})
    dst = _MemoryStorage()

    src.transfer("pool/a.deb", dst)

    assert list(dst.objects) == ["pool/a.deb"]


def test_transfer_renames_when_given_a_destination_path() -> None:
    src = _MemoryStorage({"a.bin": b"x"})
    dst = _MemoryStorage()

    src.transfer("a.bin", dst, path_dst="b.bin")

    assert list(dst.objects) == ["b.bin"]


def test_transfer_passes_the_acl_to_the_destination() -> None:
    src = _MemoryStorage({"a.bin": b"x"})
    dst = _MemoryStorage()

    src.transfer("a.bin", dst, acl="private")

    assert dst.acls["a.bin"] == "private"


def test_transfer_counts_bytes_once_on_each_side() -> None:
    """The source counts its read; the destination's `put_stream` counts its own write."""
    src = _MemoryStorage({"a.bin": b"12345"})
    dst = _MemoryStorage()

    src.transfer("a.bin", dst)

    assert src.metrics.dump()["read_bytes"] == 5
    assert dst.metrics.dump()["write_bytes"] == 5


def test_transfer_into_null_counts_the_written_bytes_once() -> None:
    sink = StorageNull()

    _MemoryStorage({"a.bin": b"12345"}).transfer("a.bin", sink)

    assert sink.metrics.dump()["write_bytes"] == 5


def test_put_writes_through_put_stream() -> None:
    storage = _MemoryStorage()

    storage.put("a.bin", b"abc", acl="private")

    assert (storage.objects["a.bin"], storage.acls["a.bin"]) == (b"abc", "private")


def test_transfer_of_a_missing_source_raises_not_found() -> None:
    with pytest.raises(StorageNotFoundException):
        _MemoryStorage().transfer("gone.bin", _MemoryStorage())


def test_transfer_into_a_digest_sink_keeps_the_digests() -> None:
    """A file is verified by transferring it into `StorageNull`."""
    digests = _MemoryStorage({"a.bin": b"hello"}).transfer("a.bin", StorageNull())

    assert digests["Size"] == "5"
