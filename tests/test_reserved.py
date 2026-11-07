"""Tests for the reserved storage-root paths (txstore/_reserved.py).

Package and index objects share the storage keyspace with `.transactions.json`
and `.transactions.lock`, and a package path can arrive from a source repo's
`Filename:` field → VALIDATION.md § Filename safety.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from helpers.s3 import s3_bucket

from txstore import (
    ObjectOperation,
    StorageException,
    StorageFS,
    StorageS3,
    TransactionHistory,
    maybe_transaction,
)
from txstore._reserved import (
    HISTORY_FILENAME,
    LOCK_FILENAME,
    check_reserved_path,
    is_reserved_path,
)


@pytest.fixture
def storage(tmp_path: Path) -> StorageFS:
    return StorageFS(str(tmp_path / "repo"))


@pytest.fixture
def s3_storage(moto_server: str) -> Iterator[StorageS3]:
    with s3_bucket(moto_server) as s:
        yield s


# --------------------------------------------------------------------------- #
# is_reserved_path / check_reserved_path
# --------------------------------------------------------------------------- #


def test_is_reserved_path_matches_normalized_forms() -> None:
    assert is_reserved_path(LOCK_FILENAME) is True
    assert is_reserved_path(HISTORY_FILENAME) is True
    assert is_reserved_path(f"./{LOCK_FILENAME}") is True
    assert is_reserved_path(f"pool/../{HISTORY_FILENAME}") is True
    assert is_reserved_path(".Transactions.Lock") is True
    assert is_reserved_path("./.TRANSACTIONS.JSON") is True
    assert is_reserved_path(f"/{LOCK_FILENAME}") is True
    assert is_reserved_path(f"//{HISTORY_FILENAME}") is True


def test_is_reserved_path_allows_regular_objects() -> None:
    assert is_reserved_path("pool/m/mc/mc_1.0_all.deb") is False
    # Same basename, but not at the storage root.
    assert is_reserved_path(f"pool/{LOCK_FILENAME}") is False
    assert is_reserved_path("dists/main/Release") is False


def test_check_reserved_path_passes_regular_object() -> None:
    check_reserved_path("pool/m/mc/mc_1.0_all.deb")


def test_check_reserved_path_rejects_reserved_object() -> None:
    with pytest.raises(StorageException, match="reserved storage path"):
        check_reserved_path(LOCK_FILENAME)


# --------------------------------------------------------------------------- #
# Enforcement on the FS backend
# --------------------------------------------------------------------------- #


def test_fs_put_stream_refuses_reserved_path(storage: StorageFS) -> None:
    with pytest.raises(StorageException, match="reserved storage path"):
        storage.put_stream(LOCK_FILENAME, io.BytesIO(b"pwned"))
    assert not storage.exists(LOCK_FILENAME)


@pytest.mark.parametrize("method", ["mkdir", "rmdir"])
@pytest.mark.parametrize("name", [LOCK_FILENAME, HISTORY_FILENAME, ".Transactions.Lock"])
def test_fs_directory_calls_refuse_reserved_path(
    storage: StorageFS, method: str, name: str
) -> None:
    """A directory at `.transactions.lock` would make every later lock open fail."""
    TransactionHistory(storage).save([])
    with pytest.raises(StorageException, match="reserved storage path"):
        getattr(storage, method)(name)
    assert (Path(storage.base_dir) / HISTORY_FILENAME).is_file()
    assert not (Path(storage.base_dir) / LOCK_FILENAME).is_dir()


def test_fs_delete_refuses_reserved_path(storage: StorageFS) -> None:
    TransactionHistory(storage).save([])
    with pytest.raises(StorageException, match="reserved storage path"):
        storage.delete(HISTORY_FILENAME)
    assert storage.exists(HISTORY_FILENAME)


def test_fs_transfer_cannot_replace_live_lock(tmp_path: Path, storage: StorageFS) -> None:
    """A source object named `.transactions.lock` must not reach the live lock.

    Without the guard the PUT was staged as a footmark and `transaction_commit`
    copied it onto the lock, corrupting the body a peer then treats as
    breakable — two writers inside one critical section.
    """
    source = StorageFS(str(tmp_path / "src"))
    source.put_stream(f"pool/{LOCK_FILENAME}", io.BytesIO(b"pwned"))

    with pytest.raises(StorageException, match="reserved storage path"):
        with maybe_transaction(storage):
            source.transfer(f"pool/{LOCK_FILENAME}", storage, path_dst=LOCK_FILENAME)

    # The transaction was discarded and the lock file is the empty flock target.
    assert (Path(storage.base_dir) / LOCK_FILENAME).read_bytes() == b""
    assert TransactionHistory(storage).load() == []


def test_fs_put_refuses_reserved_path(storage: StorageFS) -> None:
    """`put` is a `put_stream` of the bytes, so it is guarded too."""
    with pytest.raises(StorageException, match="reserved storage path"):
        storage.put(HISTORY_FILENAME, b"[]")


def test_fs_history_writer_is_unaffected_by_the_guard(storage: StorageFS) -> None:
    """`TransactionHistory.save` writes the reserved path through `_obj_put_reserved`."""
    with maybe_transaction(storage):
        storage.put("pool/a.deb", b"a")
    assert [m.id for m in TransactionHistory(storage).load()] == [1]


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "in-transaction"])
@pytest.mark.parametrize("operation", ["copy", "move"])
@pytest.mark.parametrize("dst", ["", ".", "pool/.."])
def test_fs_transfer_to_the_root_cannot_replace_the_history(
    storage: StorageFS, operation: str, dst: str, in_transaction: bool
) -> None:
    """A root destination would land the source's basename, here the history, at the root."""
    TransactionHistory(storage).save([])
    storage.mkdir("x")
    (Path(storage.base_dir) / "x" / HISTORY_FILENAME).write_bytes(b"GARBAGE")
    if in_transaction:
        storage.transaction_begin()

    with pytest.raises(StorageException, match="storage root"):
        getattr(storage, operation)(f"x/{HISTORY_FILENAME}", dst)

    assert (Path(storage.base_dir) / HISTORY_FILENAME).read_bytes() == b"[]"


