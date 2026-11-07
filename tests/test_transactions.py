"""Transaction lifecycle shared by every `FootmarkMixin` backend (STORAGE.md#transactions).

Each test runs on the in-memory backend, `StorageFS`, and `StorageS3`. A ``peer`` is a second
instance on the same root with no transaction of its own: what another process sees.
Backend-specific behavior lives in `test_fs_transactions.py` / `test_s3_transactions.py`.
"""

from __future__ import annotations

import inspect
import io
import logging
import re
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from helpers.backends import skip_without_user_xattrs
from helpers.s3 import s3_storage
from helpers.storage import (
    HISTORY_FILENAME,
    LOCK_FILENAME,
    FoldingMemoryTxnStorage,
    MemoryRoot,
    MemoryTxnStorage,
    abandon_transaction,
    commit_transaction,
    footmarks_of,
)

from txstore import (
    ObjectOperation,
    Storage,
    StorageException,
    StorageFS,
    StorageNotFoundException,
    StorageS3,
    Timestamp,
    TransactionHistory,
    TransactionModel,
    maybe_transaction,
)

TxnStorage = MemoryTxnStorage | StorageFS | StorageS3

# Plain keys: the fs backend stores `k` as the xattr `user.k` itself.
KEY = "k"
OTHER = "j"


@pytest.fixture(params=["memory", "fs", "s3"])
def make(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Callable[[], TxnStorage]]:
    """Factory of instances on one fresh storage root of the parametrized backend."""
    if request.param == "memory":
        root = MemoryRoot()
        yield lambda: MemoryTxnStorage(root)
    elif request.param == "fs":
        base = tmp_path / "repo"
        base.mkdir()
        yield lambda: StorageFS(str(base))
    else:
        endpoint = request.getfixturevalue("moto_server")
        bucket = f"test-txn-{uuid.uuid4().hex[:12]}"

        def s3(create: bool = False) -> StorageS3:
            return s3_storage(endpoint, bucket, create_bucket=create)

        owner = s3(create=True)
        yield s3
        owner.delete_bucket()


@pytest.fixture
def storage(make: Callable[[], TxnStorage]) -> TxnStorage:
    return make()


@pytest.fixture
def peer(make: Callable[[], TxnStorage]) -> TxnStorage:
    return make()


@pytest.fixture
def with_metadata(storage: TxnStorage) -> TxnStorage:
    """`storage`, skipped where the filesystem has no user xattrs."""
    if isinstance(storage, StorageFS):
        skip_without_user_xattrs(Path(storage.base_dir))
    return storage


