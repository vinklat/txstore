"""Contract test for `StorageNull` (write-only /dev/null backend)."""

from __future__ import annotations

import io

import pytest
from urllib3.exceptions import ProtocolError

from txstore import StorageException, StorageNull, StorageType


@pytest.fixture
def storage() -> StorageNull:
    return StorageNull()


def test_storage_null_get_type(storage: StorageNull) -> None:
    assert storage.get_type() is StorageType.null


def test_storage_null_put_tracks_metrics(storage: StorageNull) -> None:
    storage.put("path", b"hello")
    assert storage.metrics.write_objects_total == 1
    assert storage.metrics.write_bytes == 5


def test_storage_null_put_stream_consumes_input(storage: StorageNull) -> None:
    storage.put_stream("p", io.BytesIO(b"streamed"))
    assert storage.metrics.write_objects_total == 1


def test_storage_null_delete_is_noop_with_metric(storage: StorageNull) -> None:
    storage.delete("anything")
    assert storage.metrics.delete_objects_total == 1


def test_storage_null_metadata_is_noop(storage: StorageNull) -> None:
    storage.add_metadata("p", {"k": "v"})
    storage.delete_metadata("p")
    storage.delete_metadata_fields("p", ["k"])


@pytest.mark.parametrize(
    "path", [".transactions.json", ".TRANSACTIONS.LOCK", "", "pool/..", "../x"]
)
def test_storage_null_checks_no_path(storage: StorageNull, path: str) -> None:
    """A verify sink takes any source key → VALIDATION.md#filename-safety."""
    storage.put(path, b"x")
    storage.add_metadata(path, {"k": "v"})
    storage.delete_metadata(path)
    storage.delete_metadata_fields(path, ["k"])
    storage.delete(path)
    storage.rmdir(path)
    assert storage.metrics.write_objects_total == 1
    assert storage.metrics.delete_objects_total == 1


def test_storage_null_format_and_rmdir_are_noops(storage: StorageNull) -> None:
    storage.format()
    storage.rmdir("anything")


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("list", ()),
        ("get", ("p",)),
        ("get_metadata", ("p",)),
        ("exists", ("p",)),
        ("get_stream", ("p",)),
        ("copy", ("a", "b")),
        ("move", ("a", "b")),
        ("mkdir", ("d",)),
    ],
)
def test_storage_null_read_methods_raise(
    storage: StorageNull, method: str, args: tuple[object, ...]
) -> None:
    with pytest.raises(NotImplementedError):
        getattr(storage, method)(*args)


def test_storage_null_is_never_in_a_transaction(storage: StorageNull) -> None:
    assert storage.in_transaction() is False


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("transaction_begin", ()),
        ("transaction_commit", ()),
        ("transaction_revert", (None,)),
        ("transaction_discard", ()),
    ],
)
def test_storage_null_has_no_transaction_support(
    storage: StorageNull, method: str, args: tuple[object, ...]
) -> None:
    with pytest.raises(NotImplementedError):
        getattr(storage, method)(*args)


class _BrokenStream(io.BytesIO):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    def read(self, size: int | None = -1) -> bytes:
        raise self._error


@pytest.mark.parametrize(
    "error",
    [
        OSError("read failed"),
        # A dropped connection under an HTTP transfer: not an OSError.
        ProtocolError("Connection broken: IncompleteRead"),
        ValueError("caller stream bug"),
    ],
    ids=["oserror", "urllib3", "other"],
)
def test_storage_null_put_stream_wraps_any_stream_failure(
    storage: StorageNull, error: Exception
) -> None:
    with pytest.raises(StorageException, match="Error reading the stream") as info:
        storage.put_stream("p", _BrokenStream(error))
    assert info.value.__cause__ is error
    assert storage.metrics.dump()["write_errors_total"] == 1
    assert storage.metrics.dump()["write_objects_total"] == 0


def test_storage_null_put_stream_accepts_metadata(storage: StorageNull) -> None:
    storage.put_stream("p", io.BytesIO(b"abc"), metadata={"k": "v"})
    assert storage.metrics.dump()["write_objects_total"] == 1