@pytest.mark.parametrize("path", ["", ".", "pool/.."])
@pytest.mark.parametrize("method", ["mkdir", "rmdir"])
def test_fs_directory_calls_refuse_the_root(storage: StorageFS, method: str, path: str) -> None:
    """`rmdir` of the root would delete the history and the lock with everything else."""
    storage.put("pool/a.deb", b"a")
    with pytest.raises(StorageException, match="storage root"):
        getattr(storage, method)(path)
    assert storage.exists("pool/a.deb")


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
@pytest.mark.parametrize("verb", ["copy", "move"])
def test_fs_copy_or_move_refuses_a_directory_destination(
    storage: StorageFS, verb: str, in_transaction: bool
) -> None:
    """The destination is written as a file; it never enters or replaces a directory."""
    storage.put("a.deb", b"a")
    storage.mkdir("pool")
    if in_transaction:
        storage.transaction_begin()
    with pytest.raises(StorageException, match=f"{verb} onto a directory"):
        getattr(storage, verb)("a.deb", "pool")
    assert storage.exists("a.deb")
    assert not (Path(storage.base_dir) / "pool" / "a.deb").exists()


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
def test_fs_move_refuses_a_directory_source(storage: StorageFS, in_transaction: bool) -> None:
    """Outside a transaction a rename would carry the whole tree, footmarks included."""
    storage.put("pool/a.deb", b"a")
    if in_transaction:
        storage.transaction_begin()
    with pytest.raises(StorageException, match="move a directory"):
        storage.move("pool", "moved")
    assert storage.get("pool/a.deb") == b"a"
    assert not (Path(storage.base_dir) / "moved").exists()


@pytest.mark.parametrize("name", [LOCK_FILENAME, HISTORY_FILENAME])
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda s, p: s.put(p, b"pwned"), id="put"),
        pytest.param(lambda s, p: s.copy("a.deb", p), id="copy"),
        pytest.param(lambda s, p: s.move("a.deb", p), id="move"),
        pytest.param(lambda s, p: s.delete(p), id="delete"),
        pytest.param(lambda s, p: s.add_metadata(p, {"k": "v"}), id="add_metadata"),
    ],
)
def test_fs_mutators_refuse_a_reserved_path_reached_through_a_symlink(
    storage: StorageFS, call: Callable[[StorageFS, str], None], name: str
) -> None:
    """`d -> .` makes `d/.transactions.lock` the real lock while its path names no reserved one."""
    TransactionHistory(storage).save([])
    (Path(storage.base_dir) / LOCK_FILENAME).write_bytes(b"")
    storage.put("a.deb", b"a")
    (Path(storage.base_dir) / "d").symlink_to(".")
    before = (Path(storage.base_dir) / name).read_bytes()

    with pytest.raises(StorageException, match="reserved storage path"):
        call(storage, f"d/{name}")

    assert (Path(storage.base_dir) / name).read_bytes() == before


def test_fs_backup_replaces_a_planted_footmark_link_instead_of_writing_through_it(
    storage: StorageFS, tmp_path: Path
) -> None:
    """A dangling link at the BACKUP footmark path must not carry the original outside the root."""
    storage.put("a.deb", b"original")
    model = storage.transaction_begin()
    outside = tmp_path / "outside"
    backup = storage._add_footmark(storage._obj_full("a.deb"), ObjectOperation.BACKUP)
    Path(backup).symlink_to(outside)

    storage.put("a.deb", b"new")

    assert not outside.exists()
    assert not Path(backup).is_symlink()
    assert Path(backup).read_bytes() == b"original"
    assert model.id == storage.transaction_id


# --------------------------------------------------------------------------- #
# Enforcement on the S3 backend
# --------------------------------------------------------------------------- #


def test_s3_put_stream_refuses_reserved_path(s3_storage: StorageS3) -> None:
    with pytest.raises(StorageException, match="reserved storage path"):
        s3_storage.put_stream(HISTORY_FILENAME, io.BytesIO(b"[]"))
    assert not s3_storage.exists(HISTORY_FILENAME)


@pytest.mark.parametrize("prefix", ["/", "//"], ids=["leading-slash", "double-slash"])
def test_s3_put_refuses_a_slash_prefixed_reserved_path(s3_storage: StorageS3, prefix: str) -> None:
    """A gateway that collapses slashes would map the key onto the real lock."""
    with pytest.raises(StorageException, match="reserved storage path"):
        s3_storage.put(f"{prefix}{LOCK_FILENAME}", b"pwned")


def test_s3_delete_refuses_reserved_path(s3_storage: StorageS3) -> None:
    TransactionHistory(s3_storage).save([])
    with pytest.raises(StorageException, match="reserved storage path"):
        s3_storage.delete(HISTORY_FILENAME)
    assert json.loads(s3_storage.get(HISTORY_FILENAME)) == []