def _fail_on_call(
    real: Callable[..., None], failing_call: int, exc: Exception
) -> Callable[..., None]:
    """Wrap a primitive so its *failing_call*-th call raises *exc*; the others run *real*."""
    calls = 0

    def wrapper(*args: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == failing_call:
            raise exc
        real(*args)

    return wrapper


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def test_is_transaction_false_before_begin(storage: TxnStorage) -> None:
    assert not storage.in_transaction()


def test_transaction_begin_returns_one_token_per_operation(storage: TxnStorage) -> None:
    model = storage.transaction_begin()

    assert storage.in_transaction()
    assert model.id == 1
    assert len(model.footmarks) == len(ObjectOperation)


def test_transaction_begin_auto_increments_the_id(storage: TxnStorage) -> None:
    storage.transaction_begin()
    storage.transaction_commit()
    assert storage.transaction_begin().id == 2


def test_transaction_begin_takes_an_explicit_id(storage: TxnStorage) -> None:
    assert storage.transaction_begin(transaction_id=41).id == 41


@pytest.mark.parametrize(
    "explicit",
    [2**31, -1, 0, True, False],
    ids=["explicit-over", "explicit-neg", "auto", "bool-true", "bool-false"],
)
def test_transaction_begin_refuses_an_id_outside_int32(storage: TxnStorage, explicit: int) -> None:
    """Explicit or auto-incremented past `2^31-1`: refused, with nothing begun or advanced."""
    storage.transaction_id = 2**31 - 1
    with pytest.raises(ValueError, match="Transaction id out of range"):
        storage.transaction_begin(transaction_id=explicit)
    assert not storage.in_transaction()
    assert storage.transaction_id == 2**31 - 1


def test_transaction_begin_while_active_raises(storage: TxnStorage) -> None:
    storage.transaction_begin()
    with pytest.raises(StorageException, match="another transaction is active"):
        storage.transaction_begin()


def test_transaction_commit_without_active_raises(storage: TxnStorage) -> None:
    with pytest.raises(StorageException, match="no active transaction"):
        storage.transaction_commit()


def test_transaction_discard_without_active_raises(storage: TxnStorage) -> None:
    with pytest.raises(StorageException, match="no active transaction"):
        storage.transaction_discard()


def test_transaction_revert_while_active_raises(storage: TxnStorage) -> None:
    model = storage.transaction_begin()
    with pytest.raises(StorageException, match="another transaction is active"):
        storage.transaction_revert(model)


def test_transaction_commit_of_no_operation_reports_no_changes(storage: TxnStorage) -> None:
    storage.put("pre.txt", b"existing")
    storage.transaction_begin()

    assert storage.transaction_commit() is False
    assert storage.get("pre.txt") == b"existing"
    assert storage.hidden_objects() == []


def test_transaction_commit_of_a_put_reports_changes(storage: TxnStorage) -> None:
    storage.transaction_begin()
    storage.put("a.txt", b"v")
    assert storage.transaction_commit() is True


@pytest.mark.parametrize(
    ("existing", "change"),
    [
        pytest.param(None, lambda s: s.put("pool/pkg.deb", b"payload"), id="put"),
        pytest.param("pool/pkg.deb", lambda s: s.delete("pool/pkg.deb"), id="delete"),
        pytest.param(None, lambda s: s.put("pool/.env", b"payload"), id="dotfile-put"),
        pytest.param("pool/.env", lambda s: s.delete("pool/.env"), id="dotfile-delete"),
    ],
)
def test_commit_returns_a_listing_exact_for_its_footmarks(
    storage: TxnStorage, existing: str | None, change: Callable[[TxnStorage], None]
) -> None:
    """The prune reuses this listing, so every footmark classifier must see what is there."""
    if existing:
        storage.put(existing, b"orig")
    model = storage.transaction_begin()
    change(storage)

    committed, listing = storage._commit_listed()

    assert committed is True
    after = storage.hidden_objects()
    found = storage.find_transaction_footmarks([model], listing=listing)
    assert found == storage.find_transaction_footmarks([model], listing=after)
    assert storage.find_orphaned_footmarks([model.footmarks], listing=listing) == (
        storage.find_orphaned_footmarks([model.footmarks], listing=after)
    )


# --------------------------------------------------------------------------- #
# Overlay
# --------------------------------------------------------------------------- #


def test_put_in_transaction_is_invisible_to_a_peer_until_commit(
    storage: TxnStorage, peer: TxnStorage
) -> None:
    storage.transaction_begin()
    storage.put("pool/f.txt", b"v")

    assert (storage.exists("pool/f.txt"), storage.get("pool/f.txt")) == (True, b"v")
    assert "pool/f.txt" in storage.list()
    assert not peer.exists("pool/f.txt")

    storage.transaction_commit()
    assert peer.get("pool/f.txt") == b"v"


def test_delete_in_transaction_is_invisible_to_a_peer_until_commit(
    storage: TxnStorage, peer: TxnStorage
) -> None:
    storage.put("f.txt", b"v")
    storage.transaction_begin()
    storage.delete("f.txt")

    assert not storage.exists("f.txt")
    assert "f.txt" not in storage.list()
    assert peer.get("f.txt") == b"v"

    storage.transaction_commit()
    assert not peer.exists("f.txt")


@pytest.mark.parametrize(
    ("existing", "path"),
    [
        pytest.param(None, "pool/.env", id="new-dotfile"),
        pytest.param(".env", ".env", id="overwritten-dotfile"),
        pytest.param(None, ".dir/f.txt", id="under-a-dot-directory"),
    ],
)
def test_list_in_transaction_shows_what_commit_will_list(
    storage: TxnStorage, existing: str | None, path: str
) -> None:
    """A staged hidden name stays unlisted, as after commit (STORAGE.md#storage-abi)."""
    storage.put("a.txt", b"a")
    if existing:
        storage.put(existing, b"old")
    storage.transaction_begin()
    storage.put(path, b"new")

    inside = sorted(storage.list())
    storage.transaction_commit()

    assert inside == sorted(storage.list())


@pytest.mark.parametrize(
    ("existing", "stage"),
    [
        pytest.param(True, lambda s: None, id="original"),
        pytest.param(False, lambda s: None, id="missing"),
        pytest.param(True, lambda s: s.put("f.txt", b"new"), id="put"),
        pytest.param(True, lambda s: s.delete("f.txt"), id="delete"),
        pytest.param(True, lambda s: (s.delete("f.txt"), s.put("f.txt", b"new")), id="delete-put"),
        pytest.param(True, lambda s: (s.put("f.txt", b"new"), s.delete("f.txt")), id="put-delete"),
    ],
)
def test_overlay_reads_agree_on_a_staged_path(
    storage: TxnStorage, existing: bool, stage: Callable[[TxnStorage], object]
) -> None:
    """`exists`, `list`, `get`, and `get_metadata` see one staged state (STORAGE.md#footmarks)."""
    if existing:
        storage.put("f.txt", b"old")
    storage.transaction_begin()
    stage(storage)

    exists = storage.exists("f.txt")
    assert exists == ("f.txt" in storage.list())
    for read in (storage.get, storage.get_metadata):
        if exists:
            read("f.txt")
        else:
            with pytest.raises(StorageException):
                read("f.txt")


def _original(s: TxnStorage) -> str:
    return s._obj_full("f.txt")


def _staged_put(s: TxnStorage) -> str:
    return s._add_footmark(s._obj_full("new.txt"), ObjectOperation.PUT)


@pytest.mark.parametrize(
    ("operation", "probed"),
    [
        pytest.param(lambda s: s.delete("f.txt"), _original, id="delete-original"),
        pytest.param(lambda s: s.add_metadata("f.txt", {"k": "v"}), _original, id="metadata"),
        pytest.param(lambda s: s.copy("new.txt", "c.txt"), _staged_put, id="copy-staged-source"),
    ],
)
def test_one_operation_probes_a_path_once(
    storage: TxnStorage,
    operation: Callable[[TxnStorage], object],
    probed: Callable[[TxnStorage], str],
) -> None:
    """On S3 every probe is a HEAD request, so an operation reuses what it already learned."""
    storage.put("f.txt", b"v")
    storage.transaction_begin()
    storage.put("new.txt", b"n")
    with patch.object(storage, "_obj_exists", wraps=storage._obj_exists) as exists:
        operation(storage)
    assert [c.args[0] for c in exists.call_args_list].count(probed(storage)) == 1


def test_transaction_discard_drops_every_footmark(storage: TxnStorage, peer: TxnStorage) -> None:
    storage.put("old.txt", b"v")
    storage.transaction_begin()
    storage.put("new.txt", b"v")
    storage.put("old.txt", b"changed")
    storage.transaction_discard()

    assert not storage.in_transaction()
    assert not peer.exists("new.txt")
    assert peer.get("old.txt") == b"v"
    assert storage.hidden_objects() == []


@pytest.mark.parametrize(
    ("operation", "counted"),
    [
        ("get", 1),
        ("get_stream", 1),
        ("get_metadata", 0),
        ("copy", 0),
        ("move", 0),
        ("move-onto-itself", 0),
    ],
)
def test_use_of_a_delete_marked_source_raises_and_counts_only_reads(
    storage: TxnStorage, operation: str, counted: int
) -> None:
    """As outside a transaction: `get` / `get_stream` count a read error, the others none."""
    storage.put("f.txt", b"data")
    storage.transaction_begin()
    storage.delete("f.txt")
    calls: dict[str, Callable[[], object]] = {
        "get": lambda: storage.get("f.txt"),
        "get_stream": lambda: storage.get_stream("f.txt"),
        "get_metadata": lambda: storage.get_metadata("f.txt"),
        "copy": lambda: storage.copy("f.txt", "g.txt"),
        "move": lambda: storage.move("f.txt", "g.txt"),
        "move-onto-itself": lambda: storage.move("f.txt", "f.txt"),
    }

    errors_before = storage.metrics.read_errors_total
    with pytest.raises(StorageException, match="marked for deletion"):
        calls[operation]()
    assert storage.metrics.read_errors_total == errors_before + counted


def test_put_then_delete_in_one_transaction_commits_to_absent(
    storage: TxnStorage, peer: TxnStorage
) -> None:
    storage.transaction_begin()
    storage.put("doomed.txt", b"v")
    storage.delete("doomed.txt")
    assert not storage.exists("doomed.txt")
    storage.transaction_commit()

    assert not peer.exists("doomed.txt")
    assert storage.hidden_objects() == []


def test_modify_then_delete_in_one_transaction_commits_to_absent(
    storage: TxnStorage, peer: TxnStorage
) -> None:
    storage.put("keep.txt", b"orig")
    storage.transaction_begin()
    storage.put("keep.txt", b"changed")
    storage.delete("keep.txt")
    storage.transaction_commit()

    assert not peer.exists("keep.txt")


@pytest.mark.parametrize("recreate", ["put", "copy", "move"])
def test_delete_then_recreate_in_one_transaction_commits_the_new_object(
    storage: TxnStorage, peer: TxnStorage, recreate: str
) -> None:
    storage.put("dst.txt", b"old")
    storage.put("src.txt", b"new")
    storage.transaction_begin()
    storage.delete("dst.txt")
    if recreate == "put":
        storage.put("dst.txt", b"new")
    else:
        getattr(storage, recreate)("src.txt", "dst.txt")

    assert storage.get("dst.txt") == b"new"
    storage.transaction_commit()

    assert peer.get("dst.txt") == b"new"
    assert peer.exists("src.txt") is (recreate != "move")


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
def test_move_onto_itself_keeps_the_object(
    storage: TxnStorage, peer: TxnStorage, in_transaction: bool
) -> None:
    """Copy-then-delete onto one key would stage a DELETE over the copy and lose it."""
    storage.put("f.txt", b"data")
    if in_transaction:
        storage.transaction_begin()
    deletes_before = storage.metrics.delete_objects_total
    storage.move("f.txt", "f.txt")
    assert storage.metrics.delete_objects_total == deletes_before
    if in_transaction:
        storage.transaction_commit()

    assert peer.get("f.txt") == b"data"


def test_recorded_transaction_counts_only_its_public_write(storage: TxnStorage) -> None:
    """History reads and writes are not user I/O → STORAGE.md#storage-abi."""
    for _ in range(2):  # the first finds no history, the second reads and rewrites it
        before = storage.metrics.dump()
        with maybe_transaction(storage):
            storage.put("a.txt", b"x")
        after = storage.metrics.dump()
        delta = {name: after[name] - before[name] for name in after}
        assert delta == {**dict.fromkeys(after, 0), "write_objects_total": 1, "write_bytes": 1}


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
def test_move_counts_one_delete(
    storage: TxnStorage, peer: TxnStorage, in_transaction: bool
) -> None:
    """Whether the backend renamed or copied then deleted → metrics.py."""
    storage.put("src.txt", b"data")
    if in_transaction:
        storage.transaction_begin()
    deletes_before = storage.metrics.delete_objects_total
    storage.move("src.txt", "dst.txt")
    assert storage.metrics.delete_objects_total == deletes_before + 1
    if in_transaction:
        storage.transaction_commit()

    assert peer.get("dst.txt") == b"data"
    assert not peer.exists("src.txt")


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
@pytest.mark.parametrize(
    "dst",
    ["absent.txt", "other.txt", "file.txt/sub/other.txt"],
    ids=["onto-itself", "elsewhere", "under-a-file"],
)
@pytest.mark.parametrize("verb", ["copy", "move"])
def test_copy_or_move_of_a_missing_source_raises_not_found(
    storage: TxnStorage, verb: str, dst: str, in_transaction: bool
) -> None:
    """As for `get` and `delete`, whichever the destination → STORAGE.md#storage-abi."""
    storage.put("file.txt", b"f")
    if in_transaction:
        storage.transaction_begin()
    with pytest.raises(StorageNotFoundException):
        getattr(storage, verb)("absent.txt", dst)
    assert not storage.exists(dst)


class _FailingStream(io.BytesIO):
    """A stream whose read fails, so a `put` fails after its write was staged."""

    def read(self, size: int | None = -1, /) -> bytes:
        raise StorageException("stream broke")


# Failure → (patched primitive, its error, expected message); every verb stages through
# `_stage_write`, so each failure reaches each of them.
_STAGED_DELETE_FAILURES: dict[str, tuple[str, Exception, str | None]] = {
    "write-fails": ("_obj_copy", OSError("disk gone"), None),
    "marker-drop-fails": ("_obj_delete", OSError("gone"), "drop DELETE marker"),
    "backup-fails": ("_backup", StorageException("probe failed"), "probe failed"),
    "parent-fails": ("_obj_prepare_parent", OSError("gone"), "create parent"),
}


@pytest.mark.parametrize("verb", ["put", "copy", "move"])
@pytest.mark.parametrize("failure", list(_STAGED_DELETE_FAILURES))
def test_failed_write_onto_a_staged_delete_keeps_the_delete(
    storage: TxnStorage, peer: TxnStorage, failure: str, verb: str
) -> None:
    """A staging step or the write failing keeps the delete: the marker is dropped after every
    staging step and restored when the write fails, so commit never removes the new object."""
    target, error, match = _STAGED_DELETE_FAILURES[failure]
    storage.put("src.txt", b"src")
    storage.put("dst.txt", b"old")
    storage.transaction_begin()
    storage.delete("dst.txt")
    calls: dict[str, Callable[[], object]] = {
        "put": lambda: storage.put_stream("dst.txt", _FailingStream()),
        "copy": lambda: storage.copy("src.txt", "dst.txt"),
        "move": lambda: storage.move("src.txt", "dst.txt"),
    }
    with (
        patch.object(storage, target, side_effect=error),
        pytest.raises(StorageException, match=match),
    ):
        calls[verb]()
    assert not storage.exists("dst.txt")
    storage.transaction_commit()

    assert not peer.exists("dst.txt")
    assert peer.get("src.txt") == b"src"


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
def test_copy_of_a_missing_source_onto_a_reserved_path_is_refused_as_reserved(
    storage: TxnStorage, in_transaction: bool
) -> None:
    """The reserved refusal comes before the source check, as for `move` → STORAGE.md."""
    if in_transaction:
        storage.transaction_begin()
    with pytest.raises(StorageException, match="reserved") as raised:
        storage.copy("absent.txt", HISTORY_FILENAME)
    assert not isinstance(raised.value, StorageNotFoundException)


def test_failed_write_whose_marker_cannot_be_restored_warns_and_loses_the_delete(
    storage: TxnStorage, peer: TxnStorage, caplog: pytest.LogCaptureFixture
) -> None:
    """The double fault STORAGE.md#footmarks names: the write's error wins, the delete is lost."""
    storage.put("dst.txt", b"old")
    storage.transaction_begin()
    storage.delete("dst.txt")

    with (
        patch.object(storage, "_obj_touch", side_effect=OSError("disk gone")),
        caplog.at_level(logging.WARNING),
        pytest.raises(StorageException, match="stream broke"),
    ):
        storage.put_stream("dst.txt", _FailingStream())
    storage.transaction_commit()

    assert "Failed to restore DELETE marker" in caplog.text
    assert peer.get("dst.txt") == b"old"


def test_commit_refuses_a_restored_delete_marker_gone(
    storage: TxnStorage, peer: TxnStorage
) -> None:
    """A marker a failed write restored is staged again, so its loss is refused, not skipped."""
    storage.put("dst.txt", b"old")
    storage.transaction_begin()
    storage.delete("dst.txt")
    with pytest.raises(StorageException, match="stream broke"):
        storage.put_stream("dst.txt", _FailingStream())
    gone = _footmark(storage, "dst.txt", ObjectOperation.DELETE)
    storage._obj_delete(gone)

    with pytest.raises(StorageException, match=_gone(gone)):
        storage.transaction_commit()

    assert storage.in_transaction()
    assert peer.get("dst.txt") == b"old"


def test_delete_twice_in_one_transaction_stays_deleted(storage: TxnStorage) -> None:
    storage.put("d.txt", b"data")
    storage.transaction_begin()
    storage.delete("d.txt")
    storage.delete("d.txt")
    storage.transaction_commit()

    assert not storage.exists("d.txt")


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
def test_delete_of_a_missing_object_raises_not_found(
    storage: TxnStorage, in_transaction: bool
) -> None:
    if in_transaction:
        storage.transaction_begin()
    with pytest.raises(StorageNotFoundException, match="non-existent"):
        storage.delete("missing.txt")
    assert storage.hidden_objects() == []


@pytest.mark.parametrize("path", [HISTORY_FILENAME, LOCK_FILENAME])
def test_put_of_a_reserved_path_raises(storage: TxnStorage, path: str) -> None:
    with pytest.raises(StorageException, match="reserved storage path"):
        storage.put(path, b"[]")


_RESERVED_MUTATIONS: dict[str, Callable[[TxnStorage, str], None]] = {
    "copy-destination": lambda s, name: s.copy("pool/x", name),
    "move-source": lambda s, name: s.move(name, "pool/h"),
    "move-destination": lambda s, name: s.move("pool/x", name),
    "delete": lambda s, name: s.delete(name),
    "add_metadata": lambda s, name: s.add_metadata(name, {KEY: "v"}),
    "delete_metadata": lambda s, name: s.delete_metadata(name),
    "delete_metadata_fields": lambda s, name: s.delete_metadata_fields(name, [KEY]),
}


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "in-transaction"])
@pytest.mark.parametrize(
    "name",
    [HISTORY_FILENAME, LOCK_FILENAME, ".Transactions.Lock"],
    ids=["history", "lock", "lock-case-variant"],
)
@pytest.mark.parametrize("operation", list(_RESERVED_MUTATIONS))
def test_mutation_of_a_reserved_path_raises_and_leaves_it_untouched(
    storage: TxnStorage, operation: str, name: str, in_transaction: bool
) -> None:
    """Every mutator refuses a reserved name before any leg runs (VALIDATION.md#filename-safety).

    Without the guard a copy onto the S3 lock left a non-JSON body that a peer breaks while
    the holder still runs, and a move of the history ran its copy leg before the refusal.
    """
    storage.put("pool/x", b"pwned")
    storage._obj_put_reserved(HISTORY_FILENAME, b"[]")
    storage._obj_put_reserved(LOCK_FILENAME, b"lock")
    if in_transaction:
        storage.transaction_begin()

    with pytest.raises(StorageException, match="reserved storage path"):
        _RESERVED_MUTATIONS[operation](storage, name)

    assert storage.get(HISTORY_FILENAME) == b"[]"
    assert storage.get(LOCK_FILENAME) == b"lock"
    assert not storage.exists("pool/h")
    assert sorted(storage.hidden_objects()) == [HISTORY_FILENAME, LOCK_FILENAME]


