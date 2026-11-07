"""Contract test for `StorageFS` (filesystem backend).

Required cases → `TESTING.md#required-coverage`. All tests run against a `tmp_path`-rooted
instance, with no network; an xattr assertion skips through `helpers.backends` where the
filesystem stores none.
"""

from __future__ import annotations

import errno
import fcntl
import io
import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
import xattr
from helpers.backends import (
    FOREIGN_XATTR,
    make_setgid,
    set_foreign_xattr,
    skip_without_user_xattrs,
)
from helpers.storage import FailingStream, Pipe, deadline
from urllib3.exceptions import ProtocolError

from txstore import (
    StorageException,
    StorageFS,
    StorageNotFoundException,
    StorageNull,
    StorageType,
    TransactionHistory,
)


@pytest.fixture
def storage(tmp_path: Path) -> StorageFS:
    return StorageFS(str(tmp_path / "repo"))


# --------------------------------------------------------------------------- #
# Init / type
# --------------------------------------------------------------------------- #


def test_storage_fs_init_creates_base_dir(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "repo"
    StorageFS(str(target), auto_mkdir=True)
    assert target.is_dir()


def test_storage_fs_init_create_base_false_does_not_create(tmp_path: Path) -> None:
    target = tmp_path / "missing"
    StorageFS(str(target), create_base=False)
    assert not target.exists()


def test_storage_fs_init_auto_mkdir_false_still_creates_base_dir(tmp_path: Path) -> None:
    """`auto_mkdir` governs written objects' parents only, not the root."""
    target = tmp_path / "missing"
    StorageFS(str(target), auto_mkdir=False)
    assert target.is_dir()


def test_storage_fs_init_wraps_a_failing_base_dir_creation(tmp_path: Path) -> None:
    """One `StorageException` handler covers an unusable root, as for every backend."""
    blocker = tmp_path / "file"
    blocker.write_bytes(b"")
    with pytest.raises(StorageException, match="Failed to create base directory") as excinfo:
        StorageFS(str(blocker / "repo"))
    assert isinstance(excinfo.value.__cause__, OSError)


def test_storage_fs_create_base_false_reads_as_empty_storage(tmp_path: Path) -> None:
    """A missing root degrades to "empty", never a raw `OSError`."""
    storage = StorageFS(str(tmp_path / "missing"), create_base=False)
    assert storage.list() == []
    assert not storage.exists("foo.txt")
    with pytest.raises(StorageNotFoundException):
        storage.get("foo.txt")


def test_storage_fs_get_type(storage: StorageFS) -> None:
    assert storage.get_type() is StorageType.fs


def test_storage_fs_repr_contains_base_dir(storage: StorageFS) -> None:
    assert storage.base_dir in repr(storage)


# --------------------------------------------------------------------------- #
# put / get / exists
# --------------------------------------------------------------------------- #


def test_storage_fs_put_then_get_roundtrip(storage: StorageFS) -> None:
    storage.put("foo.txt", b"hello")
    assert storage.get("foo.txt") == b"hello"
    assert storage.exists("foo.txt")


def test_storage_fs_put_creates_nested_dirs(storage: StorageFS) -> None:
    storage.put("a/b/c/file.bin", b"x")
    assert storage.exists("a/b/c/file.bin")


def test_storage_fs_get_missing_raises(storage: StorageFS) -> None:
    with pytest.raises(StorageNotFoundException, match="File not found"):
        storage.get("nope.txt")
    assert storage.metrics.read_errors_total == 1


def test_storage_fs_get_stream_missing_raises_not_found(storage: StorageFS) -> None:
    with pytest.raises(StorageNotFoundException, match="File not found"):
        storage.get_stream("nope.txt")
    assert storage.metrics.read_errors_total == 1
    assert storage.metrics.read_objects_total == 0


def test_storage_fs_exists_false_for_missing(storage: StorageFS) -> None:
    assert not storage.exists("nope.txt")


def test_storage_fs_exists_false_under_a_file(storage: StorageFS) -> None:
    storage.put("f", b"x")
    assert not storage.exists("f/child")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize("verb", ["exists", "delete"])
def test_storage_fs_unreadable_parent_is_a_backend_error_not_a_missing_object(
    storage: StorageFS, verb: str
) -> None:
    """Only "not found" reads as missing → STORAGE.md#backend-primitives."""
    storage.put("locked/x", b"x")
    locked = Path(storage.base_dir, "locked")
    locked.chmod(0o000)
    try:
        with pytest.raises(StorageException) as info:
            getattr(storage, verb)("locked/x")
    finally:
        locked.chmod(0o755)
    assert not isinstance(info.value, StorageNotFoundException)


def test_storage_fs_put_metrics(storage: StorageFS) -> None:
    storage.put("a.txt", b"abc")
    assert storage.metrics.write_objects_total == 1
    assert storage.metrics.write_bytes == 3


def test_storage_fs_get_metrics(storage: StorageFS) -> None:
    storage.put("a.txt", b"abc")
    storage.get("a.txt")
    assert storage.metrics.read_objects_total == 1
    assert storage.metrics.read_bytes == 3


# --------------------------------------------------------------------------- #
# put_stream / get_stream
# --------------------------------------------------------------------------- #


def test_storage_fs_put_stream_then_get_stream(storage: StorageFS) -> None:
    storage.put_stream("s.bin", io.BytesIO(b"streamed"))
    with storage.get_stream("s.bin") as fp:
        assert fp.read() == b"streamed"


@pytest.mark.parametrize("read", ["get", "get_stream"])
def test_storage_fs_read_refuses_a_fifo_object(storage: StorageFS, read: str) -> None:
    """A FIFO planted as an object fails the read instead of blocking the reader."""
    Path(storage.base_dir).mkdir(parents=True, exist_ok=True)
    os.mkfifo(Path(storage.base_dir, "obj"))

    with deadline(5), pytest.raises(StorageException, match="Not a regular file"):
        getattr(storage, read)("obj")


def test_storage_fs_history_load_refuses_a_fifo_history_file(storage: StorageFS) -> None:
    """A FIFO planted as the history would otherwise block every loader."""
    Path(storage.base_dir).mkdir(parents=True, exist_ok=True)
    os.mkfifo(Path(storage.base_dir, ".transactions.json"))

    with deadline(5), pytest.raises(StorageException, match="Not a regular file"):
        TransactionHistory(storage).load()


def test_storage_fs_history_lock_refuses_a_fifo_lock_file(storage: StorageFS) -> None:
    """The lock never reads its file, but a FIFO there is refused by rule, not by accident."""
    Path(storage.base_dir).mkdir(parents=True, exist_ok=True)
    os.mkfifo(Path(storage.base_dir, ".transactions.lock"))

    with deadline(5), pytest.raises(StorageException, match="Not a regular file"):
        with storage._obj_lock():
            pass


def test_storage_fs_get_stream_returns_a_blocking_handle(storage: StorageFS) -> None:
    """`regular_opener`'s non-blocking check must not leak into the stream a caller reads."""
    storage.put("s.bin", b"x")
    with storage.get_stream("s.bin") as fp:
        assert os.get_blocking(fp.fileno())


@pytest.mark.parametrize(
    "error",
    [
        OSError("simulated mid-stream failure"),
        # A dropped connection under an HTTP transfer: not an OSError.
        ProtocolError("Connection broken: IncompleteRead"),
        ValueError("caller stream bug"),
    ],
    ids=["oserror", "urllib3", "other"],
)
def test_storage_fs_put_stream_failure_leaves_no_partial_file(
    storage: StorageFS, error: Exception
) -> None:
    stream = FailingStream(error=error)
    with pytest.raises(StorageException):
        storage.put_stream("out.bin", stream)  # type: ignore[arg-type]

    assert not storage.exists("out.bin")
    assert storage.metrics.write_errors_total == 1
    # No .tmp. leftovers anywhere under base_dir.
    leftover = list(Path(storage.base_dir).rglob("*.tmp.*"))
    assert leftover == []


def test_storage_fs_put_stream_failure_in_txn_does_not_corrupt_original(
    storage: StorageFS,
) -> None:
    storage.put("orig.bin", b"good-data")
    txn = storage.transaction_begin()

    stream = FailingStream(b"bad")
    with pytest.raises(StorageException):
        storage.put_stream("orig.bin", stream)  # type: ignore[arg-type]

    # In-txn read should still return the original.
    assert storage.get("orig.bin") == b"good-data"

    # Commit must not apply any corrupt PUT footmark.
    storage.transaction_commit()
    assert storage.get("orig.bin") == b"good-data"

    # No leftover footmarks.
    leftover = list(Path(storage.base_dir).rglob("*.tmp.*"))
    assert leftover == []
    del txn


def test_storage_fs_put_stream_acl_chmod_applied(storage: StorageFS) -> None:
    storage.put_stream("f.txt", io.BytesIO(b"data"), acl="0644")
    mode = (Path(storage.base_dir) / "f.txt").stat().st_mode & 0o777
    assert mode == 0o644
    leftover = list(Path(storage.base_dir).rglob("*.tmp.*"))
    assert leftover == []


@pytest.mark.parametrize(
    ("acl", "expected"),
    [
        pytest.param("4755", 0o755, id="setuid-masked"),
        pytest.param("0600", 0o600, id="private"),
        pytest.param("999", None, id="not-octal-ignored"),
        pytest.param("public-read", None, id="canned-acl-ignored"),
    ],
)
def test_storage_fs_put_stream_acl_sets_only_permission_bits(
    storage: StorageFS, acl: str, expected: int | None
) -> None:
    previous = os.umask(0)  # A fixed umask, so a wrong creation mode cannot hide under it.
    try:
        storage.put_stream("f.txt", io.BytesIO(b"data"), acl=acl)
    finally:
        os.umask(previous)

    mode = (Path(storage.base_dir) / "f.txt").stat().st_mode & 0o7777
    assert mode == (0o666 if expected is None else expected)


def test_storage_fs_put_stream_refuses_a_planted_temp_symlink(
    storage: StorageFS, tmp_path: Path
) -> None:
    """A planted link at the temp path fails `O_EXCL` before it could be followed.

    `O_NOFOLLOW` stays as defence in depth; with `O_EXCL` set it is never the guard that fires.
    """
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep me")
    with patch("txstore._fs_paths.secrets.token_hex", return_value="guess"):
        (Path(storage.base_dir) / ".f.txt.tmp.guess").symlink_to(victim)
        with pytest.raises(StorageException, match="Error writing") as raised:
            storage.put("f.txt", b"attack")

    assert isinstance(raised.value.__cause__, FileExistsError)

    assert victim.read_bytes() == b"keep me"
    assert not storage.exists("f.txt")


@pytest.mark.parametrize("seekable", [True, False], ids=["seekable", "pipe"])
def test_storage_fs_put_stream_counts_the_written_bytes(storage: StorageFS, seekable: bool) -> None:
    body = b"x" * 5000
    stream = io.BytesIO(body) if seekable else Pipe(body)

    storage.put_stream("f.bin", stream)  # type: ignore[arg-type]

    assert storage.metrics.write_bytes == 5000


@pytest.mark.parametrize(
    ("error", "message"),
    [
        pytest.param(FileNotFoundError("gone"), "Parent directory not found", id="missing-parent"),
        pytest.param(PermissionError("denied"), "Permission denied", id="permission"),
        pytest.param(OSError("disk full"), "Error writing", id="other"),
    ],
)
def test_storage_fs_put_maps_a_rename_failure_and_cleans_up(
    storage: StorageFS, error: OSError, message: str
) -> None:
    with (
        patch("txstore.storage_fs.os.rename", side_effect=error),
        pytest.raises(StorageException, match=message) as exc_info,
    ):
        storage.put("f.txt", b"data")

    assert exc_info.value.__cause__ is error
    assert storage.metrics.write_errors_total == 1
    assert list(Path(storage.base_dir).rglob("*.tmp.*")) == []


@pytest.mark.parametrize(
    ("error", "expected", "message"),
    [
        pytest.param(
            FileNotFoundError("gone"), StorageNotFoundException, "File not found", id="missing"
        ),
        pytest.param(
            PermissionError("denied"), StorageException, "Permission denied", id="permission"
        ),
        pytest.param(OSError("io"), StorageException, "Error opening", id="other"),
    ],
)
def test_storage_fs_get_stream_maps_an_open_failure(
    storage: StorageFS, error: OSError, expected: type[StorageException], message: str
) -> None:
    storage.put("f.txt", b"data")
    with (
        patch("builtins.open", side_effect=error),
        pytest.raises(expected, match=message),
    ):
        storage.get_stream("f.txt")
    assert storage.metrics.read_errors_total == 1


@pytest.mark.parametrize("verb", ["get", "get_stream"])
def test_storage_fs_read_refuses_a_link_swapped_in_after_resolution(
    storage: StorageFS, tmp_path: Path, verb: str
) -> None:
    """A writer in the root cannot redirect a read outside it → `VALIDATION.md#filename-safety`."""
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"SECRET")
    storage.put("f.txt", b"data")
    resolve = storage._read_target

    def resolve_then_swap(path: str) -> str:
        full = resolve(path)
        os.remove(full)
        os.symlink(secret, full)
        return full

    with (
        patch.object(storage, "_read_target", side_effect=resolve_then_swap),
        pytest.raises(StorageException),
    ):
        getattr(storage, verb)("f.txt")
    assert storage.metrics.read_errors_total == 1


