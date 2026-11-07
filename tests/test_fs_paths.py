"""Tests for txstore/_fs_paths.py: the FS path-safety and xattr helpers `StorageFS` builds on.

Path confinement is the rule VALIDATION.md#filename-safety states for `fs://`.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
import xattr
from helpers.backends import FOREIGN_XATTR, set_foreign_xattr, skip_without_user_xattrs
from helpers.storage import deadline

from txstore import StorageException, StorageNotFoundException
from txstore._fs_paths import (
    cleanup_tmp_file,
    copy_with_xattrs,
    mkdir_parent,
    regular_opener,
    remove_all_xattrs,
    remove_xattr,
    set_xattrs,
    validate_path,
    xattr_errors,
)

# --- validate_path ------------------------------------------------------------------------------


@pytest.mark.parametrize("rel", ["file.bin", "pool/main/h/hello.deb", "./file.bin", "a/../b.bin"])
def test_validate_path_accepts_a_path_inside_the_root(tmp_path: Path, rel: str) -> None:
    """An accepted path is one that raises nothing; the guard returns no value."""
    validate_path(str(tmp_path), rel)


def test_validate_path_rejects_an_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(StorageException, match="Absolute paths are not allowed"):
        validate_path(str(tmp_path), "/etc/passwd")


@pytest.mark.parametrize("rel", ["../outside.bin", "a/../../outside.bin", "../../etc/passwd"])
def test_validate_path_rejects_traversal_out_of_the_root(tmp_path: Path, rel: str) -> None:
    with pytest.raises(StorageException, match="Path traversal detected"):
        validate_path(str(tmp_path), rel)


@pytest.mark.parametrize("rel", ["a\x00b", "a\ud800b"], ids=["nul", "lone-surrogate"])
def test_validate_path_rejects_an_unencodable_path(tmp_path: Path, rel: str) -> None:
    with pytest.raises(StorageException, match="Invalid path"):
        validate_path(str(tmp_path), rel)


# --- mkdir_parent -------------------------------------------------------------------------------


def test_mkdir_parent_creates_missing_ancestors(tmp_path: Path) -> None:
    mkdir_parent(str(tmp_path / "a" / "b" / "file.bin"))

    assert (tmp_path / "a" / "b").is_dir()


def test_mkdir_parent_is_idempotent(tmp_path: Path) -> None:
    target = str(tmp_path / "a" / "file.bin")
    mkdir_parent(target)
    mkdir_parent(target)

    assert (tmp_path / "a").is_dir()


def test_mkdir_parent_ignores_a_bare_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name with no parent must create nothing, not a directory named "."."""
    monkeypatch.chdir(tmp_path)

    mkdir_parent("file.bin")

    assert list(tmp_path.iterdir()) == []


# --- regular_opener -----------------------------------------------------------------------------


@pytest.mark.parametrize("flags", [os.O_RDONLY, os.O_WRONLY], ids=["read", "write"])
def test_regular_opener_refuses_a_device(flags: int) -> None:
    """The fstat check, not a path test, refuses it: a device opens but is never used."""
    with pytest.raises(OSError, match="Not a regular file") as info:
        regular_opener(os.devnull, flags)
    assert info.value.errno == errno.EINVAL


def test_regular_opener_closes_a_refused_descriptor(tmp_path: Path) -> None:
    """POSIX hands out the lowest free descriptor, so a leak shows as a different probe fd."""
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    probe = os.open(os.devnull, os.O_RDONLY)
    os.close(probe)

    with deadline(5), pytest.raises(OSError, match="Not a regular file"):
        regular_opener(str(fifo), os.O_RDONLY)

    again = os.open(os.devnull, os.O_RDONLY)
    os.close(again)
    assert again == probe


@pytest.mark.parametrize(("umask", "mode"), [(0o000, 0o666), (0o077, 0o600)])
def test_regular_opener_returns_a_blocking_descriptor_with_the_umask_mode(
    tmp_path: Path, umask: int, mode: int
) -> None:
    """The non-blocking open is only for the check; a caller streaming the file must block."""
    previous = os.umask(umask)
    try:
        fd = regular_opener(str(tmp_path / "f"), os.O_WRONLY | os.O_CREAT)
    finally:
        os.umask(previous)
    try:
        assert os.get_blocking(fd)
        assert stat.S_IMODE(os.fstat(fd).st_mode) == mode
    finally:
        os.close(fd)


