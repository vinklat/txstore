"""Shared storage helpers and stubs."""

from __future__ import annotations

import builtins
import io
import json
import signal
import unicodedata
from collections.abc import Hashable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import IO, Protocol

import pytest

from txstore import (
    FootmarkMixin,
    Storage,
    StorageException,
    StorageFS,
    StorageNotFoundException,
    StorageS3,
    StorageType,
    TransactionModel,
    maybe_transaction,
)
from txstore._reserved import HISTORY_FILENAME, LOCK_FILENAME
from txstore.transaction import _CommitResult

# Reserved storage-root names, for tests whose module under test is not `_reserved`.
__all__ = ["HISTORY_FILENAME", "LOCK_FILENAME"]


class TransactionalStorage(Protocol):
    """The slice of a transactional backend `commit_transaction` drives."""

    def put(self, path: str, data: bytes) -> None: ...

    def transaction_begin(self, transaction_id: int = 0) -> TransactionModel: ...

    def transaction_commit(self) -> bool: ...


def seed_history(storage: FootmarkMixin, records: Sequence[Mapping[str, object]]) -> None:
    """Write raw history records into ``.transactions.json``.

    Writes the JSON as given, so a test can seed a malformed record that
    ``TransactionHistory.save`` would never produce.

    Args:
        storage: A transactional backend or stub.
        records: History array entries (``{id, timestamp}`` objects).
    """
    payload = json.dumps(list(records), separators=(",", ":")).encode("utf-8")
    storage._obj_put_reserved(HISTORY_FILENAME, payload)


def abandon_transaction(storage: FootmarkMixin) -> None:
    """Forget the active transaction without commit or discard: a writer that crashed."""
    storage._active = None


def commit_transaction(
    storage: TransactionalStorage, path: str, data: bytes = b"payload"
) -> TransactionModel:
    """Run one committed transaction writing *path* and return its model."""
    model = storage.transaction_begin()
    storage.put(path, data)
    storage.transaction_commit()
    return model


def seed_old_and_new(storage: StorageFS | StorageS3) -> tuple[TransactionModel, TransactionModel]:
    """Commit ``old.deb`` then ``new.deb`` and record both, as `maybe_transaction` would have."""
    old = commit_transaction(storage, "old.deb", b"old")
    new = commit_transaction(storage, "new.deb", b"new")
    seed_history(storage, [transaction_record(old), transaction_record(new)])
    return old, new


def footmarks_of(paths: Iterable[str], model: TransactionModel) -> list[str]:
    """The *paths* whose basename ends in one of *model*'s `.<token>` suffixes, sorted."""
    suffixes = tuple(f".{token}" for token in model.footmarks)
    return sorted(p for p in paths if p.rsplit("/", 1)[-1].endswith(suffixes))


def iso_timestamp(days_ago: float, *, now: datetime | None = None) -> str:
    """History-record timestamp *days_ago* days before *now* (default: current UTC time)."""
    return ((now or datetime.now(UTC)) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def transaction_record(model: TransactionModel) -> dict[str, object]:
    """History record exactly as `maybe_transaction` would have written it.

    Footmark tokens regenerate from ``{id, timestamp}``, so a record must carry
    the transaction's real begin timestamp for retention to name its footmarks.
    Age is then controlled by moving `prune`'s reference time instead.
    """
    return {"id": model.id, "timestamp": model.timestamp.to_json()}


def put_then_fail(storage: Storage) -> None:
    """Write inside `maybe_transaction`, then raise before it commits."""
    with maybe_transaction(storage):
        storage.put("x.deb", b"x")
        raise RuntimeError("boom")


class Pipe:
    """A read-only stream with no `seek`, `tell`, or `seekable`, like `ChecksumOpen`."""

    def __init__(self, data: bytes) -> None:
        self._data = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._data.read(size)


class FailingStream(io.RawIOBase):
    """A non-seekable stream that yields *first_chunk*, then raises *error* (default `OSError`)."""

    def __init__(self, first_chunk: bytes = b"partial", error: Exception | None = None) -> None:
        self._chunk = first_chunk
        self._error = error or OSError("simulated mid-stream failure")
        self._exhausted = False

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray) -> int:  # type: ignore[override]
        if self._exhausted:
            raise self._error
        self._exhausted = True
        n = len(self._chunk)
        b[:n] = self._chunk
        return n