def test_storage_fs_get_maps_a_read_failure(storage: StorageFS) -> None:
    storage.put("f.txt", b"data")
    broken = io.BytesIO()
    broken.read = lambda *_: (_ for _ in ()).throw(OSError("io"))  # type: ignore[method-assign]
    with (
        patch.object(storage, "get_stream", return_value=broken),
        pytest.raises(StorageException, match="Error reading f.txt"),
    ):
        storage.get("f.txt")
    assert storage.metrics.read_errors_total == 1


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda s: s.delete_metadata("f.txt"), id="delete_metadata"),
        pytest.param(lambda s: s.delete_metadata_fields("f.txt", ["k"]), id="fields"),
    ],
)
def test_storage_fs_metadata_removal_maps_an_xattr_failure(
    storage: StorageFS, call: Callable[[StorageFS], None]
) -> None:
    storage.put("f.txt", b"data")
    with (
        patch("txstore._fs_paths.xattr.get_all", return_value=[(b"k", b"v")]),
        patch("txstore._fs_paths.xattr.remove", side_effect=OSError("io")),
        pytest.raises(StorageException, match="Failed to delete metadata"),
    ):
        call(storage)


@pytest.mark.parametrize("err", [errno.ENOTSUP, errno.EOPNOTSUPP], ids=["ENOTSUP", "EOPNOTSUPP"])
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda s: s.delete_metadata("f.txt"), id="delete_metadata"),
        pytest.param(lambda s: s.delete_metadata_fields("f.txt", ["k"]), id="fields"),
    ],
)
def test_storage_fs_metadata_removal_skips_a_filesystem_without_xattrs(
    storage: StorageFS,
    call: Callable[[StorageFS], None],
    err: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No metadata can exist there, so none is left to remove → `STORAGE.md#storage-abi`."""
    storage.put("f.txt", b"data")
    with (
        patch("txstore._fs_paths.xattr.get_all", side_effect=OSError(err, "nope")),
        patch("txstore._fs_paths.xattr.remove", side_effect=OSError(err, "nope")),
        caplog.at_level("WARNING"),
    ):
        call(storage)
    assert "Failed to delete xattrs" in caplog.text


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda s: s.add_metadata("f.txt", {"a\x00b": "v"}), id="add"),
        pytest.param(lambda s: s.delete_metadata_fields("f.txt", ["\udcff"]), id="fields"),
    ],
)
def test_storage_fs_metadata_change_refuses_an_unencodable_key(
    storage: StorageFS, call: Callable[[StorageFS], None]
) -> None:
    storage.put("f.txt", b"data")
    with pytest.raises(StorageException, match="Invalid xattr"):
        call(storage)


def test_storage_fs_put_keeps_the_object_when_a_key_is_unencodable(
    storage: StorageFS, caplog: pytest.LogCaptureFixture
) -> None:
    """The xattr failure comes after the rename: logged, and the write still counted."""
    with caplog.at_level("WARNING"):
        storage.put("f.txt", b"data", metadata={"a\x00b": "v"})
    assert storage.get("f.txt") == b"data"
    assert storage.metrics.write_objects_total == 1
    assert "Invalid xattr" in caplog.text


def test_storage_fs_add_metadata_maps_an_xattr_failure(storage: StorageFS) -> None:
    storage.put("f.txt", b"data")
    with (
        patch("txstore.storage_fs.set_xattrs", side_effect=OSError(errno.EACCES, "denied")),
        pytest.raises(StorageException, match="Failed to set metadata"),
    ):
        storage.add_metadata("f.txt", {"k": "v"})


def test_storage_fs_add_metadata_skips_a_filesystem_without_xattrs(
    storage: StorageFS, caplog: pytest.LogCaptureFixture
) -> None:
    """Mirrors `get_metadata`, which yields `{}` there → `STORAGE.md#storage-abi`."""
    storage.put("f.txt", b"data")
    with (
        patch("txstore.storage_fs.set_xattrs", side_effect=OSError(errno.ENOTSUP, "nope")),
        caplog.at_level("WARNING"),
    ):
        storage.add_metadata("f.txt", {"k": "v"})
    assert "Failed to set xattr" in caplog.text


def test_storage_fs_put_keeps_the_object_when_an_xattr_fails(
    storage: StorageFS, caplog: pytest.LogCaptureFixture
) -> None:
    """The bytes are already in place, so metadata on a write is best-effort."""
    with (
        patch("txstore.storage_fs.set_xattrs", side_effect=OSError(errno.EACCES, "denied")),
        caplog.at_level("WARNING"),
    ):
        storage.put("f.txt", b"data", metadata={"k": "v"})
    assert storage.get("f.txt") == b"data"
    assert "Failed to set xattr" in caplog.text


def test_storage_fs_move_maps_a_rename_failure(storage: StorageFS) -> None:
    storage.put("a.txt", b"data")
    with (
        patch("txstore.storage_fs.os.replace", side_effect=OSError(errno.EACCES, "denied")),
        pytest.raises(StorageException, match="Failed to move"),
    ):
        storage.move("a.txt", "b.txt")
    assert storage.exists("a.txt")


def test_storage_fs_move_across_devices_copies_then_deletes(storage: StorageFS) -> None:
    """Across devices a move is a copy, so it lands only the permission bits."""
    storage.put("a.txt", b"data")
    source = Path(storage.base_dir, "a.txt")
    make_setgid(source)
    real_replace = os.replace
    calls: list[str] = []

    def rename_across_devices(src: str, dst: str) -> None:
        """Refuse the move's rename; the copy's own temp-file replace still works."""
        calls.append(src)
        if len(calls) == 1:
            raise OSError(errno.EXDEV, "cross-device")
        real_replace(src, dst)

    with patch("txstore.storage_fs.os.replace", side_effect=rename_across_devices):
        storage.move("a.txt", "b.txt")
    assert storage.get("b.txt") == b"data"
    assert not storage.exists("a.txt")
    assert Path(storage.base_dir, "b.txt").stat().st_mode & 0o7777 == 0o755


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_storage_fs_failed_move_leaves_the_destination_untouched(storage: StorageFS) -> None:
    """`shutil.move` fell back to copying over the destination when the rename was refused."""
    storage.put("ro/f.txt", b"new")
    storage.put("g.txt", b"old")
    ro = Path(storage.base_dir, "ro")
    ro.chmod(0o555)
    try:
        with pytest.raises(StorageException, match="Failed to move"):
            storage.move("ro/f.txt", "g.txt")
    finally:
        ro.chmod(0o755)
    assert storage.get("g.txt") == b"old"
    assert storage.get("ro/f.txt") == b"new"


def test_storage_fs_history_lock_unlock_failure_is_logged_not_raised(
    storage: StorageFS, caplog: pytest.LogCaptureFixture
) -> None:
    real_flock = fcntl.flock

    def flock(fd: int, op: int) -> None:
        if op == fcntl.LOCK_UN:
            raise OSError("nfs")
        real_flock(fd, op)

    with patch("txstore.storage_fs.fcntl.flock", side_effect=flock), storage._obj_lock():
        pass

    assert "Failed to unlock FS history lock" in caplog.text


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


def test_storage_fs_list_excludes_hidden_only(storage: StorageFS) -> None:
    storage.put("a.txt", b"a")
    storage.put("b/c.txt", b"c")
    storage.put("trust.CA", b"x")
    # Only dotfiles are hidden; an arbitrary suffix is a regular object.
    (Path(storage.base_dir) / ".hidden").write_bytes(b"x")
    assert set(storage.list()) == {"a.txt", "b/c.txt", "trust.CA"}


# --------------------------------------------------------------------------- #
# delete / delete_metadata
# --------------------------------------------------------------------------- #


def test_storage_fs_delete_removes_file(storage: StorageFS) -> None:
    storage.put("d.txt", b"x")
    storage.delete("d.txt")
    assert not storage.exists("d.txt")


# --------------------------------------------------------------------------- #
# copy / move
# --------------------------------------------------------------------------- #


def test_storage_fs_copy_creates_destination(storage: StorageFS) -> None:
    storage.put("src.txt", b"data")
    storage.copy("src.txt", "dst/sub/dst.txt")
    assert storage.get("dst/sub/dst.txt") == b"data"
    assert storage.exists("src.txt")


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
@pytest.mark.parametrize("verb", ["copy", "move"])
def test_storage_fs_copy_or_move_of_a_missing_source_creates_no_parent(
    storage: StorageFS, verb: str, in_transaction: bool
) -> None:
    """The source is checked before the destination's parent is created."""
    if in_transaction:
        storage.transaction_begin()
    before = sorted(Path(storage.base_dir).rglob("*"))
    with pytest.raises(StorageNotFoundException):
        getattr(storage, verb)("absent.txt", "a/b/c.txt")
    assert sorted(Path(storage.base_dir).rglob("*")) == before


@pytest.mark.parametrize("verb", ["copy", "move"])
def test_storage_fs_copy_or_move_of_a_missing_source_onto_a_directory_refuses_the_directory(
    storage: StorageFS, verb: str
) -> None:
    """The directory refusal is a path refusal, so it precedes the missing-source check."""
    storage.mkdir("d")
    with pytest.raises(StorageException, match="onto a directory") as info:
        getattr(storage, verb)("absent.txt", "d")
    assert not isinstance(info.value, StorageNotFoundException)


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
@pytest.mark.parametrize("verb", ["copy", "move"])
def test_storage_fs_copy_and_move_check_the_source_once(
    storage: StorageFS, verb: str, in_transaction: bool
) -> None:
    """The mixin resolves the source once; the copy leg of a move does not resolve it again."""
    storage.put("src.txt", b"data")
    if in_transaction:
        storage.transaction_begin()
    with patch.object(storage, "_existing_source", wraps=storage._existing_source) as source:
        getattr(storage, verb)("src.txt", "dst.txt")
    assert source.call_count == 1


@pytest.mark.parametrize(
    ("verb", "in_transaction", "expected"),
    [
        pytest.param("copy", False, {"dst.txt": 2}, id="copy-direct"),
        pytest.param("copy", True, {"dst.txt": 2}, id="copy-staged"),
        pytest.param("move", False, {"src.txt": 1, "dst.txt": 1}, id="move-direct"),
        pytest.param("move", True, {"src.txt": 2, "dst.txt": 2}, id="move-staged"),
    ],
)
def test_storage_fs_copy_and_move_leave_the_reserved_checks_to_the_mixin(
    storage: StorageFS, verb: str, in_transaction: bool, expected: dict[str, int]
) -> None:
    """FS adds its directory refusals through `_obj_check_transfer`, not its own reserved checks;
    the remaining repeats are the staging steps' own guards."""
    storage.put("src.txt", b"data")
    if in_transaction:
        storage.transaction_begin()
    with patch.object(storage, "_obj_check_reserved", wraps=storage._obj_check_reserved) as check:
        getattr(storage, verb)("src.txt", "dst.txt")
    assert Counter(call.args[0] for call in check.call_args_list) == expected


# --------------------------------------------------------------------------- #
# mkdir / rmdir
# --------------------------------------------------------------------------- #


def test_storage_fs_mkdir_creates_directory(storage: StorageFS) -> None:
    storage.mkdir("new/dir")
    assert (Path(storage.base_dir) / "new" / "dir").is_dir()


def test_storage_fs_mkdir_idempotent(storage: StorageFS) -> None:
    storage.mkdir("a")
    storage.mkdir("a")  # must not raise


def test_storage_fs_mkdir_over_a_file_raises(storage: StorageFS) -> None:
    """A file is not a directory; `mkdir` must not report success over it → STORAGE.md."""
    storage.put("f", b"x")
    with pytest.raises(StorageException, match="Failed to create directory"):
        storage.mkdir("f")
    assert storage.get("f") == b"x"


def test_storage_fs_mkdir_under_a_file_raises(storage: StorageFS) -> None:
    storage.put("f", b"x")
    with pytest.raises(StorageException, match="Failed to create directory"):
        storage.mkdir("f/sub")


def test_storage_fs_rmdir_removes_recursively(storage: StorageFS) -> None:
    storage.put("d/f.txt", b"x")
    storage.rmdir("d")
    assert not (Path(storage.base_dir) / "d").exists()


def test_storage_fs_rmdir_missing_raises(storage: StorageFS) -> None:
    with pytest.raises(StorageException):
        storage.rmdir("nope")


# --------------------------------------------------------------------------- #
# format() is intentionally a StorageException
# --------------------------------------------------------------------------- #


def test_storage_fs_format_raises_storage_exception(storage: StorageFS) -> None:
    with pytest.raises(StorageException, match="delete the base directory"):
        storage.format()


# --------------------------------------------------------------------------- #
# Path traversal defence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["../escape.txt", "a/../../escape", "sub/../../../etc/passwd"])
def test_storage_fs_rejects_traversal(storage: StorageFS, bad: str) -> None:
    with pytest.raises(StorageException, match="Path traversal"):
        storage.put(bad, b"x")


_ESCAPE = "../escape.txt"
# Replaced by the absolute path of the file outside the root.
_ABSOLUTE = "<absolute>"

_EVERY_PATH_ARGUMENT = [
    pytest.param(lambda s, p: s.get(p), id="get"),
    pytest.param(lambda s, p: s.get_stream(p), id="get_stream"),
    pytest.param(lambda s, p: s.exists(p), id="exists"),
    pytest.param(lambda s, p: s.get_metadata(p), id="get_metadata"),
    pytest.param(lambda s, p: s.put(p, b"x"), id="put"),
    pytest.param(lambda s, p: s.put_stream(p, io.BytesIO(b"x")), id="put_stream"),
    pytest.param(lambda s, p: s.add_metadata(p, {"k": "v"}), id="add_metadata"),
    pytest.param(lambda s, p: s.delete(p), id="delete"),
    pytest.param(lambda s, p: s.delete_metadata(p), id="delete_metadata"),
    pytest.param(lambda s, p: s.delete_metadata_fields(p, ["k"]), id="delete_fields"),
    pytest.param(lambda s, p: s.copy(p, "dst.txt"), id="copy-source"),
    pytest.param(lambda s, p: s.copy("ok.txt", p), id="copy-destination"),
    pytest.param(lambda s, p: s.move(p, "dst.txt"), id="move-source"),
    pytest.param(lambda s, p: s.move("ok.txt", p), id="move-destination"),
    pytest.param(lambda s, p: s.mkdir(p), id="mkdir"),
    pytest.param(lambda s, p: s.rmdir(p), id="rmdir"),
    pytest.param(lambda s, p: s.transfer(p, StorageNull()), id="transfer"),
]


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
@pytest.mark.parametrize(
    ("bad", "match"),
    [
        (_ESCAPE, "Path traversal"),
        (_ABSOLUTE, "Absolute paths"),
        ("out/escape.txt", "Path traversal"),
        ("a\x00b", "Invalid path"),
        ("a\ud800b", "Invalid path"),
    ],
    ids=["traversal", "absolute", "symlink-escape", "nul", "lone-surrogate"],
)
@pytest.mark.parametrize("call", _EVERY_PATH_ARGUMENT)
def test_storage_fs_refuses_a_bad_path_in_every_path_argument(
    storage: StorageFS,
    call: Callable[[StorageFS, str], object],
    bad: str,
    match: str,
    in_transaction: bool,
) -> None:
    """Every verb refuses an escaping or unencodable path, never as a raw ValueError.

    `_obj_full` does not confine, and the helpers past the path check raise the OS's own
    error for a path it cannot take, so the check must come first on every verb. `out` is a
    symlink inside the root to the directory above it.
    """
    escape = Path(storage.base_dir).parent / "escape.txt"
    escape.write_bytes(b"outside")
    (Path(storage.base_dir) / "out").symlink_to(escape.parent, target_is_directory=True)
    if bad == _ABSOLUTE:
        bad = str(escape)
    storage.put("ok.txt", b"x")
    if in_transaction:
        storage.transaction_begin()

    with pytest.raises(StorageException, match=match):
        call(storage, bad)

    assert escape.read_bytes() == b"outside"


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
def test_storage_fs_delete_metadata_fields_empty_keys_skips_the_path_check(
    storage: StorageFS, in_transaction: bool
) -> None:
    """An empty `keys` list returns before any path check, as on S3 → `STORAGE.md#storage-abi`."""
    if in_transaction:
        storage.transaction_begin()
    storage.delete_metadata_fields(_ESCAPE, [])


def test_storage_fs_rejects_absolute_path(storage: StorageFS) -> None:
    with pytest.raises(StorageException, match="Absolute paths"):
        storage.put("/abs/path", b"x")


# --------------------------------------------------------------------------- #
# transfer
# --------------------------------------------------------------------------- #


def test_storage_fs_transfer_between_two_instances(tmp_path: Path) -> None:
    src = StorageFS(str(tmp_path / "src"))
    dst = StorageFS(str(tmp_path / "dst"))
    src.put("file.bin", b"payload")
    digests = src.transfer("file.bin", dst)
    assert dst.get("file.bin") == b"payload"
    assert digests["Size"] == "7"
    assert "SHA256" in digests


# --------------------------------------------------------------------------- #
# Metadata (xattr) -- best-effort, must not raise on simple round-trip
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("err", "metadata"),
    [
        pytest.param(errno.EOPNOTSUPP, {}, id="no-xattr-support"),
        pytest.param(errno.EIO, None, id="io-error"),
        pytest.param(errno.EACCES, None, id="permission"),
    ],
)
def test_storage_fs_get_metadata_raises_unless_xattrs_are_unsupported(
    storage: StorageFS, err: int, metadata: dict[str, str] | None
) -> None:
    """Only a filesystem without xattrs reads as "no metadata"; real failures raise."""
    storage.put("m.txt", b"x")
    with patch("txstore._fs_paths.xattr.get_all", side_effect=OSError(err, os.strerror(err))):
        if metadata is None:
            with pytest.raises(StorageException, match="Failed to read metadata"):
                storage.get_metadata("m.txt")
        else:
            assert storage.get_metadata("m.txt") == metadata