def test_regular_opener_opens_without_following_blocking_or_taking_a_tty(tmp_path: Path) -> None:
    """Each flag guards a hazard the fstat check comes too late for: a link, a FIFO, a tty."""
    target = tmp_path / "f"
    target.write_bytes(b"")
    seen: list[int] = []
    real_open = os.open

    def recording_open(path: str, flags: int, mode: int = 0o777) -> int:
        seen.append(flags)
        return real_open(path, flags, mode)

    with patch("txstore._fs_paths.os.open", side_effect=recording_open):
        os.close(regular_opener(str(target), os.O_RDONLY))

    required = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY
    assert [flags & required for flags in seen] == [required]


# --- cleanup_tmp_file ---------------------------------------------------------------------------


def test_cleanup_tmp_file_removes_the_file(tmp_path: Path) -> None:
    tmp = tmp_path / "staged.tmp"
    tmp.write_bytes(b"x")

    cleanup_tmp_file(str(tmp))

    assert not tmp.exists()


def test_cleanup_tmp_file_tolerates_a_missing_file(tmp_path: Path) -> None:
    """Removing what is already gone is a no-op, not an error."""
    missing = tmp_path / "gone.tmp"

    cleanup_tmp_file(str(missing))

    assert not missing.exists()


def test_cleanup_tmp_file_removes_a_dangling_link(tmp_path: Path) -> None:
    link = tmp_path / "staged.tmp"
    link.symlink_to(tmp_path / "nowhere")

    cleanup_tmp_file(str(link))

    assert not link.is_symlink()