# --------------------------------------------------------------------------- #
# Revert
# --------------------------------------------------------------------------- #


def test_transaction_revert_removes_a_created_object(storage: TxnStorage) -> None:
    model = commit_transaction(storage, "new.txt")
    storage.transaction_revert(model)
    assert not storage.exists("new.txt")


@pytest.mark.parametrize("planted", [".{token}", "pool/.{token}"])
def test_footmark_name_with_no_original_is_left_alone(storage: TxnStorage, planted: str) -> None:
    """`.<token>` maps to no original, so commit never copies it onto the root or a directory."""
    storage.put("pool/a.deb", b"orig")
    model = storage.transaction_begin()
    rel = planted.format(token=model.footmarks[ObjectOperation.PUT.value])
    storage._obj_put_reserved(rel, b"planted")
    storage.put("pool/a.deb", b"new")

    assert rel not in storage._categorize(model.footmarks).footmark_paths
    storage.transaction_commit()
    assert storage.get("pool/a.deb") == b"new"
    assert rel not in storage.find_transaction_footmarks([model])
    # The orphan sweep is the path that deletes: a bare valid token must not pass for a footmark.
    assert rel not in storage.find_orphaned_footmarks([])
    assert rel not in storage.transaction_forget_orphans([])
    assert rel in storage.hidden_objects()


_NAMELESS_MUTATIONS: dict[str, Callable[[TxnStorage, str], object]] = {
    "put": lambda s, name: s.put(name, b"x"),
    "put_stream": lambda s, name: s.put_stream(name, io.BytesIO(b"x")),
    **_RESERVED_MUTATIONS,
}


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
@pytest.mark.parametrize("target", ["", "pool/", "pool/.", "pool/..", ".", ".."])
@pytest.mark.parametrize("operation", list(_NAMELESS_MUTATIONS))
def test_mutation_of_a_path_naming_no_object_is_refused(
    storage: TxnStorage, operation: str, target: str, in_transaction: bool
) -> None:
    """Such a footmark has no original, so a staged change would vanish at commit."""
    if isinstance(storage, StorageFS) and target in ("pool/", "pool/."):
        pytest.skip("StorageFS resolves the path to the object `pool`: pinned below")
    storage.put("pool/x", b"orig")
    # `StorageFS` refuses these by resolution, with its own root and traversal messages.
    match = None if isinstance(storage, StorageFS) else "names no object"
    if in_transaction:
        storage.transaction_begin()
    before = storage.hidden_objects()
    with pytest.raises(StorageException, match=match):
        _NAMELESS_MUTATIONS[operation](storage, target)
    assert storage.hidden_objects() == before


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
@pytest.mark.parametrize("target", ["pool/", "pool/.", "pool/x/.."])
def test_storage_fs_resolves_a_trailing_dot_or_slash_to_the_object(
    tmp_path: Path, target: str, in_transaction: bool
) -> None:
    """`StorageFS` resolves where other backends refuse; nothing written goes missing."""
    storage = StorageFS(str(tmp_path))
    if in_transaction:
        storage.transaction_begin()
    storage.put(target, b"x")
    if in_transaction:
        storage.transaction_commit()
    assert storage.list() == ["pool"]
    assert storage.get("pool") == b"x"


@pytest.mark.parametrize("planted", ["....{token}", "pool/....{token}", "pool/...{token}"])
def test_commit_refuses_a_planted_footmark_of_a_dot_segment(
    storage: TxnStorage, planted: str
) -> None:
    """Fails closed on every backend, as `StorageFS` already did by resolution."""
    storage.put("pool/a.deb", b"orig")
    model = storage.transaction_begin()
    storage._obj_put_reserved(
        planted.format(token=model.footmarks[ObjectOperation.PUT.value]), b"x"
    )
    with pytest.raises(StorageException):
        storage.transaction_commit()
    assert storage.in_transaction()


@pytest.mark.parametrize(
    "operation", [ObjectOperation.PUT, ObjectOperation.DELETE, ObjectOperation.BACKUP]
)
def test_commit_refusing_an_original_applies_nothing(
    storage: TxnStorage, peer: TxnStorage, operation: ObjectOperation
) -> None:
    """The refusal is permanent, so a write before it would leave a commit no retry finishes.

    `a.deb` sits at the root and the planted footmark under `pool/`, so it lists after the good
    one on every backend.
    """
    storage.put("a.deb", b"orig")
    storage.put("pool/b.deb", b"keep")
    model = storage.transaction_begin()
    storage.put("a.deb", b"new")
    storage._obj_put_reserved(f"pool/....{model.footmarks[operation.value]}", b"x")

    with pytest.raises(StorageException, match="Refusing to write"):
        storage.transaction_commit()
    assert storage.in_transaction()
    assert peer.get("a.deb") == b"orig"


@pytest.mark.parametrize("operation", [ObjectOperation.PUT, ObjectOperation.BACKUP])
def test_revert_refusing_an_original_applies_nothing(
    storage: TxnStorage, operation: ObjectOperation
) -> None:
    """Revert writes PUT and BACKUP originals only, so DELETE has nothing to refuse."""
    storage.put("a.deb", b"orig")
    model = storage.transaction_begin()
    storage.put("a.deb", b"new")
    storage.put("b.new", b"created")
    storage.transaction_commit()
    storage.put("pool/c.deb", b"keep")
    storage._obj_put_reserved(f"pool/....{model.footmarks[operation.value]}", b"x")
    footmarks = storage.hidden_objects()

    with pytest.raises(StorageException, match="Refusing to write"):
        storage.transaction_revert(model)
    assert storage.get("a.deb") == b"new"
    assert storage.get("b.new") == b"created"
    assert storage.hidden_objects() == footmarks


def test_categorize_and_reclaim_agree_on_which_files_are_a_transactions_footmarks(
    storage: TxnStorage,
) -> None:
    """Two matchers name a transaction's footmarks; reclaim must never take what revert needs."""
    storage.put("pool/a.deb", b"orig")
    storage.put("pool/b.deb", b"orig")
    model = storage.transaction_begin()
    storage.put("pool/a.deb", b"new")
    storage.put("pool/.env", b"dot")
    storage.delete("pool/b.deb")
    storage.transaction_commit()
    token = model.footmarks[ObjectOperation.PUT.value]
    for decoy in (f"pool/.x.{token}x", f"pool/.x{token}", f".{token}", f"pool/.a.deb.{token[:-1]}"):
        storage.put(decoy, b"decoy")
    listing = storage.hidden_objects()

    categorized = storage._categorize(model.footmarks).footmark_paths
    assert sorted(categorized) == storage.find_transaction_footmarks([model], listing=listing)


