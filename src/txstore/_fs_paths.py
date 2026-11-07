"""Filesystem helpers used by `StorageFS`.

Path validation (rejecting absolute paths and ``..``-traversal), temp-file naming,
exclusive no-follow opens, tmp-file cleanup, parent-directory mkdir, xattr reads, writes, and
removal with their error translation, and a true-replace ``copy_with_xattrs`` helper.

Kept out of `storage_fs.py` so the concrete `StorageFS` class stays
focused on the public `Storage` surface.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import pathlib
import secrets
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from typing import BinaryIO, Literal

import xattr

from txstore.base import CHUNK_SIZE, StorageException, StorageNotFoundException

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# errno of removing an absent xattr: ENODATA on Linux, ENOATTR on macOS and the BSDs.
_XATTR_ABSENT_ERRNOS = frozenset({errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)})
# errno of a filesystem that stores no xattrs at all.
_XATTR_UNSUPPORTED_ERRNOS = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP})
# Mode bits a written or copied file may carry: permissions only, never setuid, setgid, or sticky.
FILE_MODE_MASK = 0o777


def validate_path(base_dir: str, rel_path: str) -> None:
    """Reject absolute paths and any traversal that escapes `base_dir`.

    Raises:
        StorageException: If the path is absolute or escapes base_dir.
    """
    if os.path.isabs(rel_path):
        raise StorageException(f"Absolute paths are not allowed: {rel_path}")

    base_resolved = pathlib.Path(base_dir).resolve()
    try:
        full_path = (base_resolved / rel_path).resolve()
    except ValueError as exc:
        # A NUL byte (ValueError) or an unencodable character (UnicodeEncodeError).
        raise StorageException(f"Invalid path {rel_path!r}: {exc}") from exc
    try:
        full_path.relative_to(base_resolved)
    except ValueError as exc:
        raise StorageException(
            f"Path traversal detected: '{rel_path}' resolves outside storage root"
        ) from exc


def mkdir_parent(path: str) -> None:
    """Ensure the parent directory of `path` exists."""
    parent_dir = os.path.dirname(path)
    if parent_dir and not os.path.exists(parent_dir):
        logger.debug("Creating directory: %s", parent_dir)
        pathlib.Path(parent_dir).mkdir(parents=True, exist_ok=True)


def temp_path(path: str) -> str:
    """Return an unpredictable temp-file name beside `path`.

    The name is a dotfile, `.<name>.tmp.<16 hex>`, so a crash leftover is hidden from `list()`
    like any other dotfile and no object key can collide with it
    → [STORAGE.md](STORAGE.md#backend-primitives).
    """
    dirname, filename = os.path.split(path)
    return os.path.join(dirname, f".{filename}.tmp.{secrets.token_hex(8)}")


def regular_opener(path: str, flags: int) -> int:
    """Open `path` with `flags`, refusing anything but a regular file and never blocking on one.

    An `open` opener for any path that may already exist. The path is opened with
    ``O_NOFOLLOW | O_NONBLOCK | O_NOCTTY`` and its descriptor checked, so a link, FIFO, or device
    swapped in after a path check fails the open instead of hanging it, streaming from it, or
    becoming a controlling terminal; a regular file gets its blocking mode back. A created file
    gets mode ``0o666`` less the umask → [VALIDATION.md](VALIDATION.md#filename-safety).

    Raises:
        OSError: If the path is a link or cannot be opened, or EINVAL if it is not a regular
            file; a FIFO opened for writing without a reader fails with ENXIO.
    """
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY, 0o666)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Not a regular file", path)
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_exclusive(path: str, mode: int = 0o666) -> BinaryIO:
    """Create `path` exclusively for binary writing, never through a symlink.

    ``O_EXCL`` meets no existing file, so ``O_NOFOLLOW`` is defence in depth. `open` owns the
    descriptor the opener returns and closes it if wrapping it fails, so none can leak.

    Args:
        path: File to create.
        mode: Creation mode, as for `os.open`; the umask applies.

    Raises:
        OSError: If `path` exists (a planted file or link included) or cannot be created.
    """

    def opener(file: str, flags: int) -> int:
        return os.open(file, flags | os.O_NOFOLLOW, mode)

    return open(path, "xb", opener=opener)  # caller closes


def cleanup_tmp_file(tmp_path: str) -> None:
    """Best-effort removal of a temporary file, or of a link at its name."""
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Failed to cleanup temp file %s: %s", tmp_path, exc)


@contextmanager
def _encodable_xattr(key: str) -> Iterator[None]:
    """Raise a name or value the OS cannot take (NUL, lone surrogate) as `OSError` EINVAL."""
    try:
        yield
    except (TypeError, ValueError) as exc:
        raise OSError(errno.EINVAL, f"Invalid xattr {key!r}: {exc}") from exc


def set_xattrs(abs_path: str, metadata: dict[str, str]) -> None:
    """Apply `metadata` as ``user.<key>`` xattrs on an already-resolved absolute path.

    Keys never reach another namespace (``security.``, ``trusted.``, ``system.``), whatever
    the caller passes → [STORAGE.md](STORAGE.md#storage-abi).

    Raises:
        OSError: If an attribute cannot be set, EINVAL for a name or value the OS cannot
            encode; the caller decides whether that fails the call.
    """
    for key, value in metadata.items():
        with _encodable_xattr(key):
            # nofollow: a link swapped in at the path must not redirect the write.
            xattr.set(abs_path, key, str(value), nofollow=True, namespace=xattr.NS_USER)


def get_xattrs(abs_path: str) -> list[tuple[bytes, bytes]]:
    """Return the raw ``user.`` xattr pairs of an already-resolved path, names unprefixed.

    Raises:
        OSError: If the attributes cannot be read.
    """
    # nofollow: a link swapped in at the path must not redirect the read.
    return list(xattr.get_all(abs_path, nofollow=True, namespace=xattr.NS_USER))


def remove_xattr(abs_path: str, key: str, *, missing_ok: bool = False) -> None:
    """Remove the xattr ``user.<key>`` from an already-resolved absolute path.

    Raises:
        OSError: If the attribute cannot be removed, EINVAL for a name the OS cannot encode;
            an absent `key` only when `missing_ok` is False.
    """
    try:
        with _encodable_xattr(key):
            xattr.remove(abs_path, key, nofollow=True, namespace=xattr.NS_USER)
    except OSError as exc:
        if not (missing_ok and exc.errno in _XATTR_ABSENT_ERRNOS):
            raise


def remove_all_xattrs(abs_path: str) -> None:
    """Remove every ``user.`` xattr from an already-resolved absolute path.

    Names go back to the OS as listed, so one that is not UTF-8 is removed too; other
    namespaces, such as an SELinux label, are left alone.

    Raises:
        OSError: If the attributes cannot be listed or one cannot be removed.
    """
    for key, _ in get_xattrs(abs_path):
        xattr.remove(abs_path, key, nofollow=True, namespace=xattr.NS_USER)


XattrAction = Literal["read", "set", "delete"]


@contextmanager
def xattr_errors(
    action: XattrAction, path: str, abs_path: str, *, not_found: bool = False
) -> Iterator[None]:
    """Translate an `OSError` of the xattr calls in the block for `StorageFS` metadata methods.

    A filesystem without xattr support logs a warning and the call succeeds; any other
    failure → `StorageException` → [STORAGE.md](STORAGE.md#storage-abi).

    Args:
        action: Verb for the messages.
        path: The caller's relative path, for the exception.
        abs_path: The resolved path, for the warning.
        not_found: Map a missing object to `StorageNotFoundException`, as `get_metadata` must.

    Raises:
        StorageNotFoundException: If `not_found` is set and the object is missing.
        StorageException: If the block raises any other `OSError` but "not supported".
    """
    try:
        yield
    except OSError as exc:
        if not_found and isinstance(exc, FileNotFoundError):
            raise StorageNotFoundException(f"Object not found: {path}") from exc
        if exc.errno not in _XATTR_UNSUPPORTED_ERRNOS:
            raise StorageException(f"Failed to {action} metadata of {path}: {exc}") from exc
        logger.warning("Failed to %s xattrs of %s: %s", action, abs_path, exc)


def copy_with_xattrs(src: str, dst: str) -> None:
    """Copy `src` to `dst` (data, permission bits, and ``user.`` xattrs) as a true replace.

    The copy is written to an exclusive temp file beside `dst` and renamed over it, so `dst`
    ends with exactly `src`'s xattrs: a staged ``delete_metadata`` / ``delete_metadata_fields``
    propagates the removal on commit, and a ``transaction_revert`` cannot leave attrs added
    during the transaction on the original.

    Neither end is followed through a symlink: `src` is opened through `regular_opener`, so a
    link, FIFO, or device there fails the open, and the rename replaces a link at `dst` instead
    of writing through it. A directory at `dst` fails the rename instead of receiving the copy
    → [VALIDATION.md](VALIDATION.md#filename-safety).

    `src` may be a footmark anyone who can write into the root planted, so the copy carries
    only what a `put` can set: the mode masked to the permission bits, never setuid, setgid,
    or sticky, and only ``user.`` xattrs, the txstore metadata.

    xattr transfer is best-effort: failures are logged as warnings rather than raised so that
    filesystems that don't support user xattrs (FAT32, some network mounts) still allow plain
    commits/reverts to succeed.

    Raises:
        OSError: If `src` cannot be opened or is not a regular file, or the copy or rename
            fails.
    """
    tmp = temp_path(dst)
    try:
        with open(src, "rb", opener=regular_opener) as fsrc, open_exclusive(tmp, 0o600) as fdst:
            shutil.copyfileobj(fsrc, fdst, CHUNK_SIZE)
            os.fchmod(fdst.fileno(), os.fstat(fsrc.fileno()).st_mode & FILE_MODE_MASK)
            _copy_xattrs(fsrc.fileno(), fdst.fileno(), src, dst)
        os.replace(tmp, dst)
    except BaseException:
        cleanup_tmp_file(tmp)
        raise


def _copy_xattrs(src_fd: int, dst_fd: int, src: str, dst: str) -> None:
    """Copy the ``user.`` xattrs between open descriptors, best-effort."""
    try:
        pairs = xattr.get_all(src_fd, namespace=xattr.NS_USER)
    except OSError as exc:
        logger.warning("Failed to read xattrs from %s: %s", src, exc)
        return
    for key, value in pairs:
        try:
            xattr.set(dst_fd, key, value, namespace=xattr.NS_USER)
        except OSError as exc:
            logger.warning("Failed to copy xattr %r from %s to %s: %s", key, src, dst, exc)