def test_cleanup_tmp_file_warns_instead_of_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup runs on an error path, so it must never replace the original failure."""
    tmp = tmp_path / "staged.tmp"
    tmp.write_bytes(b"x")
    monkeypatch.setattr(os, "unlink", lambda _p: (_ for _ in ()).throw(OSError("busy")))

    with caplog.at_level(logging.WARNING):
        cleanup_tmp_file(str(tmp))

    assert "Failed to cleanup temp file" in caplog.text


# --- xattrs -------------------------------------------------------------------------------------


def test_set_xattrs_raises_on_a_missing_file(tmp_path: Path) -> None:
    """The caller decides: `put` logs the failure, `add_metadata` raises it."""
    with pytest.raises(FileNotFoundError):
        set_xattrs(str(tmp_path / "gone.bin"), {"sha256": "abc"})


@pytest.mark.parametrize(
    "metadata",
    [{"a\x00b": "v"}, {"\udcff": "v"}, {"k": "\udcff"}],
    ids=["nul-key", "surrogate-key", "surrogate-value"],
)
def test_set_xattrs_raises_an_unencodable_xattr_as_einval(
    tmp_path: Path, metadata: dict[str, str]
) -> None:
    """Never a raw TypeError or UnicodeEncodeError, which no caller catches."""
    target = tmp_path / "file.bin"
    target.write_bytes(b"x")
    with pytest.raises(OSError, match="Invalid xattr") as info:
        set_xattrs(str(target), metadata)
    assert info.value.errno == errno.EINVAL


@pytest.mark.parametrize("key", ["a\x00b", "\udcff"], ids=["nul", "surrogate"])
def test_remove_xattr_raises_an_unencodable_name_as_einval(tmp_path: Path, key: str) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"x")
    with pytest.raises(OSError, match="Invalid xattr") as info:
        remove_xattr(str(target), key)
    assert info.value.errno == errno.EINVAL


# ENOATTR exists on macOS and the BSDs only, so Linux CI runs the ENODATA case alone.
@pytest.mark.parametrize("name", ["ENODATA", "ENOATTR"])
@pytest.mark.parametrize("missing_ok", [True, False])
def test_remove_xattr_ignores_an_absent_name_only_when_missing_ok(
    tmp_path: Path, missing_ok: bool, name: str
) -> None:
    err = getattr(errno, name, None)
    if err is None:
        pytest.skip(f"errno.{name} does not exist on this platform")
    absent = OSError(err, "no such attribute")
    with patch("txstore._fs_paths.xattr.remove", side_effect=absent):
        if missing_ok:
            remove_xattr(str(tmp_path / "f"), "k", missing_ok=True)
        else:
            with pytest.raises(OSError, match="no such attribute"):
                remove_xattr(str(tmp_path / "f"), "k")


def test_remove_xattr_raises_any_other_failure_even_when_missing_ok(tmp_path: Path) -> None:
    with (
        patch("txstore._fs_paths.xattr.remove", side_effect=OSError(errno.EIO, "io")),
        pytest.raises(OSError, match="io"),
    ):
        remove_xattr(str(tmp_path / "f"), "k", missing_ok=True)


def test_remove_all_xattrs_removes_every_listed_name_as_listed(tmp_path: Path) -> None:
    """Names go back as bytes, so one that is not UTF-8 is removable too."""
    names = [(b"a", b"1"), (b"\xff", b"2")]
    with (
        patch("txstore._fs_paths.xattr.get_all", return_value=names) as get_all,
        patch("txstore._fs_paths.xattr.remove") as remove,
    ):
        remove_all_xattrs(str(tmp_path / "f"))
    assert [c.args[1] for c in remove.call_args_list] == [b"a", b"\xff"]
    assert get_all.call_args.kwargs["namespace"] == xattr.NS_USER
    assert all(c.kwargs["namespace"] == xattr.NS_USER for c in remove.call_args_list)


def test_remove_all_xattrs_leaves_other_namespaces(tmp_path: Path) -> None:
    """An SELinux label or ACL on the file is not txstore metadata, so it is never stripped."""
    skip_without_user_xattrs(tmp_path)
    target = tmp_path / "f"
    target.write_bytes(b"x")
    set_xattrs(str(target), {"k": "v"})
    set_foreign_xattr(target)

    remove_all_xattrs(str(target))

    assert [name for name, _ in xattr.get_all(str(target))] == [FOREIGN_XATTR]


@pytest.mark.parametrize(
    ("not_found", "expected"),
    [(True, StorageNotFoundException), (False, StorageException)],
    ids=["not-found", "plain"],
)
def test_xattr_errors_maps_a_missing_object_only_when_asked(
    not_found: bool, expected: type[StorageException]
) -> None:
    with pytest.raises(StorageException) as info:
        with xattr_errors("read", "a.txt", "/root/a.txt", not_found=not_found):
            raise FileNotFoundError(errno.ENOENT, "gone")
    assert type(info.value) is expected


@pytest.mark.parametrize("err", [errno.ENOTSUP, errno.EOPNOTSUPP])
def test_xattr_errors_warns_on_a_filesystem_without_xattrs(
    err: int, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING), xattr_errors("set", "a.txt", "/root/a.txt"):
        raise OSError(err, "unsupported")
    assert "Failed to set xattrs of /root/a.txt" in caplog.text


def test_xattr_errors_raises_any_other_failure_chained() -> None:
    cause = OSError(errno.EIO, "io")
    with pytest.raises(StorageException, match="Failed to delete metadata of a.txt") as info:
        with xattr_errors("delete", "a.txt", "/root/a.txt"):
            raise cause
    assert info.value.__cause__ is cause


def test_set_xattrs_stringifies_values(tmp_path: Path) -> None:
    skip_without_user_xattrs(tmp_path)
    target = tmp_path / "file.bin"
    target.write_bytes(b"x")

    set_xattrs(str(target), {"size": "7"})

    assert xattr.getxattr(str(target), "user.size") == b"7"


@pytest.mark.parametrize("key", ["security.selinux", "trusted.x", "system.posix_acl_access"])
def test_set_xattrs_keeps_every_key_in_the_user_namespace(tmp_path: Path, key: str) -> None:
    """A caller key never names another namespace, so it can never relabel or re-permission."""
    skip_without_user_xattrs(tmp_path)
    target = tmp_path / "file.bin"
    target.write_bytes(b"x")

    set_xattrs(str(target), {key: "v"})

    assert [name for name, _ in xattr.get_all(str(target))] == [f"user.{key}".encode()]


def test_set_xattrs_does_not_follow_a_symlink(tmp_path: Path) -> None:
    """A link swapped in after the temp-file rename must not redirect the metadata write."""
    skip_without_user_xattrs(tmp_path)
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"x")
    link = tmp_path / "link.bin"
    link.symlink_to(victim)

    # macOS labels the link itself; Linux refuses user.* xattrs on a link with EPERM.
    with contextlib.suppress(PermissionError):
        set_xattrs(str(link), {"k": "v"})

    assert xattr.get_all(str(victim)) == []


def test_copy_with_xattrs_copies_data_and_attributes(tmp_path: Path) -> None:
    skip_without_user_xattrs(tmp_path)
    src, dst = tmp_path / "src.bin", tmp_path / "dst.bin"
    src.write_bytes(b"payload")
    xattr.setxattr(str(src), "user.sha256", b"abc")

    copy_with_xattrs(str(src), str(dst))

    assert dst.read_bytes() == b"payload"
    assert xattr.getxattr(str(dst), "user.sha256") == b"abc"


def test_copy_with_xattrs_copies_only_user_attributes(tmp_path: Path) -> None:
    """A planted footmark's ACL or label must not reach the live object."""
    skip_without_user_xattrs(tmp_path)
    src, dst = tmp_path / "src.bin", tmp_path / "dst.bin"
    src.write_bytes(b"payload")
    xattr.setxattr(str(src), "user.k", b"v")
    set_foreign_xattr(src)

    copy_with_xattrs(str(src), str(dst))

    assert [name for name, _ in xattr.get_all(str(dst))] == [b"user.k"]