def test_commit_and_revert_delete_originals_without_an_existence_check(
    storage: TxnStorage,
) -> None:
    """`_obj_delete` accepts an absent object, so no HEAD precedes each delete on S3."""
    storage.put("gone.txt", b"v")
    storage.transaction_begin()
    storage.delete("gone.txt")
    with patch.object(storage, "_obj_exists", wraps=storage._obj_exists) as probe:
        storage.transaction_commit()
    assert storage._obj_full("gone.txt") not in [c.args[0] for c in probe.call_args_list]

    model = commit_transaction(storage, "new.txt")
    with patch.object(storage, "_obj_exists", wraps=storage._obj_exists) as probe:
        storage.transaction_revert(model)
    assert storage._obj_full("new.txt") not in [c.args[0] for c in probe.call_args_list]
    assert not storage.exists("new.txt")


@pytest.mark.parametrize("phase", ["commit", "revert"])
def test_an_original_removed_out_of_band_fails_neither_commit_nor_revert(
    storage: TxnStorage, peer: TxnStorage, phase: str
) -> None:
    """A writer that ignores the lock may remove it first; the delete is already done."""
    if phase == "commit":
        storage.put("a.txt", b"v")
        storage.transaction_begin()
        storage.delete("a.txt")
        peer.delete("a.txt")
        storage.transaction_commit()
    else:
        model = commit_transaction(storage, "a.txt")
        peer.delete("a.txt")
        storage.transaction_revert(model)
    assert not storage.exists("a.txt")


def test_transaction_revert_restores_a_modified_object(storage: TxnStorage) -> None:
    storage.put("mut.txt", b"orig")
    model = commit_transaction(storage, "mut.txt", b"changed")
    storage.transaction_revert(model)
    assert storage.get("mut.txt") == b"orig"


def test_transaction_revert_restores_a_deleted_object(storage: TxnStorage) -> None:
    storage.put("del.txt", b"orig")
    model = storage.transaction_begin()
    storage.delete("del.txt")
    storage.transaction_commit()

    storage.transaction_revert(model)
    assert storage.get("del.txt") == b"orig"


def test_transaction_revert_of_two_transactions_walks_back_in_order(storage: TxnStorage) -> None:
    first = storage.transaction_begin()
    storage.put("a.txt", b"1")
    storage.put("sub/b.txt", b"1")
    storage.transaction_commit()
    second = storage.transaction_begin()
    storage.delete("a.txt")
    storage.put("sub/b.txt", b"2")
    storage.put("sub/c.txt", b"2")
    storage.transaction_commit()

    storage.transaction_revert(second)
    assert (storage.get("a.txt"), storage.get("sub/b.txt")) == (b"1", b"1")
    assert not storage.exists("sub/c.txt")

    storage.transaction_revert(first)
    assert storage.list() == []
    assert storage.hidden_objects() == []


def test_transaction_revert_warns_when_the_footmarks_are_already_gone(
    storage: TxnStorage, caplog: pytest.LogCaptureFixture
) -> None:
    model = commit_transaction(storage, "gone.deb")
    storage.transaction_forget([model])

    with caplog.at_level(logging.WARNING):
        storage.transaction_revert(model)

    assert "no footmarks left" in caplog.text
    assert storage.exists("gone.deb")


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #


def test_put_metadata_survives_commit(with_metadata: TxnStorage) -> None:
    storage = with_metadata
    storage.transaction_begin()
    storage.put("a.txt", b"hello", metadata={KEY: "v1"})
    storage.transaction_commit()

    assert storage.get_metadata("a.txt").get(KEY) == "v1"


def test_add_metadata_in_transaction_is_invisible_to_a_peer_until_commit(
    with_metadata: TxnStorage, peer: TxnStorage
) -> None:
    storage = with_metadata
    storage.put("a.txt", b"x", metadata={KEY: "old"})
    storage.transaction_begin()
    storage.add_metadata("a.txt", {KEY: "new"})

    assert storage.get_metadata("a.txt").get(KEY) == "new"
    assert peer.get_metadata("a.txt").get(KEY) == "old"

    storage.transaction_commit()
    assert peer.get_metadata("a.txt").get(KEY) == "new"


def _change_metadata(storage: TxnStorage, change: str) -> None:
    if change == "add":
        storage.add_metadata("a.txt", {KEY: "new"})
    elif change == "delete":
        storage.delete_metadata("a.txt")
    else:
        storage.delete_metadata_fields("a.txt", [KEY])


@pytest.mark.parametrize("change", ["add", "delete", "delete_fields"])
@pytest.mark.parametrize("undo", ["discard", "revert"])
def test_metadata_change_is_undone_by_discard_and_revert(
    with_metadata: TxnStorage, change: str, undo: str
) -> None:
    storage = with_metadata
    storage.put("a.txt", b"x", metadata={KEY: "v", OTHER: "w"})

    model = storage.transaction_begin()
    _change_metadata(storage, change)
    if undo == "discard":
        storage.transaction_discard()
    else:
        storage.transaction_commit()
        assert storage.get_metadata("a.txt").get(KEY) != "v"
        storage.transaction_revert(model)

    meta = storage.get_metadata("a.txt")
    assert (meta.get(KEY), meta.get(OTHER)) == ("v", "w")
    assert storage.hidden_objects() == []


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
def test_get_metadata_of_a_missing_object_raises_not_found(
    storage: TxnStorage, in_transaction: bool
) -> None:
    """Like `get`, `get_metadata` reports a missing object the same way on every backend."""
    if in_transaction:
        storage.transaction_begin()
    with pytest.raises(StorageNotFoundException):
        storage.get_metadata("missing.txt")


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
def test_delete_metadata_fields_removes_only_named_present_keys(
    with_metadata: TxnStorage, in_transaction: bool
) -> None:
    """An empty list removes nothing and an absent key is ignored, on every backend."""
    storage = with_metadata
    storage.put("a.txt", b"x", metadata={KEY: "v", OTHER: "w"})
    if in_transaction:
        storage.transaction_begin()

    storage.delete_metadata_fields("a.txt", [])
    assert storage.get_metadata("a.txt").get(KEY) == "v"
    storage.delete_metadata_fields("a.txt", ["absent", KEY])

    meta = storage.get_metadata("a.txt")
    assert KEY not in meta
    assert meta.get(OTHER) == "w"


def test_add_metadata_of_a_missing_object_raises(with_metadata: TxnStorage) -> None:
    """Never a silent no-op: the caller would believe the metadata was written."""
    with pytest.raises(StorageException):
        with_metadata.add_metadata("missing.txt", {KEY: "v"})
    assert not with_metadata.exists("missing.txt")


@pytest.mark.parametrize("change", ["add", "delete", "delete_fields"])
def test_metadata_change_of_a_missing_object_raises_in_transaction(
    with_metadata: TxnStorage, change: str
) -> None:
    storage = with_metadata
    storage.transaction_begin()
    with pytest.raises(StorageException, match="does not exist"):
        _change_metadata(storage, change)
    assert storage.hidden_objects() == []


def test_add_metadata_after_put_reuses_the_staged_object(with_metadata: TxnStorage) -> None:
    storage = with_metadata
    storage.put("a.txt", b"orig", metadata={KEY: "old"})
    storage.transaction_begin()
    storage.put("a.txt", b"new", metadata={KEY: "mid"})
    staged = storage.hidden_objects()

    storage.add_metadata("a.txt", {KEY: "newer"})

    assert storage.hidden_objects() == staged
    storage.transaction_commit()
    assert storage.get("a.txt") == b"new"
    assert storage.get_metadata("a.txt").get(KEY) == "newer"


@pytest.mark.parametrize("operation", ["copy", "move"])
def test_copy_in_transaction_replaces_the_destination_metadata(
    with_metadata: TxnStorage, operation: str
) -> None:
    storage = with_metadata
    storage.put("src.txt", b"new", metadata={KEY: "v"})
    storage.put("dst.txt", b"old", metadata={OTHER: "1"})

    storage.transaction_begin()
    getattr(storage, operation)("src.txt", "dst.txt")
    storage.transaction_commit()

    meta = storage.get_metadata("dst.txt")
    assert storage.get("dst.txt") == b"new"
    assert (meta.get(KEY), meta.get(OTHER)) == ("v", None)


@pytest.mark.parametrize("change", ["modify", "delete"])
def test_transaction_revert_restores_the_original_metadata(
    with_metadata: TxnStorage, change: str
) -> None:
    storage = with_metadata
    storage.put("a.txt", b"orig", metadata={KEY: "old"})
    model = storage.transaction_begin()
    if change == "modify":
        storage.put("a.txt", b"changed", metadata={KEY: "new"})
    else:
        storage.delete("a.txt")
    storage.transaction_commit()

    storage.transaction_revert(model)

    assert storage.get("a.txt") == b"orig"
    assert storage.get_metadata("a.txt").get(KEY) == "old"


# --------------------------------------------------------------------------- #
# Failure injection: a failing primitive leaves a retryable state
# --------------------------------------------------------------------------- #


def test_transaction_commit_failing_mid_put_raises_and_can_be_retried(
    storage: TxnStorage, peer: TxnStorage
) -> None:
    storage.transaction_begin()
    storage.put("a.txt", b"a")
    storage.put("b.txt", b"b")

    failing = _fail_on_call(storage._obj_copy, 2, OSError("disk gone"))
    with (
        patch.object(storage, "_obj_copy", side_effect=failing),
        pytest.raises(StorageException, match="Failed to commit PUT"),
    ):
        storage.transaction_commit()

    assert storage.in_transaction()
    assert len([p for p in ("a.txt", "b.txt") if peer.exists(p)]) == 1

    assert storage.transaction_commit() is True
    assert (peer.get("a.txt"), peer.get("b.txt")) == (b"a", b"b")