def test_storage_fs_add_and_get_metadata(storage: StorageFS, tmp_path: Path) -> None:
    skip_without_user_xattrs(tmp_path)
    storage.put("m.txt", b"x")
    storage.add_metadata("m.txt", {"k": "v"})
    md = storage.get_metadata("m.txt")
    assert md.get("k") == "v"


def test_storage_fs_metadata_ignores_other_xattr_namespaces(
    storage: StorageFS, tmp_path: Path
) -> None:
    """An ACL or label on the file is not txstore metadata: never read, never removed."""
    skip_without_user_xattrs(tmp_path)
    storage.put("m.txt", b"x", metadata={"k": "v"})
    path = Path(storage.base_dir, "m.txt")
    set_foreign_xattr(path)

    assert storage.get_metadata("m.txt") == {"k": "v"}
    storage.delete_metadata("m.txt")

    assert [name for name, _ in xattr.get_all(str(path))] == [FOREIGN_XATTR]


def test_storage_fs_get_metadata_skips_an_undecodable_xattr(storage: StorageFS) -> None:
    """A key or value that is not UTF-8 was set outside txstore and is left out."""
    storage.put("m.txt", b"x")
    pairs = [(b"ok", b"v"), (b"bad", b"\xff"), (b"\xff", b"v")]
    with patch("txstore._fs_paths.xattr.get_all", return_value=pairs):
        assert storage.get_metadata("m.txt") == {"ok": "v"}