@pytest.mark.parametrize("failing", ["get_all", "set"])
def test_copy_with_xattrs_lands_the_data_when_an_attribute_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failing: str
) -> None:
    """Attributes are best-effort on a copy: a failure is logged, the bytes still land."""
    src, dst = tmp_path / "src.bin", tmp_path / "dst.bin"
    src.write_bytes(b"payload")
    pairs = [(b"user.k", b"v")]
    with (
        patch("txstore._fs_paths.xattr.get_all", return_value=pairs),
        patch(f"txstore._fs_paths.xattr.{failing}", side_effect=OSError(errno.EIO, "io")),
        caplog.at_level(logging.WARNING),
    ):
        copy_with_xattrs(str(src), str(dst))
    assert dst.read_bytes() == b"payload"
    assert "Failed to" in caplog.text


def test_copy_with_xattrs_replaces_rather_than_merges(tmp_path: Path) -> None:
    """True-replace semantics: a staged `delete_metadata` must propagate on commit."""
    skip_without_user_xattrs(tmp_path)
    src, dst = tmp_path / "src.bin", tmp_path / "dst.bin"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    xattr.setxattr(str(dst), "user.stale", b"1")

    copy_with_xattrs(str(src), str(dst))

    # pyxattr returns keys as bytes; a str key would never match.
    assert b"user.stale" not in [k for k, _ in xattr.get_all(str(dst))]


def test_copy_with_xattrs_refuses_a_symlinked_source(tmp_path: Path) -> None:
    """A source link (a planted footmark) must not lend its target's bytes or attrs."""
    target = tmp_path / "target.bin"
    target.write_bytes(b"secret")
    link = tmp_path / "link.bin"
    link.symlink_to(target)
    dst = tmp_path / "dst.bin"

    with pytest.raises(OSError, match="symbolic links"):  # ELOOP from O_NOFOLLOW
        copy_with_xattrs(str(link), str(dst))

    assert sorted(p.name for p in tmp_path.iterdir()) == ["link.bin", "target.bin"]


def test_copy_with_xattrs_replaces_a_symlinked_destination(tmp_path: Path) -> None:
    """A destination link is replaced by the copy; its target keeps its bytes and attrs."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"new")
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"old")
    link = tmp_path / "link.bin"
    link.symlink_to(victim)

    copy_with_xattrs(str(src), str(link))

    assert victim.read_bytes() == b"old"
    assert not link.is_symlink()
    assert link.read_bytes() == b"new"


def test_copy_with_xattrs_refuses_a_directory_destination(tmp_path: Path) -> None:
    """`shutil.copy` would write `dst/<name>`; the rename onto a directory fails instead."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"new")
    dst = tmp_path / "dir"
    dst.mkdir()

    with pytest.raises(IsADirectoryError):
        copy_with_xattrs(str(src), str(dst))

    assert list(dst.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dir", "src.bin"]


def test_copy_with_xattrs_copies_only_the_permission_bits(tmp_path: Path) -> None:
    """A planted setuid footmark must not land setuid; empty, so no write clears the bit."""
    src, dst = tmp_path / "src.bin", tmp_path / "dst.bin"
    src.write_bytes(b"")
    src.chmod(0o4750)

    copy_with_xattrs(str(src), str(dst))

    assert dst.stat().st_mode & 0o7777 == 0o750