def _fold(name: str) -> str:
    """`name` as a case- and Unicode-insensitive filesystem compares it.

    Kept apart from `txstore.transaction._folded`, so an alias test still fails when that breaks.
    """
    return unicodedata.normalize("NFD", name).casefold()


class FoldingDict[V](MutableMapping[str, V]):
    """Name-preserving mapping whose keys fold like macOS APFS, vfat, or SMB.

    A write under another spelling of a stored key replaces its value and keeps its name, as
    opening an existing file under an alias does.
    """

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, V]] = {}

    def __getitem__(self, key: str) -> V:
        return self._items[_fold(key)][1]

    def __setitem__(self, key: str, value: V) -> None:
        stored = self._items.get(_fold(key))
        self._items[_fold(key)] = (stored[0] if stored else key, value)

    def __delitem__(self, key: str) -> None:
        del self._items[_fold(key)]

    def __iter__(self) -> Iterator[str]:
        return (name for name, _ in self._items.values())

    def __len__(self) -> int:
        return len(self._items)


@dataclass
class MemoryRoot:
    """The shared state of one in-memory storage root: what a peer instance also sees."""

    data: MutableMapping[str, bytes] = field(default_factory=dict)
    metadata: MutableMapping[str, dict[str, str]] = field(default_factory=dict)


class MemoryTxnStorage(FootmarkMixin, Storage):
    """In-memory transactional backend: `FootmarkMixin` over dict primitives.

    Exercises the mixin's overlay and lifecycle with no filesystem or S3 underneath. Two
    instances built on one `MemoryRoot` behave like two processes on one storage root.
    """

    def __init__(self, root: MemoryRoot | None = None) -> None:
        super().__init__()
        self.root = root if root is not None else MemoryRoot()

    def get_type(self) -> StorageType:
        """Memory has no scheme of its own."""
        return StorageType.null

    # Backend primitives

    def _obj_full(self, rel: str) -> str:
        return rel

    def _obj_iter_rel_all(self) -> Iterable[str]:
        return list(self.root.data)

    def _obj_exists(self, full: str) -> bool:
        return full in self.root.data

    def _obj_copy(self, src_full: str, dst_full: str) -> None:
        self.root.data[dst_full] = self.root.data[src_full]
        self.root.metadata[dst_full] = dict(self.root.metadata.get(src_full, {}))

    def _obj_delete(self, full: str) -> None:
        self.root.data.pop(full, None)
        self.root.metadata.pop(full, None)

    def _obj_touch(self, full: str) -> None:
        self.root.data[full] = b""

    def _obj_lock(self) -> AbstractContextManager[None]:
        return nullcontext()

    def _obj_lock_identity(self) -> Hashable:
        # Instances sharing one `MemoryRoot` share its lock.
        return ("memory", id(self.root))

    def _obj_put_reserved(self, rel: str, data: bytes) -> None:
        self.root.data[rel] = data

    def _obj_get_reserved(self, rel: str) -> bytes | None:
        return self.root.data.get(rel)

    # Storage ABI

    def list(self) -> builtins.list[str]:
        """Regular objects, as the active transaction sees them."""
        return self._overlay_list()

    def get(self, path: str) -> bytes:
        """Object bytes."""
        with self.get_stream(path) as stream:
            return stream.read()

    def get_stream(self, path: str) -> IO[bytes]:
        """Object bytes as a stream."""
        full = self._read_target(path)
        if full not in self.root.data:
            raise StorageNotFoundException(f"Object not found: {path}")
        return io.BytesIO(self.root.data[full])

    def get_metadata(self, path: str) -> dict[str, str]:
        """A copy of the object's metadata."""
        full = self._source_target(path)
        if full not in self.root.data:
            raise StorageNotFoundException(f"Object not found: {path}")
        return dict(self.root.metadata.get(full, {}))

    def exists(self, path: str) -> bool:
        """Existence, as the active transaction sees it."""
        return self._overlay_exists(path)

    def put_stream(
        self,
        path: str,
        stream: IO[bytes],
        metadata: dict[str, str] | None = None,
        acl: str | None = None,
    ) -> None:
        """Write the stream; metadata replaces the object's."""
        with self._stage_write(path) as full:
            self.root.data[full] = stream.read()
            self.root.metadata[full] = dict(metadata or {})
        self.metrics.write_bytes_inc(len(self.root.data[full]))
        self.metrics.write_objects_inc()

    def add_metadata(self, path: str, metadata: dict[str, str], acl: str | None = None) -> None:
        """Merge metadata; a missing object is refused like on every real backend."""
        full = self._metadata_target(path)
        if full not in self.root.data:
            raise StorageException(f"Failed to set metadata of {path}: no such object")
        self.root.metadata.setdefault(full, {}).update(metadata)

    def delete(self, path: str) -> None:
        """Delete, or stage the delete."""
        self._stage_delete(path)
        self.metrics.delete_objects_inc()

    def delete_metadata(self, path: str) -> None:
        """Drop every metadata key."""
        self.root.metadata[self._metadata_target(path)] = {}

    def delete_metadata_fields(self, path: str, keys: builtins.list[str]) -> None:
        """Drop the named metadata keys."""
        meta = self.root.metadata.setdefault(self._metadata_target(path), {})
        for key in keys:
            meta.pop(key, None)

    def copy(self, path_src: str, path_dst: str) -> None:
        """Copy data and metadata."""
        self._copy_object(path_src, path_dst)

    def move(self, path_src: str, path_dst: str) -> None:
        """Copy, then delete the source → `FootmarkMixin._move_object`."""
        self._move_object(path_src, path_dst)

    def format(self) -> None:
        """Drop everything."""
        self.root.data.clear()
        self.root.metadata.clear()

    def mkdir(self, path: str) -> None:
        """No directories."""

    def rmdir(self, path: str) -> None:
        """No directories."""


