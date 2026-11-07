"""Tests for `TransactionHistory` (txstore/history.py)."""

from __future__ import annotations

import dataclasses
import errno
import fcntl
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import responses
from helpers.storage import (
    HISTORY_FILENAME,
    LOCK_FILENAME,
    MemoryTxnStorage,
    abandon_transaction,
    commit_transaction,
    deadline,
    footmarks_of,
    iso_timestamp,
    put_then_fail,
    seed_history,
    seed_old_and_new,
    transaction_record,
)

from txstore import (
    FootmarkMixin,
    HistoryRetention,
    StorageException,
    StorageFS,
    StorageHTTP,
    StorageNull,
    Timestamp,
    TransactionHistory,
    TransactionModel,
    history_lock,
    maybe_transaction,
)


def _footmark_files(storage: StorageFS, model: TransactionModel) -> list[str]:
    """Relative paths of every footmark object belonging to *model*."""
    files = (
        str(p.relative_to(storage.base_dir))
        for p in Path(storage.base_dir).rglob("*")
        if p.is_file()
    )
    return footmarks_of(files, model)


def _model(record: dict[str, object]) -> TransactionModel:
    return TransactionHistory.record_to_model(record)


def _write_history(storage: StorageFS, raw: bytes) -> None:
    """Write raw bytes as the history file, bypassing every check."""
    storage._obj_put_reserved(HISTORY_FILENAME, raw)


@pytest.fixture
def storage(tmp_path: Path) -> StorageFS:
    return StorageFS(str(tmp_path / "repo"))


@pytest.fixture
def history(storage: StorageFS) -> TransactionHistory:
    return TransactionHistory(storage)


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #


def test_load_empty_when_no_file(history: TransactionHistory) -> None:
    assert history.load() == []


_MIRROR = "http://repo.example.com"


@responses.activate
def test_load_reads_a_non_transactional_storage_through_get() -> None:
    """A read-only mirror carries the history as a plain object → `STORAGE.md#operations`."""
    record = {"id": 3, "timestamp": "2026-01-01T00:00:00Z"}
    responses.add(responses.GET, f"{_MIRROR}/.transactions.json", json=[record])
    assert [m.id for m in TransactionHistory(StorageHTTP(_MIRROR)).load()] == [3]


@responses.activate
def test_load_of_a_mirror_without_a_history_is_empty() -> None:
    responses.add(responses.GET, f"{_MIRROR}/.transactions.json", status=404)
    assert TransactionHistory(StorageHTTP(_MIRROR)).load() == []


def test_load_reads_once_without_exists(storage: StorageFS, history: TransactionHistory) -> None:
    """Missing history is detected by the one reserved read alone (no exists probe)."""
    with (
        patch.object(storage, "exists", side_effect=AssertionError("exists must not be called")),
        patch.object(storage, "get", side_effect=AssertionError("get must not be called")),
        patch.object(storage, "_obj_get_reserved", return_value=None) as mock_read,
    ):
        assert history.load() == []
    mock_read.assert_called_once()


def test_load_never_reads_a_symlinked_history(
    storage: StorageFS, history: TransactionHistory, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(b"[]")
    Path(storage.base_dir, HISTORY_FILENAME).symlink_to(elsewhere)
    with pytest.raises(StorageException, match="Error reading"):
        history.load()


def test_load_of_a_history_that_is_a_directory_raises(
    storage: StorageFS, history: TransactionHistory
) -> None:
    """Every library error of the read surfaces as `StorageException` → EXCEPTIONS.md#rules."""
    Path(storage.base_dir, HISTORY_FILENAME).mkdir()
    with pytest.raises(StorageException, match="Error reading"):
        history.load()


def test_load_returns_existing_records(storage: StorageFS, history: TransactionHistory) -> None:
    records = [{"id": 1, "timestamp": "2026-01-01T00:00:00Z"}]
    seed_history(storage, records)
    assert [transaction_record(m) for m in history.load()] == records


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b'[{"id": ' + b"1" * 5000 + b', "timestamp": "x"}]', id="int-digit-limit"),
    ],
)
def test_load_raises_on_invalid_json(
    storage: StorageFS, history: TransactionHistory, raw: bytes
) -> None:
    """Every parse failure, not only `JSONDecodeError`, is the malformed-history refusal."""
    _write_history(storage, raw)
    with pytest.raises(StorageException, match="Cannot parse.*move or delete"):
        history.load()


def test_load_raises_on_recursion_error(storage: StorageFS, history: TransactionHistory) -> None:
    """A `RecursionError` from the decoder is the malformed-history refusal."""
    _write_history(storage, b"[]")
    with (
        patch("txstore.history.json.loads", side_effect=RecursionError("too deep")),
        pytest.raises(StorageException, match="Cannot parse.*move or delete"),
    ):
        history.load()


def test_load_refuses_too_deep_nesting(storage: StorageFS, history: TransactionHistory) -> None:
    """Deep nesting is refused on every interpreter.

    Python 3.13 raises `RecursionError` while decoding; 3.14 decodes it and the entry check fails.
    """
    _write_history(storage, b"[" * 100_000 + b"]" * 100_000)
    with pytest.raises(StorageException, match="move or delete"):
        history.load()


def test_load_raises_on_non_array(storage: StorageFS, history: TransactionHistory) -> None:
    _write_history(storage, b'{"id": 1}')
    with pytest.raises(StorageException, match="not a JSON array"):
        history.load()


def test_load_raises_on_non_object_entry(storage: StorageFS, history: TransactionHistory) -> None:
    _write_history(storage, b'[{"id": 1, "timestamp": "2026-01-01T00:00:00Z"}, "bad"]')
    with pytest.raises(StorageException, match="non-object entries"):
        history.load()


@pytest.mark.parametrize(
    "bad_id",
    [0, -1, 2**31, True, False, "1", None, 2**31 - 1 + 1],
)
def test_load_rejects_invalid_transaction_id(
    storage: StorageFS, history: TransactionHistory, bad_id: object
) -> None:
    seed_history(storage, [{"id": bad_id, "timestamp": "2026-01-01T00:00:00Z"}])
    with pytest.raises(StorageException, match="malformed transaction id"):
        history.load()