def test_storage_fs_add_metadata_accepts_acl_kwarg(storage: StorageFS) -> None:
    storage.put("m.txt", b"x")
    # ACL is silently ignored on FS; must not raise.
    storage.add_metadata("m.txt", {}, acl="public-read")


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "in-transaction"])
@pytest.mark.parametrize("operation", ["copy", "move"])
def test_storage_fs_copy_or_move_without_auto_mkdir_refuses_a_missing_parent(
    tmp_path: Path, operation: str, in_transaction: bool
) -> None:
    """The instance `auto_mkdir` governs `copy` / `move` as it does `put` (STORAGE.md)."""
    storage = StorageFS(str(tmp_path / "repo"), auto_mkdir=False)
    storage.put("a.txt", b"a")
    if in_transaction:
        storage.transaction_begin()

    with pytest.raises(StorageException):
        getattr(storage, operation)("a.txt", "sub/b.txt")

    assert not (tmp_path / "repo" / "sub").exists()
    assert storage.get("a.txt") == b"a"


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "in-transaction"])
@pytest.mark.parametrize("operation", ["copy", "move"])
def test_storage_fs_copy_or_move_with_auto_mkdir_creates_the_parent(
    tmp_path: Path, operation: str, in_transaction: bool
) -> None:
    storage = StorageFS(str(tmp_path / "repo"), auto_mkdir=True)
    storage.put("a.txt", b"a")
    if in_transaction:
        storage.transaction_begin()

    getattr(storage, operation)("a.txt", "sub/b.txt")
    if in_transaction:
        storage.transaction_commit()

    assert storage.get("sub/b.txt") == b"a"
