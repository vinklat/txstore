"""Tests for txstore/base.py: the shared `Storage` surface every backend inherits.

Backend-specific behavior lives in `test_storage_<backend>.py`; this file covers only what
`base.py` itself owns (STORAGE.md#storage-abi).
"""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from typing import IO, Any, cast

import pytest

from txstore import (
    CHUNK_SIZE,
    REQUEST_TIMEOUT,
    Storage,
    StorageException,
    StorageNotFoundException,
    StorageNull,
    StorageTooLargeException,
    StorageType,
    calc_digests,
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

    def get_stream(self, path: str) -> IO[bytes]:
        try:
            return io.BytesIO(self.objects[path])
        except KeyError as exc:
            raise StorageNotFoundException(f"Object not found: {path}") from exc

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


# --- size cap → STORAGE.md#storage-abi, VALIDATION.md#read-size-cap -----------------------------


class _Endless(io.RawIOBase):
    """A body that never ends, as a hostile origin sends it; counts the bytes pulled."""

    def __init__(self) -> None:
        self.pulled = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        self.pulled += len(buffer)
        return len(buffer)


class _EndlessStorage(_MemoryStorage):
    def __init__(self) -> None:
        super().__init__()
        self.body = _Endless()

    def get_stream(self, path: str) -> IO[bytes]:
        return cast(IO[bytes], self.body)


@pytest.mark.parametrize(
    ("body", "max_size"),
    [(b"", 0), (b"abc", 3), (b"abc", 4)],
    ids=["empty-at-zero", "at-cap", "under-cap"],
)
def test_get_returns_an_object_within_the_cap(body: bytes, max_size: int) -> None:
    storage = _MemoryStorage({"a.bin": body})
    assert storage.get("a.bin", max_size=max_size) == body
    assert storage.metrics.dump()["read_bytes"] == len(body)


@pytest.mark.parametrize(
    ("body", "max_size"), [(b"a", 0), (b"abcd", 3)], ids=["one-over-zero", "one-over-three"]
)
def test_get_refuses_an_object_over_the_cap(body: bytes, max_size: int) -> None:
    storage = _MemoryStorage({"a.bin": body})
    with pytest.raises(StorageTooLargeException, match=f"a.bin is larger than {max_size} bytes"):
        storage.get("a.bin", max_size=max_size)
    assert storage.metrics.read_errors_total == 1
    assert storage.metrics.dump()["read_bytes"] == 0


def test_get_stops_reading_an_endless_body_at_the_cap() -> None:
    storage = _EndlessStorage()
    with pytest.raises(StorageTooLargeException):
        storage.get("a.bin", max_size=10)
    assert storage.body.pulled == 11


def test_transfer_stops_reading_an_endless_body_at_the_cap() -> None:
    source = _EndlessStorage()
    dst = _MemoryStorage()
    with pytest.raises(StorageTooLargeException):
        source.transfer("a.bin", dst, max_size=10)
    assert source.body.pulled == 11
    assert dst.objects == {}


def test_transfer_within_the_cap_returns_the_digests() -> None:
    digests = _MemoryStorage({"a.bin": b"abc"}).transfer("a.bin", _MemoryStorage(), max_size=3)
    assert digests["Size"] == "3"


def test_transfer_over_the_cap_into_a_sink_raises_the_refusal_unchanged() -> None:
    """The destination counts its write error and lets the refusal through, unwrapped."""
    source = _MemoryStorage({"a.bin": b"abcd"})
    sink = StorageNull()
    with pytest.raises(StorageTooLargeException, match="a.bin is larger than 3 bytes") as excinfo:
        source.transfer("a.bin", sink, max_size=3)
    assert excinfo.value.__cause__ is None
    assert source.metrics.read_errors_total == 1
    assert sink.metrics.write_errors_total == 1


def test_transfer_under_the_cap_lets_a_destination_failure_through() -> None:
    class _Refusing(_MemoryStorage):
        def put_stream(
            self,
            path: str,
            stream: IO[bytes],
            metadata: dict[str, str] | None = None,
            acl: str | None = None,
        ) -> None:
            raise StorageException("disk full")

    with pytest.raises(StorageException, match="disk full") as excinfo:
        _MemoryStorage({"a.bin": b"abcd"}).transfer("a.bin", _Refusing(), max_size=10)
    assert not isinstance(excinfo.value, StorageTooLargeException)


@pytest.mark.parametrize(
    "max_size", [-1, True, 1.5, "10"], ids=["negative", "bool", "float", "str"]
)
@pytest.mark.parametrize(
    "read",
    [lambda s, m: s.get("gone", max_size=m), lambda s, m: s.transfer("gone", s, max_size=m)],
    ids=["get", "transfer"],
)
def test_capped_reads_refuse_an_invalid_max_size_before_any_io(
    read: Callable[[Storage, Any], object], max_size: object
) -> None:
    """A missing object would raise `StorageNotFoundException`; the argument is checked first."""
    with pytest.raises(ValueError, match="max_size must be None or an integer of 0 or more"):
        read(_MemoryStorage(), max_size)


def test_transfer_keeps_refusing_a_destination_that_reads_again() -> None:
    """A destination that swallows the refusal and reads on gets it again, never more bytes."""

    class _Retrying(_MemoryStorage):
        def put_stream(
            self,
            path: str,
            stream: IO[bytes],
            metadata: dict[str, str] | None = None,
            acl: str | None = None,
        ) -> None:
            with pytest.raises(StorageTooLargeException):
                stream.read()
            stream.read(1)

    source = _EndlessStorage()
    with pytest.raises(StorageTooLargeException):
        source.transfer("a.bin", _Retrying(), max_size=10)
    assert source.body.pulled == 11
    assert source.metrics.read_errors_total == 1


# --- get_digests → STORAGE.md#digests ----------------------------------------------------------


def test_get_digests_matches_calc_digests_and_counts_the_read() -> None:
    body = b"x" * (CHUNK_SIZE + 5)
    storage = _MemoryStorage({"a.bin": body})
    assert storage.get_digests("a.bin") == calc_digests(body)
    assert storage.metrics.dump()["read_bytes"] == len(body)


def test_get_digests_within_the_cap() -> None:
    assert _MemoryStorage({"a.bin": b"abc"}).get_digests("a.bin", max_size=3)["Size"] == "3"


def test_get_digests_stops_reading_an_endless_body_at_the_cap() -> None:
    storage = _EndlessStorage()
    with pytest.raises(StorageTooLargeException):
        storage.get_digests("a.bin", max_size=10)
    assert storage.body.pulled == 11


def test_get_digests_refuses_an_invalid_max_size() -> None:
    with pytest.raises(ValueError, match="max_size"):
        _MemoryStorage().get_digests("gone", max_size=-1)


def test_get_digests_of_a_missing_object_raises_not_found() -> None:
    with pytest.raises(StorageNotFoundException):
        _MemoryStorage().get_digests("gone")


class _RecordingStorage(_MemoryStorage):
    """Serves one body and records every read size `get` asks its stream for."""

    def __init__(self, body: bytes) -> None:
        super().__init__({"a.bin": body})
        self.sizes: list[int | None] = []

    def get_stream(self, path: str) -> IO[bytes]:
        sizes = self.sizes

        class _Body(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                sizes.append(size)
                return super().read(size)

        return _Body(self.objects[path])


@pytest.mark.parametrize(
    ("read", "expected"),
    [
        (lambda s: s.get("a.bin"), [-1]),
        (lambda s: s.get("a.bin", max_size=2**40), [CHUNK_SIZE, CHUNK_SIZE]),
        (lambda s: s.transfer("a.bin", _MemoryStorage(), max_size=2**40), [CHUNK_SIZE] * 2),
    ],
    ids=["get-uncapped-one-read", "get-capped-in-chunks", "transfer-capped-in-chunks"],
)
def test_capped_reads_never_ask_the_backend_for_the_cap_at_once(
    read: Callable[[Storage], object], expected: list[int]
) -> None:
    """A cap of 1 TiB must not ask the backend for 1 TiB in one read, even when the
    destination reads to the end, as `_MemoryStorage.put_stream` does."""
    storage = _RecordingStorage(b"hello")
    read(storage)
    assert storage.sizes == expected


_CROSS_CHUNK_READS = [
    pytest.param(lambda s, m: s.get("a.bin", max_size=m), id="get"),
    pytest.param(lambda s, m: s.get_digests("a.bin", max_size=m)["Size"], id="get_digests"),
    pytest.param(
        lambda s, m: s.transfer("a.bin", StorageNull(), max_size=m)["Size"], id="transfer"
    ),
]


@pytest.mark.parametrize("read", _CROSS_CHUNK_READS)
def test_capped_reads_count_the_size_across_chunks(
    read: Callable[[Storage, int], object],
) -> None:
    """The running count spans `CHUNK_SIZE` reads: one byte over two chunks is refused."""
    over = _MemoryStorage({"a.bin": b"x" * (2 * CHUNK_SIZE + 1)})
    with pytest.raises(StorageTooLargeException):
        read(over, 2 * CHUNK_SIZE)
    assert over.metrics.read_errors_total == 1
    assert over.metrics.dump()["read_bytes"] == 0

    at = _MemoryStorage({"a.bin": b"x" * (2 * CHUNK_SIZE)})
    assert read(at, 2 * CHUNK_SIZE) in (at.objects["a.bin"], str(2 * CHUNK_SIZE))
    assert at.metrics.dump()["read_bytes"] == 2 * CHUNK_SIZE