@pytest.mark.parametrize(
    "bad_ts",
    [
        "T",
        "2026-01-01",
        "2026-06-08T10:00:00Z\n999",
        "2026-06-08T10:00:00Z\x1b[2K",
        "2026-06-08 10:00:00Z",
        "",
        123,
    ],
)
def test_load_rejects_invalid_timestamp(
    storage: StorageFS, history: TransactionHistory, bad_ts: object
) -> None:
    _write_history(
        storage,
        json.dumps([{"id": 1, "timestamp": bad_ts}]).encode(),
    )
    with pytest.raises(StorageException, match="malformed transaction timestamp"):
        history.load()


# --------------------------------------------------------------------------- #
# next_id
# --------------------------------------------------------------------------- #


def test_next_id_starts_at_1(history: TransactionHistory) -> None:
    assert history.next_id() == 1


def test_next_id_increments(storage: StorageFS, history: TransactionHistory) -> None:
    records = [{"id": 5, "timestamp": "2026-01-01T00:00:00Z"}]
    seed_history(storage, records)
    assert history.next_id() == 6


def test_next_id_uses_max_not_last(storage: StorageFS, history: TransactionHistory) -> None:
    records = [
        {"id": 3, "timestamp": "2026-01-01T00:00:03Z"},
        {"id": 1, "timestamp": "2026-01-01T00:00:01Z"},
    ]
    seed_history(storage, records)
    assert history.next_id() == 4


def test_next_id_exhausted_raises(storage: StorageFS, history: TransactionHistory) -> None:
    seed_history(storage, [{"id": 2**31 - 1, "timestamp": "2026-01-01T00:00:00Z"}])
    with pytest.raises(StorageException, match="id space exhausted"):
        history.next_id()


# --------------------------------------------------------------------------- #
# append
# --------------------------------------------------------------------------- #


def test_append_creates_file(history: TransactionHistory, storage: StorageFS) -> None:
    history.append(TransactionModel(1, Timestamp.from_json("2026-01-01T00:00:00Z")))

    records = json.loads(storage.get(HISTORY_FILENAME))
    assert len(records) == 1
    assert records[0]["id"] == 1
    assert records[0]["timestamp"] == "2026-01-01T00:00:00Z"
    assert "footmarks" not in records[0]


def test_append_preserves_existing(storage: StorageFS, history: TransactionHistory) -> None:
    existing = [{"id": 1, "timestamp": "2026-01-01T00:00:00Z"}]
    seed_history(storage, existing)

    history.append(TransactionModel(2, Timestamp.from_json("2026-01-02T00:00:00Z")))

    records = json.loads(storage.get(HISTORY_FILENAME))
    assert len(records) == 2
    assert records[0]["id"] == 1
    assert records[1]["id"] == 2


@pytest.mark.parametrize(
    "bad_ts",
    [
        "2026-06-08T10:00:00\x1b[31m",
        "2026-06-08T10:00:00Z\n",
        "2026-06-08 10:00:00Z",
        "",
        "not-a-timestamp",
        "2026-13-01T00:00:00Z",
    ],
    ids=["control-char", "trailing-newline", "space-separator", "empty", "garbage", "month-13"],
)
def test_load_rejects_an_invalid_stored_timestamp(storage: StorageFS, bad_ts: str) -> None:
    seed_history(storage, [{"id": 1, "timestamp": bad_ts}])
    with pytest.raises(StorageException, match="malformed transaction timestamp"):
        TransactionHistory(storage).load()


# --------------------------------------------------------------------------- #
# pop_last
# --------------------------------------------------------------------------- #


def test_pop_last_returns_none_when_empty(history: TransactionHistory) -> None:
    assert history.pop_last() is None


def test_pop_last_removes_and_returns(storage: StorageFS, history: TransactionHistory) -> None:
    records = [
        {"id": 1, "timestamp": "2026-01-01T00:00:01Z"},
        {"id": 2, "timestamp": "2026-01-01T00:00:02Z"},
    ]
    seed_history(storage, records)

    removed = history.pop_last()
    assert removed is not None
    assert removed.id == 2

    remaining = json.loads(storage.get(HISTORY_FILENAME))
    assert len(remaining) == 1
    assert remaining[0]["id"] == 1


# --------------------------------------------------------------------------- #
# record_to_model
# --------------------------------------------------------------------------- #


def test_record_to_model_regenerates_footmarks() -> None:
    ts = Timestamp.from_json("2026-06-08T10:15:30.123456789Z")
    record = {"id": 7, "timestamp": "2026-06-08T10:15:30.123456789Z"}

    model = TransactionHistory.record_to_model(record)

    assert model == TransactionModel(7, ts)


def test_load_rejects_a_timestamp_that_is_no_real_instant(
    storage: StorageFS, history: TransactionHistory
) -> None:
    """The RFC 3339 regex admits month 13; the parse every record passes on load does not."""
    seed_history(storage, [{"id": 1, "timestamp": "2000-13-45T99:99:99Z"}])
    with pytest.raises(StorageException, match="malformed transaction timestamp"):
        history.load()


def test_record_to_model_roundtrips_with_append(history: TransactionHistory) -> None:
    ts = Timestamp.from_json("2026-06-08T10:15:30Z")
    original = TransactionModel(3, ts)
    history.append(original)

    assert history.load()[-1] == original


_GOOD_TS = "2026-01-01T00:00:00Z"


@pytest.mark.parametrize(
    ("record", "field"),
    [
        pytest.param({"id": "x", "timestamp": _GOOD_TS}, "id", id="bad-id-type"),
        pytest.param({"id": 2**31, "timestamp": _GOOD_TS}, "id", id="id-out-of-range"),
        pytest.param({"id": True, "timestamp": _GOOD_TS}, "id", id="bool-id"),
        pytest.param({"id": 1, "timestamp": "not-a-timestamp"}, "timestamp", id="bad-timestamp"),
        pytest.param(
            {"id": 1, "timestamp": "\u0662026-01-01T00:00:00Z"}, "timestamp", id="non-ascii-digit"
        ),
        pytest.param(
            {"id": 1, "timestamp": "2026-01-01T00:00:00+99:99"},
            "timestamp",
            id="offset-out-of-range",
        ),
    ],
)
def test_record_to_model_raises_on_malformed(record: dict[str, object], field: str) -> None:
    with pytest.raises(StorageException, match=f"malformed transaction {field}"):
        TransactionHistory.record_to_model(record)