class FoldingMemoryTxnStorage(MemoryTxnStorage):
    """`MemoryTxnStorage` whose names fold like macOS APFS, vfat, or SMB.

    Runs the alias paths of the overlay on every host; `StorageFS` runs them only where the host
    filesystem folds.
    """

    def __init__(self) -> None:
        super().__init__(MemoryRoot(data=FoldingDict(), metadata=FoldingDict()))

    def _obj_same(self, full_a: str, full_b: str) -> bool:
        return _fold(full_a) == _fold(full_b) and full_a in self.root.data


class TxnStorageStub(MemoryTxnStorage):
    """Transactional stub whose lifecycle only records calls.

    ``maybe_transaction`` tests assert the exact call sequence through ``began``,
    ``committed``, and ``discarded``; history reads and writes go to memory.
    """

    def __init__(self) -> None:
        super().__init__()
        self.began = False
        self.committed = False
        self.discarded = False

    def transaction_begin(self, transaction_id: int = 0) -> TransactionModel:
        """Record that a transaction was started."""
        model = super().transaction_begin(transaction_id)
        self.began = True
        return model

    def _commit_listed(self) -> _CommitResult:
        """Record the commit and report changes, so ``maybe_transaction`` appends history."""
        self._active = None
        self.committed = True
        return _CommitResult(changed=True, hidden=[])

    def transaction_revert(self, transaction: TransactionModel) -> None:
        """No-op revert."""

    def transaction_discard(self) -> None:
        """Record that the transaction was discarded."""
        self._active = None
        self.discarded = True


@contextmanager
def deadline(seconds: int) -> Iterator[None]:
    """Fail the block if it blocks longer than *seconds*: no pytest-timeout plugin is installed.

    The alarm fails the test with a `BaseException`, so no ``except Exception`` in the code under
    test can wrap the hang into an ordinary error. Main thread only; it cancels any outer alarm.
    """

    def expire(signum: int, frame: object) -> None:
        pytest.fail(f"blocked for {seconds}s", pytrace=False)

    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
