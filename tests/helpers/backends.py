"""Backend-availability skips that ``--strict-backends`` turns into failures (TESTING.md)."""

from __future__ import annotations

import errno
import os
import stat
import struct
from pathlib import Path
from typing import NoReturn

import pytest
import xattr

# Set from ``--strict-backends`` by ``tests/conftest.py``; CI passes it so a missing
# backend leg fails the run instead of silently skipping.
STRICT = False


def backend_unavailable(reason: str) -> NoReturn:
    """Skip the test for *reason*, or fail it under ``--strict-backends``."""
    if STRICT:
        pytest.fail(f"{reason} (--strict-backends)")
    pytest.skip(reason)


def skip_without_user_xattrs(path: Path) -> None:
    """Skip the test, or fail it under ``--strict-backends``, without user xattrs at *path*.

    Args:
        path: An existing directory on the filesystem under test; the probe file is removed.
    """
    probe = path / ".xattr-probe"
    probe.write_bytes(b"")
    try:
        xattr.set(str(probe), "user.probe", b"1")
    except OSError:
        backend_unavailable("filesystem does not support user xattrs")
    finally:
        probe.unlink(missing_ok=True)


def make_setgid(path: Path) -> None:
    """Make *path* setgid, or skip (fail under ``--strict-backends``) where the bit drops.

    The group is set to the caller's own first, so the kernel cannot clear the bit on chmod.
    """
    os.chown(path, -1, os.getegid())
    path.chmod(0o2755)
    if not path.stat().st_mode & stat.S_ISGID:
        backend_unavailable("filesystem drops the setgid bit")


# An xattr outside ``user.`` that the file owner may set on Linux: an extended POSIX ACL.
FOREIGN_XATTR = b"system.posix_acl_access"
# Linux ``posix_acl_xattr`` layout: a version header, then (tag, perm, id) entries in tag order.
_ACL_VERSION = 2
_ACL_USER_OBJ, _ACL_USER, _ACL_GROUP_OBJ, _ACL_MASK, _ACL_OTHER = 0x01, 0x02, 0x04, 0x10, 0x20
_ACL_UNDEFINED_ID = 0xFFFFFFFF
_READ, _READ_WRITE = 0o4, 0o6
# A filesystem or user without ACL support; any other errno means the blob itself is wrong.
_ACL_UNAVAILABLE_ERRNOS = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM})


def set_foreign_xattr(path: Path) -> None:
    """Set `FOREIGN_XATTR` on *path*, or skip (fail under ``--strict-backends``) without ACLs.

    The ACL has a named-user entry and a mask, so Linux keeps it as an xattr instead of folding
    it into the mode, though it still sets the mode to 0644 from the entries; macOS stores the
    name as given.

    Raises:
        OSError: If the OS rejects the ACL itself (EINVAL), not merely lacks ACL support.
    """
    entries = [
        (_ACL_USER_OBJ, _READ_WRITE, _ACL_UNDEFINED_ID),
        (_ACL_USER, _READ, os.getuid()),
        (_ACL_GROUP_OBJ, _READ, _ACL_UNDEFINED_ID),
        (_ACL_MASK, _READ, _ACL_UNDEFINED_ID),
        (_ACL_OTHER, _READ, _ACL_UNDEFINED_ID),
    ]
    acl = struct.pack("<I", _ACL_VERSION) + b"".join(struct.pack("<HHI", *e) for e in entries)
    try:
        xattr.set(str(path), FOREIGN_XATTR, acl)
    except OSError as exc:
        if exc.errno not in _ACL_UNAVAILABLE_ERRNOS:
            raise
        backend_unavailable("cannot set a POSIX ACL xattr here")