# --------------------------------------------------------------------------- #
# maybe_transaction integration (FS)
# --------------------------------------------------------------------------- #


def test_maybe_transaction_rejects_poisoned_max_int32_id(storage: StorageFS) -> None:
    """Poisoned id at int32 max must fail as StorageException, not an encoder ValueError."""
    seed_history(storage, [{"id": 2**31 - 1, "timestamp": "2026-01-01T00:00:00Z"}])
    with pytest.raises(StorageException, match="id space exhausted"):
        with maybe_transaction(storage):
            storage.put("pkg.deb", b"x")


def test_maybe_transaction_empty_commit_skips_history(storage: StorageFS) -> None:
    with maybe_transaction(storage):
        pass

    history = TransactionHistory(storage)
    assert history.load() == []


def test_maybe_transaction_appends_on_non_empty_commit(storage: StorageFS) -> None:
    with maybe_transaction(storage):
        storage.put("pkg.deb", b"payload")

    history = TransactionHistory(storage)
    assert [m.id for m in history.load()] == [1]
    assert list(json.loads(storage.get(HISTORY_FILENAME))[0]) == ["id", "timestamp"]
    assert storage.exists("pkg.deb")


def test_maybe_transaction_revert_via_history_clears_entry(storage: StorageFS) -> None:
    with maybe_transaction(storage):
        storage.put("pkg.deb", b"payload")

    history = TransactionHistory(storage)
    assert len(history.load()) == 1
    assert storage.exists("pkg.deb")

    removed = history.revert_last()
    assert removed is not None
    assert removed.id == 1

    assert history.load() == []
    assert not storage.exists("pkg.deb")


# --------------------------------------------------------------------------- #
# Concurrent writers (history_lock)
# --------------------------------------------------------------------------- #