def test_transaction_commit_failing_mid_delete_raises_and_can_be_retried(
    storage: TxnStorage, peer: TxnStorage
) -> None:
    """The retry meets an original the first attempt already deleted, and skips it."""
    storage.put("a.txt", b"a")
    storage.put("b.txt", b"b")
    storage.transaction_begin()
    storage.delete("a.txt")
    storage.delete("b.txt")

    failing = _fail_on_call(storage._obj_delete, 2, OSError("busy"))
    with (
        patch.object(storage, "_obj_delete", side_effect=failing),
        pytest.raises(StorageException, match="Failed to commit DELETE"),
    ):
        storage.transaction_commit()

    assert storage.in_transaction()
    assert len([p for p in ("a.txt", "b.txt") if peer.exists(p)]) == 1

    assert storage.transaction_commit() is True
    assert not peer.exists("a.txt")
    assert not peer.exists("b.txt")


def test_transaction_revert_failing_on_a_restore_keeps_the_footmarks_for_a_retry(
    storage: TxnStorage,
) -> None:
    storage.put("mut.txt", b"orig")
    model = commit_transaction(storage, "mut.txt", b"changed")
    footmarks = storage.hidden_objects()

    with (
        patch.object(storage, "_obj_copy", side_effect=OSError("disk gone")),
        pytest.raises(StorageException, match="Failed to restore"),
    ):
        storage.transaction_revert(model)

    assert storage.hidden_objects() == footmarks
    storage.transaction_revert(model)
    assert storage.get("mut.txt") == b"orig"
    assert storage.hidden_objects() == []


def test_revert_last_failing_keeps_the_record_for_a_retry(storage: TxnStorage) -> None:
    """`revert_last` reverts before it rewrites the history → STORAGE.md#operations."""
    storage.put("mut.txt", b"orig")
    with maybe_transaction(storage):
        storage.put("mut.txt", b"changed")
    history = TransactionHistory(storage)
    records = history.load()
    footmarks = storage.hidden_objects()

    # The restore copies the BACKUP back, so only a modified original reaches `_obj_copy`.
    with (
        patch.object(storage, "_obj_copy", side_effect=OSError("disk gone")),
        pytest.raises(StorageException, match="Failed to restore"),
    ):
        history.revert_last()

    assert history.load() == records
    assert storage.hidden_objects() == footmarks
    assert storage.get("mut.txt") == b"changed"

    reverted = history.revert_last()
    assert reverted is not None
    assert reverted.id == records[-1].id
    assert history.load() == []
    assert storage.get("mut.txt") == b"orig"


class _Crash(BaseException):
    """The process dies mid-call: no handler in the library catches it."""


@contextmanager
def _crash_in_reclaim(storage: TxnStorage, deletes: int) -> Iterator[None]:
    """Kill the process in `_obj_reclaim_delete_many` after *deletes* footmark deletes.

    The block fails unless `_Crash` escapes it.
    """
    real = storage._obj_reclaim_delete_many
    budget = deletes

    def wrapper(rels: Sequence[str]) -> Any:
        nonlocal budget
        allowed = list(rels[:budget])
        budget -= len(allowed)
        result = real(allowed)
        if len(allowed) < len(rels):
            raise _Crash
        return result

    with (
        patch.object(storage, "_obj_reclaim_delete_many", side_effect=wrapper),
        pytest.raises(_Crash),
    ):
        yield


# BACKUP and PUT of a.txt and b.txt, PUT of the created c.txt.
_COMMITTED_FOOTMARKS = 5


def _commit_two_changes_and_a_creation(storage: TxnStorage) -> TransactionModel:
    """Commit new `a.txt` and `b.txt` over originals and a created `c.txt`."""
    storage.put("a.txt", b"olda")
    storage.put("b.txt", b"oldb")
    model = storage.transaction_begin()
    for name in ("a", "b", "c"):
        storage.put(f"{name}.txt", f"new{name}".encode())
    storage.transaction_commit()
    assert len(storage.hidden_objects()) == _COMMITTED_FOOTMARKS
    return model


@pytest.mark.parametrize("deletes", range(_COMMITTED_FOOTMARKS))
def test_a_revert_cut_short_in_its_cleanup_restores_again_on_retry(
    storage: TxnStorage, deletes: int
) -> None:
    """A retry must never take a PUT footmark whose BACKUP went first for a created object."""
    model = _commit_two_changes_and_a_creation(storage)
    with _crash_in_reclaim(storage, deletes):
        storage.transaction_revert(model)

    storage.transaction_revert(model)

    assert (storage.get("a.txt"), storage.get("b.txt")) == (b"olda", b"oldb")
    assert not storage.exists("c.txt")


@pytest.mark.parametrize("deletes", range(_COMMITTED_FOOTMARKS))
def test_a_forget_cut_short_never_lets_a_revert_delete_an_original(
    storage: TxnStorage, deletes: int
) -> None:
    """A prune forgets before it rewrites the history, so a record can outlive part of them."""
    model = _commit_two_changes_and_a_creation(storage)
    with _crash_in_reclaim(storage, deletes):
        storage.transaction_forget([model])

    storage.transaction_revert(model)

    assert storage.exists("a.txt")
    assert storage.exists("b.txt")


def test_a_revert_keeps_the_backup_beside_a_footmark_that_resisted(
    storage: TxnStorage, caplog: pytest.LogCaptureFixture
) -> None:
    """The kept BACKUP is what stops a retry from taking the original for a created one."""
    model = _commit_two_changes_and_a_creation(storage)
    stubborn = f".b.txt.{model.footmarks[ObjectOperation.PUT.value]}"
    backup = f".b.txt.{model.footmarks[ObjectOperation.BACKUP.value]}"
    real = storage._obj_reclaim_delete_many

    def resist(rels: Sequence[str]) -> tuple[list[str], list[tuple[str, str]]]:
        removed, failed = real([rel for rel in rels if rel != stubborn])
        return removed, failed + [(rel, "busy") for rel in rels if rel == stubborn]

    with (
        caplog.at_level(logging.WARNING),
        patch.object(storage, "_obj_reclaim_delete_many", side_effect=resist),
    ):
        storage.transaction_revert(model)

    assert storage.hidden_objects() == sorted([stubborn, backup])
    assert f"Kept BACKUP footmark {backup}" in caplog.text
    storage.transaction_revert(model)
    assert storage.get("b.txt") == b"oldb"


def _gone(footmark: str) -> str:
    """The `match` of the refusal naming *footmark*, a staged footmark gone from storage."""
    return re.escape(f"staged footmark gone: {footmark}")


def _footmark(storage: TxnStorage, rel: str, operation: ObjectOperation) -> str:
    """Full path of *operation*'s footmark of `rel` in the active transaction."""
    return storage._add_footmark(storage._obj_full(rel), operation)


def _stage_then_lose(storage: TxnStorage, rel: str, operation: ObjectOperation) -> str:
    """Stage `a.txt` over an original and delete `b.txt`, then remove one staged footmark.

    Returns:
        The full path of the removed footmark: *operation*'s footmark of *rel*.
    """
    storage.put("a.txt", b"olda")
    storage.put("b.txt", b"oldb")
    storage.transaction_begin()
    storage.put("a.txt", b"a")
    storage.delete("b.txt")
    gone = _footmark(storage, rel, operation)
    storage._obj_delete(gone)
    return gone


@pytest.mark.parametrize(
    ("rel", "operation"),
    [
        pytest.param("a.txt", ObjectOperation.PUT, id="put"),
        pytest.param("a.txt", ObjectOperation.BACKUP, id="backup"),
        pytest.param("b.txt", ObjectOperation.DELETE, id="delete"),
    ],
)
def test_commit_refuses_a_staged_footmark_gone_before_its_first_attempt(
    storage: TxnStorage, peer: TxnStorage, rel: str, operation: ObjectOperation
) -> None:
    """A footmark a peer's orphan sweep removed must not be skipped, losing its change."""
    gone = _stage_then_lose(storage, rel, operation)

    with pytest.raises(StorageException, match=_gone(gone)):
        storage.transaction_commit()

    assert storage.in_transaction()
    assert (peer.get("a.txt"), peer.get("b.txt")) == (b"olda", b"oldb")


def _fail_a_commit_then_lose_a_put(storage: TxnStorage) -> str:
    """Fail a commit of `a.txt` over an original and a created `b.txt`, then drop b's PUT.

    The original makes the failed attempt also stage a BACKUP of `a.txt`.

    Returns:
        The full path of the dropped PUT footmark.
    """
    storage.put("a.txt", b"olda")
    storage.transaction_begin()
    storage.put("a.txt", b"a")
    storage.put("b.txt", b"b")
    with (
        patch.object(storage, "_obj_copy", side_effect=OSError("disk gone")),
        pytest.raises(StorageException, match="Failed to commit PUT"),
    ):
        storage.transaction_commit()
    gone = _footmark(storage, "b.txt", ObjectOperation.PUT)
    storage._obj_delete(gone)
    return gone


def test_a_commit_retry_refuses_a_footmark_gone_since_the_failed_attempt(
    storage: TxnStorage, peer: TxnStorage
) -> None:
    """A retry lists again; it must not apply the rest and drop the vanished PUT in silence."""
    gone = _fail_a_commit_then_lose_a_put(storage)

    with pytest.raises(StorageException, match=_gone(gone)):
        storage.transaction_commit()

    assert storage.in_transaction()
    storage.put("b.txt", b"b2")  # a fresh write stages the PUT again
    assert storage.transaction_commit() is True
    assert (peer.get("a.txt"), peer.get("b.txt")) == (b"a", b"b2")


def _refused_delete_of_the_lost(storage: TxnStorage) -> None:
    """A delete that finds nothing to delete stages nothing, so b's PUT is still expected."""
    with pytest.raises(StorageNotFoundException):
        storage.delete("b.txt")


def _delete_cut_short(storage: TxnStorage) -> None:
    """The PUT a delete drops itself is no longer expected, even when its marker write fails.

    Commit names the smallest gone footmark, so a's PUT left staged would be named, not b's.
    """
    with (
        patch.object(storage, "_obj_touch", side_effect=OSError("disk gone")),
        pytest.raises(StorageException, match="Failed to create DELETE marker"),
    ):
        storage.delete("a.txt")


@pytest.mark.parametrize(
    "stage",
    [
        pytest.param(lambda s: s.delete("a.txt"), id="delete"),
        pytest.param(lambda s: s.put("c.txt", b"c"), id="put"),
        pytest.param(lambda s: s.move("a.txt", "d.txt"), id="move"),
        pytest.param(_refused_delete_of_the_lost, id="refused-delete"),
        pytest.param(_delete_cut_short, id="delete-cut-short"),
    ],
)
def test_staging_after_a_failed_commit_keeps_the_vanished_put_refused(
    storage: TxnStorage, peer: TxnStorage, stage: Callable[[TxnStorage], None]
) -> None:
    """Staging after a failed commit, done or refused, must not let the retry lose b's write."""
    gone = _fail_a_commit_then_lose_a_put(storage)

    stage(storage)

    with pytest.raises(StorageException, match=_gone(gone)):
        storage.transaction_commit()
    assert not peer.exists("b.txt")


def test_a_delete_supersedes_a_staged_write_whose_put_vanished(
    storage: TxnStorage, peer: TxnStorage
) -> None:
    """Deleting the object drops the write it staged, so its vanished PUT no longer matters."""
    _stage_then_lose(storage, "a.txt", ObjectOperation.PUT)

    storage.delete("a.txt")

    assert storage.transaction_commit() is True
    assert not peer.exists("a.txt")


@pytest.mark.parametrize("change", ["add", "delete", "delete_fields"])
def test_a_metadata_change_never_re_creates_a_vanished_put(
    with_metadata: TxnStorage, peer: TxnStorage, change: str
) -> None:
    """Copying the original into the PUT again would commit it in place of the staged write."""
    storage = with_metadata
    gone = _stage_then_lose(storage, "a.txt", ObjectOperation.PUT)

    with pytest.raises(StorageException, match=_gone(gone)):
        _change_metadata(storage, change)

    with pytest.raises(StorageException, match=_gone(gone)):
        storage.transaction_commit()
    assert peer.get("a.txt") == b"olda"


@pytest.mark.parametrize(
    "stage",
    [
        pytest.param(lambda s: s.put("a.txt", b"a2"), id="put"),
        pytest.param(lambda s: s.delete("a.txt"), id="delete"),
    ],
)
def test_staging_never_re_creates_a_vanished_backup(
    storage: TxnStorage, peer: TxnStorage, stage: Callable[[TxnStorage], None]
) -> None:
    """Copying the original again could snapshot what a failed commit already overwrote."""
    gone = _stage_then_lose(storage, "a.txt", ObjectOperation.BACKUP)

    with pytest.raises(StorageException, match=_gone(gone)):
        stage(storage)

    with pytest.raises(StorageException, match=_gone(gone)):
        storage.transaction_commit()
    assert peer.get("a.txt") == b"olda"


def test_case_variants_stay_two_footmarks_where_names_are_case_sensitive() -> None:
    """The fold only finds alias candidates; on a case-sensitive store `A` and `a` stay apart."""
    storage = MemoryTxnStorage()
    storage.put("A.txt", b"oldA")
    storage.put("a.txt", b"olda")
    storage.transaction_begin()
    storage.put("A.txt", b"A")
    storage.put("a.txt", b"a")  # A's BACKUP still exists: no refusal for a's
    storage.delete("a.txt")  # drops a's PUT only
    gone = _footmark(storage, "A.txt", ObjectOperation.PUT)
    storage._obj_delete(gone)

    with pytest.raises(StorageException, match=_gone(gone)):
        storage.transaction_commit()


@pytest.fixture(params=["folding-memory", "fs"])
def folding(request: pytest.FixtureRequest, tmp_path: Path) -> TxnStorage:
    """A backend whose names fold: the folding memory one on every host, and `StorageFS`.

    `StorageFS` folds only where the host filesystem does (macOS APFS, vfat, SMB); each test
    probes its own spellings through `_skip_unless_aliased`.
    """
    if request.param == "folding-memory":
        return FoldingMemoryTxnStorage()
    base = tmp_path / "repo"
    base.mkdir()
    return StorageFS(str(base))


def _skip_unless_aliased(storage: TxnStorage, name: str, alias: str) -> None:
    """Skip unless `storage` takes *alias* for *name*; only `StorageFS` can tell them apart.

    Linux filesystems are case- and normalization-sensitive by default: that is a property of the
    host, not a backend, so a plain skip, which `--strict-backends` leaves a skip.
    """
    if not isinstance(storage, StorageFS):
        return
    probe = Path(storage.base_dir, name)
    probe.write_bytes(b"")
    aliased = Path(storage.base_dir, alias).exists()
    probe.unlink()
    if not aliased:
        pytest.skip(f"filesystem tells {name!r} from {alias!r}")


_NFC = unicodedata.normalize("NFC", "é.txt")
_NFD = unicodedata.normalize("NFD", "é.txt")


def _put_twice(name: str, alias: str) -> Callable[[Storage], None]:
    def stage(s: Storage) -> None:
        s.put(name, b"one")
        s.put(alias, b"new")

    return stage


def _put_then_delete(name: str, alias: str) -> Callable[[Storage], None]:
    def stage(s: Storage) -> None:
        s.put(name, b"new")
        s.delete(alias)

    return stage


def _delete_then_put(name: str, alias: str) -> Callable[[Storage], None]:
    def stage(s: Storage) -> None:
        s.delete(name)
        s.put(alias, b"new")

    return stage


def _put_alias(name: str, alias: str) -> Callable[[Storage], None]:
    del name
    return lambda s: s.put(alias, b"new")


@pytest.mark.parametrize(
    ("name", "alias", "stage", "committed"),
    [
        pytest.param("A.txt", "a.txt", _put_twice, b"new", id="case-put-twice"),
        pytest.param(_NFC, _NFD, _put_twice, b"new", id="unicode-put-twice"),
        pytest.param("d/B.txt", "D/B.txt", _put_alias, b"new", id="directory-case-put"),
        pytest.param("A.txt", "a.txt", _put_then_delete, None, id="put-then-delete-alias"),
        pytest.param("A.txt", "a.txt", _delete_then_put, b"new", id="delete-then-put-alias"),
    ],
)
def test_staging_under_an_alias_spelling_commits(
    folding: TxnStorage,
    name: str,
    alias: str,
    stage: Callable[[str, str], Callable[[Storage], None]],
    committed: bytes | None,
) -> None:
    """Two spellings of one object stage one footmark, which commit must find, not call gone."""
    storage = folding
    _skip_unless_aliased(storage, name.replace("/", "_"), alias.replace("/", "_"))
    storage.put(name, b"old")
    storage.transaction_begin()

    stage(name, alias)(storage)

    assert storage.transaction_commit() is True
    assert (storage.get(name) if storage.exists(name) else None) == committed


def test_a_write_under_an_alias_never_re_creates_a_vanished_backup(folding: TxnStorage) -> None:
    """The alias spelling must not hide that the staged BACKUP is gone and re-copy the original."""
    storage = folding
    _skip_unless_aliased(storage, "probe.deb", "PROBE.deb")
    storage.put("A.txt", b"old")
    storage.transaction_begin()
    storage.put("A.txt", b"one")
    backup = _footmark(storage, "A.txt", ObjectOperation.BACKUP)
    storage._obj_delete(backup)

    with pytest.raises(StorageException, match=_gone(backup)):
        storage.put("a.txt", b"two")

    assert not storage._obj_exists(backup)  # under any spelling: the backend folds


@pytest.mark.parametrize("written", ["direct", "committed", "staged"])
def test_move_onto_a_case_alias_of_itself_keeps_the_object(
    folding: TxnStorage, written: str
) -> None:
    """`F.deb` and `f.deb` name one object; a staged one has no backup to restore."""
    storage = folding
    _skip_unless_aliased(storage, "probe.deb", "PROBE.deb")
    if written != "staged":
        storage.put("F.deb", b"data")
    if written != "direct":
        storage.transaction_begin()
    if written == "staged":
        storage.put("F.deb", b"data")
    storage.move("F.deb", "f.deb")
    if written != "direct":
        storage.transaction_commit()

    assert storage.get("F.deb") == b"data"
    # `get` folds too; the stored name shows whether a rename happened.
    assert "F.deb" in storage._obj_iter_rel_all()


def test_transaction_begin_clears_the_staged_footmarks(storage: TxnStorage) -> None:
    """`transaction_begin` drops what a discarded transaction staged, vanished PUT included."""
    _fail_a_commit_then_lose_a_put(storage)
    storage.transaction_discard()

    storage.transaction_begin()

    # Nothing staged: a write would only add to the carried-over footmarks, not hide them.
    assert storage.transaction_commit() is False


def test_transaction_revert_failing_on_a_created_object_raises(storage: TxnStorage) -> None:
    model = commit_transaction(storage, "new.txt")

    with (
        patch.object(storage, "_obj_delete", side_effect=OSError("busy")),
        pytest.raises(StorageException, match="Failed to remove newly created object"),
    ):
        storage.transaction_revert(model)

    assert storage.exists("new.txt")