def test_concurrent_maybe_transaction_preserves_both_entries(tmp_path: Path) -> None:
    """Two threads on the same FS root must both land in history with distinct ids."""

    repo = tmp_path / "repo"
    repo.mkdir()
    errors: list[BaseException] = []
    started = threading.Barrier(2)

    def worker(name: str) -> None:
        try:
            storage = StorageFS(str(repo))
            started.wait(timeout=10)
            with maybe_transaction(storage):
                # Hold the critical section briefly so the peer contends on the lock.
                time.sleep(0.05)
                storage.put(f"{name}.deb", name.encode())
        except BaseException as exc:  # collect for assertion
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("a",)),
        threading.Thread(target=worker, args=("b",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    assert errors == []
    storage = StorageFS(str(repo))
    records = TransactionHistory(storage).load()
    assert len(records) == 2
    assert {m.id for m in records} == {1, 2}
    assert storage.exists("a.deb")
    assert storage.exists("b.deb")


def test_revert_last_on_an_empty_history_returns_none(history: TransactionHistory) -> None:
    assert history.revert_last() is None
    assert history.load() == []


def test_revert_last_reloads_under_lock_after_concurrent_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit that finishes before revert acquires the lock must not be dropped.

    Simulates the lost-commit race: revert would have loaded ``[1]``, a peer
    appends ``2``, then revert must reload under the lock and revert ``2``
    (not save a stale ``[]`` that erases the peer's history entry).
    """
    from contextlib import contextmanager

    import txstore.history as history_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    storage = StorageFS(str(repo))

    with maybe_transaction(storage):
        storage.put("a.deb", b"a")

    real_lock = history_mod.history_lock
    injected = {"done": False}

    @contextmanager
    def lock_injecting_commit(st: StorageFS) -> Iterator[None]:
        if not injected["done"]:
            injected["done"] = True
            # Nested commit must use the real lock (avoid recurse into inject).
            monkeypatch.setattr(history_mod, "history_lock", real_lock)
            peer = StorageFS(str(repo))
            with maybe_transaction(peer):
                peer.put("b.deb", b"b")
        with real_lock(st):
            yield

    monkeypatch.setattr(history_mod, "history_lock", lock_injecting_commit)

    removed = TransactionHistory(storage).revert_last()
    assert removed is not None
    assert removed.id == 2

    history = TransactionHistory(storage)
    assert [m.id for m in history.load()] == [1]
    assert storage.exists("a.deb")
    assert not storage.exists("b.deb")


def test_concurrent_revert_last_serialises(tmp_path: Path) -> None:
    """Two parallel reverts must each undo a distinct transaction, not double-hit one."""

    repo = tmp_path / "repo"
    repo.mkdir()
    storage = StorageFS(str(repo))

    with maybe_transaction(storage):
        storage.put("a.deb", b"a")
    with maybe_transaction(storage):
        storage.put("b.deb", b"b")

    errors: list[BaseException] = []
    removed_ids: list[int] = []
    started = threading.Barrier(2)
    lock = threading.Lock()

    revert = StorageFS.transaction_revert

    def slow_revert(self: StorageFS, model: TransactionModel) -> None:
        # Inside the lock, so without it both workers would read the same last record.
        time.sleep(0.05)
        revert(self, model)

    def worker() -> None:
        try:
            local = StorageFS(str(repo))
            started.wait(timeout=10)
            removed = TransactionHistory(local).revert_last()
            with lock:
                if removed is not None:
                    removed_ids.append(removed.id)
        except BaseException as exc:  # collect for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    with patch.object(StorageFS, "transaction_revert", slow_revert):
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive()

    assert errors == []
    assert sorted(removed_ids) == [1, 2]
    assert TransactionHistory(storage).load() == []
    assert not storage.exists("a.deb")
    assert not storage.exists("b.deb")


def test_history_lock_released_after_failure(tmp_path: Path) -> None:
    """Exception inside maybe_transaction must still release the FS lock."""
    repo = tmp_path / "repo"
    repo.mkdir()
    storage = StorageFS(str(repo))

    with pytest.raises(RuntimeError, match="boom"):
        put_then_fail(storage)

    # A second writer must be able to acquire the lock immediately.
    with maybe_transaction(storage):
        storage.put("y.deb", b"y")

    assert [m.id for m in TransactionHistory(storage).load()] == [1]
    assert storage.exists("y.deb")
    assert not storage.exists("x.deb")


def test_a_transactional_backend_without_a_lock_cannot_be_constructed() -> None:
    """A backend that implements no `_obj_lock` must fail, never run unlocked."""

    class _Unlocked(MemoryTxnStorage):
        _obj_lock = FootmarkMixin._obj_lock

    with pytest.raises(TypeError, match="_obj_lock"):
        _Unlocked()


def test_a_transactional_backend_without_a_lock_identity_cannot_be_constructed() -> None:
    """Without an identity `history_lock` could not refuse the holding thread, so it must fail."""

    class _Unidentified(MemoryTxnStorage):
        _obj_lock_identity = FootmarkMixin._obj_lock_identity

    with pytest.raises(TypeError, match="_obj_lock_identity"):
        _Unidentified()


def test_history_lock_is_a_no_op_for_a_non_transactional_backend() -> None:
    with history_lock(StorageNull()):
        held = True
    assert held


def test_fs_history_lock_open_failure_is_mapped_to_storage_exception(tmp_path: Path) -> None:
    """A lock file that cannot be created fails as `StorageException`.

    `open` raises `OSError`, which callers do not handle, so an unwrapped one
    escaped every transactional FS write as a traceback.
    """
    repo = tmp_path / "repo"
    storage = StorageFS(str(repo))
    # A directory where the lock file belongs: `open` raises IsADirectoryError.
    (repo / LOCK_FILENAME).mkdir()

    with pytest.raises(StorageException, match="Failed to open history lock"):
        with maybe_transaction(storage):
            pass


def test_fs_history_lock_missing_root_is_mapped_to_storage_exception(tmp_path: Path) -> None:
    """The lock never creates its root, so a withheld `create_path` stays withheld."""
    repo = tmp_path / "missing"
    storage = StorageFS(str(repo), create_base=False)

    with pytest.raises(StorageException, match="Failed to open history lock"):
        with maybe_transaction(storage):
            pass

    assert not repo.exists()


def test_fs_history_lock_flock_failure_is_mapped_to_storage_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing `flock` fails in the Generic group and leaves no held lock."""

    storage = StorageFS(str(tmp_path / "repo"))

    def refuse(fd: int, op: int) -> None:
        raise OSError("flock unsupported on this filesystem")

    monkeypatch.setattr(fcntl, "flock", refuse)
    with pytest.raises(StorageException, match="Failed to acquire history lock"):
        with maybe_transaction(storage):
            pass

    # Unpatched again: the failed attempt left nothing locked.
    monkeypatch.undo()
    with maybe_transaction(storage):
        storage.put("z.deb", b"z")
    assert storage.exists("z.deb")


def test_concurrent_prune_waits_for_an_in_flight_transaction(tmp_path: Path) -> None:
    """`prune --orphans` must not take a peer's staged footmarks for residue: it waits its turn."""

    repo = tmp_path / "repo"
    repo.mkdir()
    errors: list[BaseException] = []
    in_flight = threading.Event()
    staged: list[float] = []

    def peer() -> None:
        try:
            storage = StorageFS(str(repo))
            with maybe_transaction(storage):
                storage.put("peer.deb", b"peer")
                staged.append(time.monotonic())
                in_flight.set()
                time.sleep(0.3)
        except BaseException as exc:  # collect for assertion
            errors.append(exc)
            in_flight.set()

    thread = threading.Thread(target=peer)
    thread.start()
    assert in_flight.wait(timeout=10)
    main = StorageFS(str(repo))
    result = TransactionHistory(main).prune(HistoryRetention(keep=1, max_age_days=90), orphans=True)
    finished = time.monotonic()
    thread.join(timeout=30)
    assert not thread.is_alive()

    assert errors == []
    # The prune could only start once the peer had released the lock after its sleep.
    assert finished - staged[0] >= 0.25
    assert result == ([], [], [])
    records = TransactionHistory(main).load()
    assert [m.id for m in records] == [1]
    assert _footmark_files(main, records[0])
    assert main.exists("peer.deb")


def _enter(lock: AbstractContextManager[None]) -> None:
    with lock:
        pass


# Every entry point that takes `history_lock`, called on the storage given.
_LOCK_TAKERS: dict[str, Callable[[StorageFS], object]] = {
    "maybe_transaction": lambda s: _enter(maybe_transaction(s)),
    "revert_last": lambda s: TransactionHistory(s).revert_last(),
    "pop_last": lambda s: TransactionHistory(s).pop_last(),
    "prune": lambda s: TransactionHistory(s).prune(HistoryRetention(keep=1, max_age_days=90)),
    "history_lock": lambda s: _enter(history_lock(s)),
}


@pytest.mark.parametrize("inner", list(_LOCK_TAKERS))
def test_a_history_lock_taken_inside_maybe_transaction_is_refused_not_awaited(
    storage: StorageFS, inner: str
) -> None:
    """The FS lock is not re-entrant: before the guard, every one of these waited forever."""
    with deadline(30), maybe_transaction(storage):
        storage.put("a.deb", b"a")
        with pytest.raises(StorageException, match="another transaction is active"):
            _LOCK_TAKERS[inner](storage)

    # The refusal left the outer transaction intact.
    assert [m.id for m in TransactionHistory(storage).load()] == [1]
    assert storage.exists("a.deb")


@pytest.mark.parametrize("alias", ["same-instance", "second-instance", "symlinked-root"])
@pytest.mark.parametrize("inner", list(_LOCK_TAKERS))
def test_a_root_locked_by_this_thread_is_refused_through_any_instance(
    tmp_path: Path, alias: str, inner: str
) -> None:
    """The holding thread is refused whichever instance of an `fs` root, or symlink to it, it uses.

    No transaction is active on the inner instance, so only the per-thread claim can refuse.
    """
    repo = tmp_path / "repo"
    outer = StorageFS(str(repo))
    (tmp_path / "link").symlink_to(repo)
    root = {"same-instance": repo, "second-instance": repo, "symlinked-root": tmp_path / "link"}
    nested = outer if alias == "same-instance" else StorageFS(str(root[alias]))

    with deadline(30):
        with history_lock(outer):
            with pytest.raises(StorageException, match="this thread already holds"):
                _LOCK_TAKERS[inner](nested)
        # The claim went with the lock: this thread may take it again.
        with maybe_transaction(nested):
            nested.put("b.deb", b"b")

    assert [m.id for m in TransactionHistory(outer).load()] == [1]


def test_a_root_locked_by_this_thread_is_refused_through_a_case_alias(tmp_path: Path) -> None:
    """On a case-insensitive volume another case of the root is the same root."""
    repo = tmp_path / "repo"
    outer = StorageFS(str(repo))
    if not (tmp_path / "REPO").exists():
        pytest.skip("case-sensitive volume: REPO is another root")

    with deadline(30), history_lock(outer):
        with pytest.raises(StorageException, match="this thread already holds"):
            _enter(history_lock(StorageFS(str(tmp_path / "REPO"))))


def test_one_thread_may_hold_the_locks_of_two_roots(tmp_path: Path) -> None:
    """The claim is per root: holding one root's lock leaves another's free to this thread."""
    first = StorageFS(str(tmp_path / "first"))
    second = StorageFS(str(tmp_path / "second"))

    with deadline(30), history_lock(first), maybe_transaction(second):
        second.put("c.deb", b"c")

    assert [m.id for m in TransactionHistory(second).load()] == [1]
    assert TransactionHistory(first).load() == []


@pytest.mark.parametrize("inner", list(_LOCK_TAKERS))
def test_every_lock_taker_is_refused_while_a_hand_begun_transaction_is_staged(
    storage: StorageFS, inner: str
) -> None:
    """A transaction begun by hand holds no lock, so only the active-transaction guard refuses.

    Without it `pop_last` dropped the recorded transaction while changes were staged.
    """
    with maybe_transaction(storage):
        storage.put("a.deb", b"a")
    storage.transaction_begin()
    storage.put("b.deb", b"b")

    with deadline(30), pytest.raises(StorageException, match="another transaction is active"):
        _LOCK_TAKERS[inner](storage)

    storage.transaction_discard()
    assert [m.id for m in TransactionHistory(storage).load()] == [1]
    assert not storage.exists("b.deb")


# --------------------------------------------------------------------------- #
# Retention: HistoryRetention.select_expired / prune
# --------------------------------------------------------------------------- #


def _seed_aged(storage: StorageFS, ages: dict[int, float]) -> None:
    """Seed one record per ``{id: age in days}`` entry, in id order."""
    seed_history(
        storage, [{"id": tid, "timestamp": iso_timestamp(age)} for tid, age in ages.items()]
    )


def _later(days: float) -> datetime:
    """Reference time *days* into the future, to age real transactions."""
    return datetime.now(UTC) + timedelta(days=days)


def test_select_expired_expires_only_records_past_max_age(
    storage: StorageFS, history: TransactionHistory
) -> None:
    _seed_aged(storage, {1: 400.0, 2: 100.0, 3: 10.0})
    retention = HistoryRetention(keep=1, max_age_days=90)

    expired = retention.select_expired(history.load())

    assert [m.id for m in expired] == [1, 2]


def test_select_expired_keeps_a_record_exactly_at_max_age() -> None:
    """Expiry is strictly older than the cutoff: the boundary instant stays, a hair older goes."""
    now = datetime.now(UTC)
    retention = HistoryRetention(keep=1, max_age_days=90)
    young = _model({"id": 2, "timestamp": iso_timestamp(1, now=now)})

    at_limit = _model({"id": 1, "timestamp": iso_timestamp(90, now=now)})
    assert retention.select_expired([at_limit, young], now=now) == []

    past_limit = _model({"id": 1, "timestamp": iso_timestamp(90 + 1e-5, now=now)})
    assert retention.select_expired([past_limit, young], now=now) == [past_limit]


def test_select_expired_keeps_the_newest_ids_as_a_floor(
    storage: StorageFS, history: TransactionHistory
) -> None:
    """Everything is old, so only the keep-count floor decides."""
    _seed_aged(storage, {1: 400.0, 2: 300.0, 3: 200.0, 4: 100.0})

    expired = HistoryRetention(keep=2, max_age_days=90).select_expired(history.load())

    assert [m.id for m in expired] == [1, 2]


def test_select_expired_protects_the_highest_id_regardless_of_file_order(
    storage: StorageFS, history: TransactionHistory
) -> None:
    """`next_id` is max(id)+1, so the floor must be by id, not by position."""
    seed_history(
        storage,
        [
            {"id": 9, "timestamp": iso_timestamp(400)},
            {"id": 3, "timestamp": iso_timestamp(390)},
            {"id": 5, "timestamp": iso_timestamp(380)},
        ],
    )
    records = history.load()

    expired = HistoryRetention(keep=1, max_age_days=90).select_expired(records)

    assert [m.id for m in expired] == [3, 5]


def test_select_expired_floor_counts_a_repeated_id_once(
    storage: StorageFS, history: TransactionHistory
) -> None:
    """`load` never demands unique ids, so a duplicate must not eat a keep slot."""
    seed_history(
        storage,
        [
            {"id": 5, "timestamp": iso_timestamp(400)},
            {"id": 5, "timestamp": iso_timestamp(390)},
            {"id": 4, "timestamp": iso_timestamp(380)},
        ],
    )

    expired = HistoryRetention(keep=2, max_age_days=90).select_expired(history.load())

    # Ids 5 and 4 are the two highest; nothing is left to expire.
    assert expired == []


def test_select_expired_disabled_by_zero_max_age(
    storage: StorageFS, history: TransactionHistory
) -> None:
    _seed_aged(storage, {1: 5_000.0, 2: 4_000.0})

    assert HistoryRetention(keep=1, max_age_days=0).select_expired(history.load()) == []


def test_select_expired_honours_an_explicit_now(
    storage: StorageFS, history: TransactionHistory
) -> None:
    _seed_aged(storage, {1: 100.0, 2: 10.0})
    records = history.load()
    retention = HistoryRetention(keep=1, max_age_days=90)

    # Rewinding "now" by a year makes every record younger than the cutoff.
    past = datetime.now(UTC) - timedelta(days=365)
    assert retention.select_expired(records, now=past) == []


def test_prune_rewrites_history_and_forgets_footmarks(storage: StorageFS) -> None:
    """The pruned transaction loses its footmarks; the survivor keeps them."""
    old_model, new_model = seed_old_and_new(storage)
    history = TransactionHistory(storage)

    result = history.prune(HistoryRetention(keep=1, max_age_days=90), now=_later(400))

    assert [m.id for m in result.records] == [old_model.id]
    assert result.orphans == []
    assert [m.id for m in history.load()] == [new_model.id]
    assert not _footmark_files(storage, old_model)
    assert _footmark_files(storage, new_model)
    # Pruning is not a revert: the committed object stays.
    assert storage.get("old.deb") == b"old"


def test_prune_keeps_next_id_monotonic(storage: StorageFS) -> None:
    history = TransactionHistory(storage)
    _seed_aged(storage, {1: 400.0, 2: 390.0, 3: 380.0})

    history.prune(HistoryRetention(keep=1, max_age_days=90))

    assert history.next_id() == 4


def test_prune_without_candidates_issues_no_listing(storage: StorageFS) -> None:
    """The common commit must pay nothing when nothing is expirable."""
    history = TransactionHistory(storage)
    _seed_aged(storage, {1: 1.0})

    with patch.object(storage, "_obj_iter_rel_all", side_effect=AssertionError("must not list")):
        assert history.prune(HistoryRetention(keep=1, max_age_days=90)) == ([], [], [])


def test_prune_dry_run_changes_nothing(storage: StorageFS) -> None:
    model, second = seed_old_and_new(storage)
    history = TransactionHistory(storage)

    result = history.prune(HistoryRetention(keep=1, max_age_days=90), now=_later(400), dry_run=True)

    assert [m.id for m in result.records] == [model.id]
    assert [m.id for m in history.load()] == [model.id, second.id]
    assert _footmark_files(storage, model)


def test_prune_rejects_a_non_transactional_backend() -> None:
    history = TransactionHistory(StorageNull())
    with pytest.raises(StorageException, match="does not support transaction retention"):
        history.prune(HistoryRetention(keep=1, max_age_days=90))


def test_revert_last_rejects_a_non_transactional_backend() -> None:
    with pytest.raises(StorageException, match="does not support transaction revert"):
        TransactionHistory(StorageNull()).revert_last()


def test_pop_last_rejects_a_non_transactional_backend() -> None:
    """Refused up front, so the answer never depends on what the history holds."""
    with pytest.raises(StorageException, match="does not record transaction history"):
        TransactionHistory(StorageNull()).pop_last()


def test_save_rejects_a_non_transactional_backend() -> None:
    with pytest.raises(StorageException, match="does not record transaction history"):
        TransactionHistory(StorageNull()).save([])


def test_prune_sweeps_orphans_left_by_a_crashed_transaction(storage: StorageFS) -> None:
    recorded = commit_transaction(storage, "kept.deb", b"kept")
    history = TransactionHistory(storage)
    seed_history(storage, [transaction_record(recorded)])

    crashed = storage.transaction_begin()
    storage.put("ghost.deb", b"lost")
    abandon_transaction(storage)

    result = history.prune(HistoryRetention(keep=1, max_age_days=90), orphans=True)

    assert result.records == []
    assert result.orphans
    assert not _footmark_files(storage, crashed)
    assert _footmark_files(storage, recorded)


def test_prune_without_orphans_leaves_residue_alone(storage: StorageFS) -> None:
    history = TransactionHistory(storage)
    _seed_aged(storage, {1: 1.0})
    crashed = storage.transaction_begin()
    storage.put("ghost.deb", b"lost")
    abandon_transaction(storage)

    assert history.prune(HistoryRetention(keep=1, max_age_days=90)).orphans == []
    assert _footmark_files(storage, crashed)


def test_prune_orphan_dry_run_reports_without_deleting(storage: StorageFS) -> None:
    history = TransactionHistory(storage)
    crashed = storage.transaction_begin()
    storage.put("ghost.deb", b"lost")
    abandon_transaction(storage)

    result = history.prune(HistoryRetention(keep=1, max_age_days=90), orphans=True, dry_run=True)

    assert result.orphans == sorted(result.orphans)
    assert result.orphans
    assert _footmark_files(storage, crashed)


def test_prune_orphan_sweep_spares_the_footmarks_of_a_pruned_record(storage: StorageFS) -> None:
    """A record and its footmarks go together: no double-reporting as orphans."""
    old_model, new_model = seed_old_and_new(storage)
    history = TransactionHistory(storage)

    result = history.prune(HistoryRetention(keep=1, max_age_days=90), now=_later(400), orphans=True)

    assert [m.id for m in result.records] == [old_model.id]
    assert result.orphans == []
    assert not _footmark_files(storage, old_model)
    assert _footmark_files(storage, new_model)


def test_prune_dry_run_with_orphans_matches_the_real_run(storage: StorageFS) -> None:
    """The preview predicts the real run: expiring footmarks are not orphans."""
    old_model, new_model = seed_old_and_new(storage)
    history = TransactionHistory(storage)
    retention = HistoryRetention(keep=1, max_age_days=90)

    preview = history.prune(retention, now=_later(400), orphans=True, dry_run=True)
    assert _footmark_files(storage, old_model)
    result = history.prune(retention, now=_later(400), orphans=True)

    assert preview == result
    assert [m.id for m in result.records] == [old_model.id]
    assert result.orphans == []
    assert not _footmark_files(storage, old_model)
    assert _footmark_files(storage, new_model)


def test_prune_keeps_a_young_record_sharing_an_expired_record_id(storage: StorageFS) -> None:
    """Expiry is per record, so a duplicate id must not drag a survivor out."""
    history = TransactionHistory(storage)
    seed_history(
        storage,
        [
            {"id": 1, "timestamp": iso_timestamp(400)},
            {"id": 1, "timestamp": iso_timestamp(1)},
            {"id": 5, "timestamp": iso_timestamp(400)},
        ],
    )

    result = history.prune(HistoryRetention(keep=1, max_age_days=90))

    assert len(result.records) == 1
    survivors = history.load()
    assert [m.id for m in survivors] == [1, 5]
    # The survivor kept is the young one, not the expired namesake.
    assert survivors[0].timestamp != result.records[0].timestamp


def test_prune_drops_every_copy_of_a_duplicated_expired_record(storage: StorageFS) -> None:
    history = TransactionHistory(storage)
    old, young = iso_timestamp(400), iso_timestamp(1)
    seed_history(
        storage,
        [
            {"id": 1, "timestamp": old},
            {"id": 1, "timestamp": old},
            {"id": 1, "timestamp": young},
            {"id": 5, "timestamp": old},
        ],
    )
    loaded = history.load()

    result = history.prune(HistoryRetention(keep=1, max_age_days=90))

    assert result.records == loaded[:2]
    assert history.load() == loaded[2:]


def test_prune_drops_records_equal_to_the_expired_ones_not_only_the_same_objects(
    storage: StorageFS,
) -> None:
    """The rewrite must not depend on `select_expired` handing back the loaded objects."""
    _, new_model = seed_old_and_new(storage)
    history = TransactionHistory(storage)
    select = HistoryRetention.select_expired

    def select_copies(
        policy: HistoryRetention, records: Sequence[TransactionModel], now: datetime | None = None
    ) -> list[TransactionModel]:
        return [dataclasses.replace(m) for m in select(policy, records, now)]

    with patch.object(HistoryRetention, "select_expired", select_copies):
        history.prune(HistoryRetention(keep=1, max_age_days=90), now=_later(400))

    assert history.load() == [new_model]


def test_prune_of_a_malformed_history_fails_before_touching_anything(storage: StorageFS) -> None:
    """A record that is no real instant fails the load: the real run fails as the dry run does."""
    old_model = commit_transaction(storage, "old.deb", b"old")
    history = TransactionHistory(storage)
    # The regex `load` applies admits month 13; `record_to_model` does not.
    seed_history(
        storage, [transaction_record(old_model), {"id": 9, "timestamp": "2026-13-45T99:99:99Z"}]
    )
    retention = HistoryRetention(keep=1, max_age_days=90)
    doomed = _footmark_files(storage, old_model)
    assert doomed

    for dry_run in (True, False):
        with pytest.raises(StorageException, match="malformed transaction timestamp"):
            history.prune(retention, now=_later(400), orphans=True, dry_run=dry_run)
        assert _footmark_files(storage, old_model) == doomed
        assert [r["id"] for r in json.loads(storage.get(HISTORY_FILENAME))] == [old_model.id, 9]


def test_prune_reports_the_footmark_paths_it_removed(storage: StorageFS) -> None:
    old_model, new_model = seed_old_and_new(storage)
    history = TransactionHistory(storage)
    expected = _footmark_files(storage, old_model)
    assert expected

    result = history.prune(HistoryRetention(keep=1, max_age_days=90), now=_later(400))

    assert result.footmark_paths == expected
    assert not _footmark_files(storage, old_model)
    assert _footmark_files(storage, new_model)


def test_prune_warns_when_a_footmark_delete_fails(
    storage: StorageFS, caplog: pytest.LogCaptureFixture
) -> None:
    """A stubborn footmark stays out of the result and is left to the orphan sweep."""
    old_model, new_model = seed_old_and_new(storage)
    history = TransactionHistory(storage)
    doomed = _footmark_files(storage, old_model)
    stubborn = doomed[0]
    real_delete = storage._reclaim_footmark

    def flaky_delete(rel: str) -> None:
        if rel == stubborn:
            raise OSError("busy")
        real_delete(rel)

    with (
        caplog.at_level(logging.WARNING),
        patch.object(storage, "_reclaim_footmark", side_effect=flaky_delete),
    ):
        result = history.prune(HistoryRetention(keep=1, max_age_days=90), now=_later(400))

    assert result.footmark_paths == doomed[1:]
    assert "resisted removal" in caplog.text
    assert [m.id for m in history.load()] == [new_model.id]
    preview = history.prune(HistoryRetention(keep=1, max_age_days=90), orphans=True, dry_run=True)
    assert preview.orphans == [stubborn]


def test_prune_with_orphans_lists_once(
    storage: StorageFS, caplog: pytest.LogCaptureFixture
) -> None:
    """The reclaim and the orphan sweep share one walk; the sweep never retries the reclaim."""
    old_model, new_model = seed_old_and_new(storage)
    history = TransactionHistory(storage)
    walk = storage._obj_iter_rel_all

    with (
        caplog.at_level(logging.WARNING),
        patch.object(storage, "_obj_iter_rel_all", side_effect=walk) as listing,
    ):
        result = history.prune(
            HistoryRetention(keep=1, max_age_days=90), now=_later(400), orphans=True
        )

    assert listing.call_count == 1
    assert [m.id for m in result.records] == [old_model.id]
    assert result.orphans == []
    assert "Failed to remove" not in caplog.text


def test_retention_from_env_falls_back_to_defaults() -> None:
    assert HistoryRetention.from_env() == HistoryRetention(keep=20, max_age_days=90)


def test_retention_from_env_reads_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TX_STORE_HISTORY_KEEP", "3")
    monkeypatch.setenv("TX_STORE_HISTORY_MAX_AGE_DAYS", "7")
    retention = HistoryRetention.from_env()
    assert (retention.keep, retention.max_age_days) == (3, 7)
    assert retention.enabled


def test_retention_from_env_arguments_override_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TX_STORE_HISTORY_KEEP", "3")
    monkeypatch.setenv("TX_STORE_HISTORY_MAX_AGE_DAYS", "7")
    retention = HistoryRetention.from_env(keep=5, max_age_days=0)
    assert (retention.keep, retention.max_age_days) == (5, 0)
    assert not retention.enabled


def test_retention_from_env_arguments_skip_invalid_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TX_STORE_HISTORY_KEEP", "many")
    monkeypatch.setenv("TX_STORE_HISTORY_MAX_AGE_DAYS", "old")
    assert HistoryRetention.from_env(keep=5, max_age_days=0) == HistoryRetention(
        keep=5, max_age_days=0
    )


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        pytest.param("TX_STORE_HISTORY_KEEP", "many", "expected an integer", id="keep-text"),
        pytest.param("TX_STORE_HISTORY_KEEP", "0", "keep must be", id="keep-zero"),
        pytest.param("TX_STORE_HISTORY_MAX_AGE_DAYS", "-1", "max age must be", id="age-negative"),
    ],
)
def test_retention_from_env_rejects_invalid_variables(
    name: str, value: str, match: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=match):
        HistoryRetention.from_env()


@pytest.mark.parametrize("age", [0, 36_500], ids=["age-zero", "age-max"])
def test_retention_accepts_the_age_bounds(age: int) -> None:
    assert HistoryRetention(max_age_days=age).max_age_days == age


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"keep": 0}, id="keep-zero"),
        pytest.param({"keep": True}, id="keep-bool"),
        pytest.param({"max_age_days": -1}, id="age-negative"),
        pytest.param({"max_age_days": 1.5}, id="age-float"),
        pytest.param({"max_age_days": 36_501}, id="age-too-large"),
    ],
)
def test_retention_rejects_out_of_range_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        HistoryRetention(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Retention inside maybe_transaction
# --------------------------------------------------------------------------- #


def test_maybe_transaction_prunes_after_a_recorded_commit(storage: StorageFS) -> None:
    history = TransactionHistory(storage)
    _seed_aged(storage, {1: 400.0, 2: 390.0})

    with maybe_transaction(storage, HistoryRetention(keep=1, max_age_days=90)):
        storage.put("pkg.deb", b"payload")

    # The just-committed record holds the highest id, so it is always protected.
    assert [m.id for m in history.load()] == [3]


def test_maybe_transaction_without_retention_keeps_everything(storage: StorageFS) -> None:
    history = TransactionHistory(storage)
    _seed_aged(storage, {1: 400.0, 2: 390.0})

    with maybe_transaction(storage):
        storage.put("pkg.deb", b"payload")

    assert [m.id for m in history.load()] == [1, 2, 3]


def test_maybe_transaction_never_sweeps_orphans(storage: StorageFS) -> None:
    """Auto-prune must not touch residue; that is an explicit prune's job."""
    crashed = storage.transaction_begin()
    storage.put("ghost.deb", b"lost")
    abandon_transaction(storage)

    with maybe_transaction(storage, HistoryRetention(keep=1, max_age_days=90)):
        storage.put("pkg.deb", b"payload")

    assert _footmark_files(storage, crashed)


@pytest.mark.parametrize(
    "failure",
    [StorageException("listing unavailable"), OSError(errno.EIO, "io"), RuntimeError("bug")],
    ids=["storage", "os", "other"],
)
def test_maybe_transaction_survives_a_failing_prune(
    storage: StorageFS, caplog: pytest.LogCaptureFixture, failure: Exception
) -> None:
    """The commit is already durable, so no failed reclaim, of any type, fails the write."""
    history = TransactionHistory(storage)
    _seed_aged(storage, {1: 400.0})

    with (
        caplog.at_level(logging.WARNING),
        patch.object(TransactionHistory, "_prune_locked", side_effect=failure),
    ):
        with maybe_transaction(storage, HistoryRetention(keep=1, max_age_days=90)):
            storage.put("pkg.deb", b"payload")

    assert storage.exists("pkg.deb")
    assert [m.id for m in history.load()] == [1, 2]
    assert "Failed to prune transaction history" in caplog.text


def test_maybe_transaction_reclaims_footmarks_of_expired_records(storage: StorageFS) -> None:
    """End to end: an aged transaction's footmarks go with its record."""
    old_ts = Timestamp.from_json(iso_timestamp(400))

    # Freeze the begin timestamp so the record is genuinely old and its tokens
    # still regenerate from what was stored.
    with patch("txstore.transaction.now_timestamp", return_value=old_ts):
        with maybe_transaction(storage):
            storage.put("old.deb", b"old")

    history = TransactionHistory(storage)
    old_model = history.load()[0]
    assert _footmark_files(storage, old_model)

    with maybe_transaction(storage, HistoryRetention(keep=1, max_age_days=90)):
        storage.put("new.deb", b"new")

    assert [m.id for m in history.load()] == [2]
    assert not _footmark_files(storage, old_model)
    assert storage.get("old.deb") == b"old"


def test_maybe_transaction_prune_reuses_the_commit_listing(storage: StorageFS) -> None:
    """Auto-prune walks nothing of its own: the commit's listing is reused."""
    old_ts = Timestamp.from_json(iso_timestamp(400))
    with patch("txstore.transaction.now_timestamp", return_value=old_ts):
        with maybe_transaction(storage):
            storage.put("old.deb", b"old")
    history = TransactionHistory(storage)
    old_model = history.load()[0]
    walk = storage._obj_iter_rel_all

    with patch.object(storage, "_obj_iter_rel_all", side_effect=walk) as listing:
        with maybe_transaction(storage, HistoryRetention(keep=1, max_age_days=90)):
            storage.put("new.deb", b"new")

    assert listing.call_count == 1
    assert [m.id for m in history.load()] == [2]
    assert not _footmark_files(storage, old_model)