def _batch_fails_every_path(rels: Sequence[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """A `_obj_reclaim_delete_many` stand-in that removes nothing, as a busy backend would."""
    return [], [(rel, "batch-refused") for rel in rels]


def test_transaction_discard_failing_keeps_the_transaction_for_a_retry(
    storage: TxnStorage,
) -> None:
    storage.transaction_begin()
    storage.put("a.txt", b"v")

    with (
        patch.object(storage, "_obj_reclaim_delete_many", side_effect=_batch_fails_every_path),
        patch.object(storage, "_reclaim_footmark", side_effect=OSError("busy")),
        pytest.raises(StorageException, match="Failed to remove footmark") as info,
    ):
        storage.transaction_discard()

    assert isinstance(info.value.__cause__, OSError)
    assert storage.in_transaction()
    storage.transaction_discard()
    assert storage.hidden_objects() == []


def test_transaction_discard_retries_every_failed_path_and_logs_the_later_failures(
    storage: TxnStorage, caplog: pytest.LogCaptureFixture
) -> None:
    """One failing retry does not leave the others unattempted → STORAGE.md#lifecycle."""
    storage.transaction_begin()
    storage.put("a.txt", b"v")
    storage.put("b.txt", b"v")
    retried: list[str] = []

    def fail(rel: str) -> None:
        retried.append(rel)
        raise OSError(f"busy {rel}")

    with (
        patch.object(storage, "_obj_reclaim_delete_many", side_effect=_batch_fails_every_path),
        patch.object(storage, "_reclaim_footmark", side_effect=fail),
        caplog.at_level(logging.WARNING, logger="txstore.transaction"),
        pytest.raises(StorageException, match="Failed to remove footmark") as info,
    ):
        storage.transaction_discard()

    assert len(retried) == 2
    assert retried[0] in str(info.value)
    assert retried[1] not in str(info.value)
    warned = " ".join(r.getMessage() for r in caplog.records if r.name == "txstore.transaction")
    assert retried[1] in warned
    assert retried[0] not in warned
    assert storage.in_transaction()


def test_transaction_discard_completes_when_the_retry_of_a_batch_failure_succeeds(
    storage: TxnStorage, caplog: pytest.LogCaptureFixture
) -> None:
    """A transient or unreported batch failure resolves on the single delete → STORAGE.md;
    its reason is still logged."""
    storage.transaction_begin()
    storage.put("a.txt", b"v")

    with (
        patch.object(storage, "_obj_reclaim_delete_many", side_effect=_batch_fails_every_path),
        caplog.at_level(logging.DEBUG, logger="txstore.transaction"),
    ):
        storage.transaction_discard()
    assert "batch-refused" in caplog.text

    assert not storage.in_transaction()
    assert storage.hidden_objects() == []


def test_transaction_discard_passes_a_storage_exception_through(storage: TxnStorage) -> None:
    """A refusal already names its path; it is not wrapped a second time."""
    storage.transaction_begin()
    storage.put("a.txt", b"v")
    refusal = StorageException("Refusing non-regular footmark: x")

    with (
        patch.object(storage, "_obj_reclaim_delete_many", side_effect=_batch_fails_every_path),
        patch.object(storage, "_reclaim_footmark", side_effect=refusal),
        pytest.raises(StorageException) as info,
    ):
        storage.transaction_discard()

    assert info.value is refusal
    assert storage.in_transaction()


def test_put_whose_backup_fails_stages_nothing(storage: TxnStorage, peer: TxnStorage) -> None:
    storage.put("a.txt", b"orig")
    storage.transaction_begin()

    with (
        patch.object(storage, "_obj_copy", side_effect=OSError("disk gone")),
        pytest.raises(StorageException, match="Failed to create backup"),
    ):
        storage.put("a.txt", b"new")

    assert storage.get("a.txt") == b"orig"
    storage.transaction_commit()
    assert peer.get("a.txt") == b"orig"


def test_delete_whose_marker_fails_raises(storage: TxnStorage) -> None:
    storage.put("a.txt", b"orig")
    storage.transaction_begin()

    with (
        patch.object(storage, "_obj_touch", side_effect=OSError("disk gone")),
        pytest.raises(StorageException, match="Failed to create DELETE marker"),
    ):
        storage.delete("a.txt")

    assert storage.exists("a.txt")


def test_metadata_change_whose_staging_copy_fails_leaves_the_original(
    with_metadata: TxnStorage,
) -> None:
    storage = with_metadata
    storage.put("a.txt", b"orig", metadata={KEY: "old"})
    storage.transaction_begin()
    failing = _fail_on_call(storage._obj_copy, 2, OSError("disk gone"))  # 1: backup, 2: stage

    with (
        patch.object(storage, "_obj_copy", side_effect=failing),
        pytest.raises(StorageException, match="for metadata change") as raised,
    ):
        storage.add_metadata("a.txt", {KEY: "new"})

    assert isinstance(raised.value.__cause__, OSError)
    storage.transaction_discard()
    assert storage.get_metadata("a.txt")[KEY] == "old"


def test_delete_whose_put_footmark_drop_fails_raises(storage: TxnStorage) -> None:
    storage.transaction_begin()
    storage.put("new.txt", b"v")

    with (
        patch.object(storage, "_obj_delete", side_effect=OSError("disk gone")),
        pytest.raises(StorageException, match="Failed to drop PUT footmark"),
    ):
        storage.delete("new.txt")

    assert storage.get("new.txt") == b"v"


def test_copy_in_a_transaction_whose_copy_fails_stages_nothing(
    storage: TxnStorage, peer: TxnStorage
) -> None:
    storage.put("a.txt", b"orig")
    storage.transaction_begin()

    with (
        patch.object(storage, "_obj_copy", side_effect=OSError("disk gone")),
        pytest.raises(StorageException, match="Failed to copy a.txt -> b.txt"),
    ):
        storage.copy("a.txt", "b.txt")

    storage.transaction_commit()
    assert not peer.exists("b.txt")


@pytest.mark.parametrize(
    ("primitive", "operation", "message"),
    [
        ("_obj_delete", lambda s: s.delete("a.txt"), "Failed to delete a.txt"),
        ("_obj_copy", lambda s: s.copy("a.txt", "b.txt"), "Failed to copy a.txt -> b.txt"),
    ],
    ids=["delete", "copy"],
)
def test_direct_mutation_maps_a_primitive_failure(
    storage: TxnStorage,
    primitive: str,
    operation: Callable[[TxnStorage], None],
    message: str,
) -> None:
    """Outside a transaction a failing primitive surfaces as `StorageException`, chained."""
    storage.put("a.txt", b"orig")

    with (
        patch.object(storage, primitive, side_effect=OSError("disk gone")),
        pytest.raises(StorageException, match=message) as raised,
    ):
        operation(storage)

    assert type(raised.value) is StorageException
    assert isinstance(raised.value.__cause__, OSError)
    assert storage.get("a.txt") == b"orig"


def test_maybe_transaction_records_nothing_when_the_commit_fails(storage: TxnStorage) -> None:
    with (
        patch.object(storage, "_obj_copy", side_effect=OSError("disk gone")),
        pytest.raises(StorageException, match="Failed to commit PUT"),
        maybe_transaction(storage),
    ):
        storage.put("a.txt", b"v")

    assert TransactionHistory(storage).load() == []


def test_maybe_transaction_abandons_a_failed_commit_and_keeps_its_footmarks(
    storage: TxnStorage, peer: TxnStorage
) -> None:
    with (
        patch.object(storage, "_obj_copy", side_effect=OSError("disk gone")),
        pytest.raises(StorageException, match="Failed to commit PUT"),
        maybe_transaction(storage),
    ):
        storage.put("a.txt", b"v")

    assert not storage.in_transaction()
    assert storage.find_orphaned_footmarks([])
    with maybe_transaction(storage):
        storage.put("b.txt", b"b")
    assert peer.get("b.txt") == b"b"
    assert len(TransactionHistory(storage).load()) == 1


def test_maybe_transaction_abandons_a_transaction_whose_discard_fails(
    storage: TxnStorage,
) -> None:
    def put_then_fail() -> None:
        with maybe_transaction(storage):
            storage.put("a.txt", b"v")
            raise RuntimeError("body")

    with (
        patch.object(storage, "_obj_reclaim_delete_many", side_effect=_batch_fails_every_path),
        patch.object(storage, "_reclaim_footmark", side_effect=OSError("busy")),
        pytest.raises(RuntimeError, match="body"),
    ):
        put_then_fail()

    assert not storage.in_transaction()
    assert storage.find_orphaned_footmarks([])


# --------------------------------------------------------------------------- #
# Footmark reclamation
# --------------------------------------------------------------------------- #


def test_transaction_forget_removes_only_the_listed_transactions(storage: TxnStorage) -> None:
    old = commit_transaction(storage, "old.deb")
    kept = commit_transaction(storage, "kept.deb")

    removed = storage.transaction_forget([old])

    hidden = storage.hidden_objects()
    assert removed == footmarks_of(removed, old)
    assert not footmarks_of(hidden, old)
    assert footmarks_of(hidden, kept)
    # Originals are untouched: forget is not a revert.
    assert storage.get("old.deb") == b"payload"


def test_transaction_forget_of_no_model_lists_nothing(storage: TxnStorage) -> None:
    with patch.object(storage, "_obj_iter_rel_all", side_effect=AssertionError("must not list")):
        assert storage.transaction_forget([]) == []


def test_transaction_forget_refuses_while_a_transaction_is_active(storage: TxnStorage) -> None:
    model = commit_transaction(storage, "old.deb")
    storage.transaction_begin()
    with pytest.raises(StorageException, match="another transaction is active"):
        storage.transaction_forget([model])


def test_transaction_forget_deletes_the_given_footmarks_without_listing(
    storage: TxnStorage,
) -> None:
    old = commit_transaction(storage, "old.deb")
    kept = commit_transaction(storage, "kept.deb")
    found = storage.find_transaction_footmarks([old])

    with patch.object(storage, "_obj_iter_rel_all", side_effect=AssertionError("must not list")):
        assert storage.transaction_forget([old], paths=found) == found
        assert storage.transaction_forget([old], paths=[]) == []

    hidden = storage.hidden_objects()
    assert not footmarks_of(hidden, old)
    assert footmarks_of(hidden, kept)


def test_transaction_forget_spares_given_paths_that_are_not_the_models(
    storage: TxnStorage,
) -> None:
    """The given paths are re-checked, so a caller cannot delete through forget what is not its."""
    old = commit_transaction(storage, "old.deb")
    kept = commit_transaction(storage, "kept.deb")
    storage._obj_put_reserved(HISTORY_FILENAME, b"[]")
    storage.put(".hidden.txt", b"mine")
    foreign = [HISTORY_FILENAME, ".hidden.txt", *footmarks_of(storage.hidden_objects(), kept)]
    found = storage.find_transaction_footmarks([old])

    assert storage.transaction_forget([old], paths=[*found, *foreign]) == found

    assert set(foreign) <= set(storage.hidden_objects())


def test_transaction_forget_with_footmarks_refuses_while_a_transaction_is_active(
    storage: TxnStorage,
) -> None:
    model = commit_transaction(storage, "old.deb")
    found = storage.find_transaction_footmarks([model])
    storage.transaction_begin()
    with pytest.raises(StorageException, match="another transaction is active"):
        storage.transaction_forget([model], paths=found)


def test_transaction_forget_orphans_deletes_the_given_orphans_without_listing(
    storage: TxnStorage,
) -> None:
    recorded = commit_transaction(storage, "kept.deb")
    storage.transaction_begin()
    storage.put("ghost.deb", b"lost")
    abandon_transaction(storage)
    found = storage.find_orphaned_footmarks([recorded.footmarks])
    spared = footmarks_of(storage.hidden_objects(), recorded)

    with patch.object(storage, "_obj_iter_rel_all", side_effect=AssertionError("must not list")):
        removed = storage.transaction_forget_orphans(
            [recorded.footmarks], orphans=[*found, *spared]
        )

    assert removed == found
    assert footmarks_of(storage.hidden_objects(), recorded) == spared


def test_find_orphaned_footmarks_refuses_while_a_transaction_is_active(
    storage: TxnStorage,
) -> None:
    storage.transaction_begin()
    with pytest.raises(StorageException, match="another transaction is active"):
        storage.find_orphaned_footmarks([])


def test_find_orphaned_footmarks_reports_residue_of_a_crashed_writer(storage: TxnStorage) -> None:
    recorded = commit_transaction(storage, "kept.deb")
    crashed = storage.transaction_begin()
    storage.put("ghost.deb", b"lost")
    abandon_transaction(storage)

    orphans = storage.find_orphaned_footmarks([recorded.footmarks])

    assert orphans
    assert footmarks_of(orphans, crashed) == orphans


def test_transaction_forget_orphans_spares_recorded_reserved_and_foreign_objects(
    storage: TxnStorage,
) -> None:
    recorded = commit_transaction(storage, "kept.deb")
    storage._obj_put_reserved(HISTORY_FILENAME, b"[]")
    storage.put(".hidden.txt", b"mine")
    storage.transaction_begin()
    storage.put("ghost.deb", b"lost")
    abandon_transaction(storage)

    removed = storage.transaction_forget_orphans([recorded.footmarks])

    assert removed
    assert storage.find_orphaned_footmarks([recorded.footmarks]) == []
    hidden = storage.hidden_objects()
    assert {HISTORY_FILENAME, ".hidden.txt"} <= set(hidden)
    assert footmarks_of(hidden, recorded)


def test_find_transaction_footmarks_uses_the_given_listing(storage: TxnStorage) -> None:
    model = commit_transaction(storage, "old.deb")
    footmarks = footmarks_of(storage.hidden_objects(), model)

    with patch.object(storage, "_obj_iter_rel_all", side_effect=AssertionError("must not list")):
        assert storage.find_transaction_footmarks([model], listing=footmarks) == footmarks
        assert storage.find_transaction_footmarks([model], listing=[]) == []


def test_hidden_objects_lists_every_dotfile_once(storage: TxnStorage) -> None:
    model = commit_transaction(storage, "pool/pkg.deb")
    storage._obj_put_reserved(HISTORY_FILENAME, b"[]")
    storage.put(".hidden.txt", b"x")

    hidden = storage.hidden_objects()

    assert hidden == sorted(set(hidden))
    assert {HISTORY_FILENAME, ".hidden.txt"} <= set(hidden)
    assert footmarks_of(hidden, model)
    assert "pool/pkg.deb" not in hidden


def test_hidden_objects_propagates_a_listing_failure(storage: TxnStorage) -> None:
    with (
        patch.object(storage, "_obj_iter_rel_all", side_effect=StorageException("boom")),
        pytest.raises(StorageException, match="boom"),
    ):
        storage.hidden_objects()


# --------------------------------------------------------------------------- #
# FootmarkMixin matching rules, on the in-memory backend alone
# --------------------------------------------------------------------------- #


def _with_tokens(*tokens: str) -> TransactionModel:
    """A stand-in carrying hand-picked tokens, for the matching rules alone."""
    return cast(TransactionModel, SimpleNamespace(footmarks=tokens))


def test_find_transaction_footmarks_reads_the_last_segment_as_the_token() -> None:
    """Tokens hold no `.`, so the split `_categorize` shares never yields a dotted one."""
    dotted = _with_tokens("A.B")
    listing = ["pool/.pkg.deb.A.B", ".root.A.B"]

    assert MemoryTxnStorage().find_transaction_footmarks([dotted], listing=listing) == []


def test_find_transaction_footmarks_matches_only_the_exact_token_tail() -> None:
    model = TransactionModel(7, Timestamp.from_json("2026-01-01T00:00:00Z"))
    token = model.footmarks[ObjectOperation.PUT]
    listing = [
        f".pkg.deb.{token}",
        f".pkg.deb.{token}x",
        f".pkg.deb.X{token}",
        f".pkg.deb.{token}.bak",
    ]

    assert MemoryTxnStorage().find_transaction_footmarks([model], listing=listing) == [
        f".pkg.deb.{token}"
    ]


def test_find_transaction_footmarks_matches_tokens_of_different_lengths() -> None:
    short = _with_tokens("AB")
    long = _with_tokens("ABCD")
    listing = [".a.AB", ".b.ABCD", ".c.CD"]

    assert MemoryTxnStorage().find_transaction_footmarks([short, long], listing=listing) == [
        ".a.AB",
        ".b.ABCD",
    ]


def test_categorize_classifies_each_footmark_by_operation() -> None:
    storage = MemoryTxnStorage()
    storage.put("mod.txt", b"orig")
    storage.put("del.txt", b"orig")
    model = storage.transaction_begin()
    storage.put("mod.txt", b"new")
    storage.put("new.txt", b"new")
    storage.delete("del.txt")

    cat = storage._categorize(model.footmarks)

    assert set(cat.put.values()) == {"mod.txt", "new.txt"}
    assert set(cat.backup.values()) == {"mod.txt", "del.txt"}
    assert set(cat.delete.values()) == {"del.txt"}
    assert cat.footmark_paths == cat.put.keys() | cat.backup.keys() | cat.delete.keys()
    assert cat.objects == {"mod.txt", "del.txt"}


def test_categorize_lists_a_noop_footmark_only_among_footmark_paths() -> None:
    """NOOP keeps the tokens aligned with the wire schema; it is never a PUT, DELETE, or BACKUP."""
    storage = MemoryTxnStorage()
    model = storage.transaction_begin()
    noop = f".x.txt.{model.footmarks[ObjectOperation.NOOP]}"
    storage.root.data[storage._obj_full(noop)] = b""

    cat = storage._categorize(model.footmarks)

    assert cat.footmark_paths == {noop}
    assert cat.put == cat.delete == cat.backup == {}
    assert storage._categorize([]).footmark_paths == storage._categorize().footmark_paths == set()


def test_categorize_refuses_a_token_beyond_the_operations() -> None:
    """A hand-built model with a surplus token fails loudly instead of dropping it."""
    model = MemoryTxnStorage().transaction_begin()
    with pytest.raises(ValueError, match="is not a valid ObjectOperation"):
        MemoryTxnStorage()._categorize([*model.footmarks, "EXTRA"])


# --------------------------------------------------------------------------- #
# Every public verb is classified for the overlay
# --------------------------------------------------------------------------- #

# Mutators stage through the overlay, so a peer sees nothing until commit and discard undoes them.
_OVERLAY_MUTATORS: dict[str, Callable[[TxnStorage], object]] = {
    "put": lambda s: s.put("a.txt", b"new"),
    "put_stream": lambda s: s.put_stream("a.txt", io.BytesIO(b"new")),
    "delete": lambda s: s.delete("a.txt"),
    "add_metadata": lambda s: s.add_metadata("a.txt", {"k": "w"}),
    "delete_metadata": lambda s: s.delete_metadata("a.txt"),
    "delete_metadata_fields": lambda s: s.delete_metadata_fields("a.txt", ["k"]),
    "copy": lambda s: s.copy("a.txt", "b.txt"),
    "move": lambda s: s.move("a.txt", "b.txt"),
}
# Reads see staged changes through the overlay → STORAGE.md#footmarks.
_OVERLAY_READS = {"exists", "get", "get_metadata", "get_stream", "list", "transfer"}
# Verbs the overlay does not stage, each for a stated reason.
_NOT_STAGED = {
    "get_type": "names the backend, no object",
    "format": "resets the root; refused or immediate by backend",
    "mkdir": "acts immediately → ROADMAP.md (staged mkdir / rmdir)",
    "rmdir": "acts immediately → ROADMAP.md (staged mkdir / rmdir)",
}


def test_storage_verbs_are_all_classified_for_the_overlay() -> None:
    """A new verb fails here until someone decides how a transaction sees it."""
    public = {name for name, _ in inspect.getmembers(Storage, inspect.isfunction) if name[0] != "_"}
    classified = [*_OVERLAY_MUTATORS, *_OVERLAY_READS, *_NOT_STAGED]
    assert sorted(classified) == sorted(public)


@pytest.mark.parametrize("verb", sorted(_OVERLAY_MUTATORS))
def test_mutator_is_staged_hidden_from_a_peer_and_undone_by_discard(
    storage: TxnStorage, peer: TxnStorage, verb: str
) -> None:
    storage.put("a.txt", b"orig", metadata={"k": "v"})

    def snapshot(s: TxnStorage) -> dict[str, tuple[bytes, dict[str, Any]]]:
        return {p: (s.get(p), s.get_metadata(p)) for p in sorted(s.list())}

    before = snapshot(peer)
    storage.transaction_begin()
    _OVERLAY_MUTATORS[verb](storage)

    # A mutator that did nothing would pass every check below.
    assert snapshot(storage) != before
    assert snapshot(peer) == before
    storage.transaction_discard()
    assert snapshot(storage) == snapshot(peer) == before
    assert storage.hidden_objects() == []
