"""StorageFS: Filesystem-based Storage Backend Implementation.

This module provides a concrete implementation of the `Storage` ABC for
local-filesystem operations. Transactional behaviour lives in
`FootmarkMixin` -- this module only supplies the backend primitives
(`_obj_*`) and the public read/write/delete code-paths.

It supports:

  - Standard CRUD operations (create, read, update, delete)
  - Transactional operations with commit/rollback semantics (mixed in)
  - File metadata management via extended attributes (xattr)
  - Automatic directory creation
  - Stream-based I/O for memory-efficient large file handling

Example:
    >>> storage = StorageFS("/var/data/repo")
    >>> storage.put("packages/nginx.deb", package_data)
    >>> content = storage.get("packages/nginx.deb")

Note:
    Extended attributes (xattr) are used for metadata storage and may not be
    supported on all filesystems (e.g., FAT32, some network filesystems).
"""

from __future__ import annotations

import builtins
import errno
import fcntl
import io
import logging
import os
import pathlib
import re
import shutil
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from typing import IO, Any

from txstore._fs_paths import (
    FILE_MODE_MASK,
    cleanup_tmp_file,
    copy_with_xattrs,
    get_xattrs,
    mkdir_parent,
    open_exclusive,
    regular_opener,
    remove_all_xattrs,
    remove_xattr,
    set_xattrs,
    temp_path,
    validate_path,
    xattr_errors,
)
from txstore._reserved import LOCK_FILENAME, check_reserved_path, is_reserved_path
from txstore.base import (
    CHUNK_SIZE,
    Storage,
    StorageException,
    StorageNotFoundException,
    StorageType,
)
from txstore.transaction import FootmarkMixin
from txstore.url import join_path

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# An `acl` of three or four octal digits is a file mode; any other ACL is ignored.
_FILE_MODE_RE = re.compile(r"[0-7]{3,4}")
# Creation mode of a written file without a mode `acl`; the umask applies, as for `open`.
_DEFAULT_FILE_MODE = 0o666


class StorageFS(FootmarkMixin, Storage):
    """Filesystem storage backend implementation.

    Provides file operations against a local directory tree with full
    transactional support (via `FootmarkMixin`), xattr-based metadata
    and streaming I/O.

    Attributes:
        base_dir: Root directory for all storage operations.
        auto_mkdir: If True, automatically create parent directories
            when writing.
        create_base: If True, create `base_dir` itself when missing.
    """

    def __init__(self, base_dir: str, auto_mkdir: bool = True, create_base: bool = True) -> None:
        """Initialise the filesystem storage backend.

        Args:
            base_dir: Root directory for storage operations. May be relative.
            auto_mkdir: Create parent directories of written objects when needed.
            create_base: Create `base_dir` itself when missing. Withheld by
                `make_storage` unless `create_path` is set, so a read-only
                command never materialises a mistyped path.

        Raises:
            StorageException: If the base directory cannot be created.
        """
        self.base_dir = os.path.abspath(base_dir)
        self.auto_mkdir = auto_mkdir
        self.create_base = create_base

        logger.debug("Initializing StorageFS with base_dir=%s", self.base_dir)

        if self.create_base and not os.path.exists(self.base_dir):
            logger.debug("Creating base directory: %s", self.base_dir)
            try:
                pathlib.Path(self.base_dir).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StorageException(
                    f"Failed to create base directory {self.base_dir}: {exc}"
                ) from exc

        super().__init__()

    def get_type(self) -> StorageType:
        """Return the storage backend type identifier."""
        return StorageType.fs

    def __repr__(self) -> str:
        """Return a human-readable representation of this storage instance."""
        return f"<StorageFS type={self.get_type()} base_dir={self.base_dir!r}>"

    # =========================================================================
    # Backend primitives (consumed by FootmarkMixin)
    # =========================================================================

    def _obj_full(self, rel: str) -> str:
        """Return the absolute filesystem path for a relative path."""
        return join_path(self.base_dir, rel, no_root=False)

    def _obj_iter_rel_all(self) -> Iterable[str]:
        """Yield every file in `base_dir` as a POSIX relative path."""
        base = self.base_dir
        base_prefix_len = len(base) + 1
        for dirpath, _dirnames, filenames in os.walk(base):
            if dirpath == base:
                rel_dir = ""
            else:
                rel_dir = dirpath[base_prefix_len:].replace(os.sep, "/") + "/"
            for filename in filenames:
                yield rel_dir + filename

    def _obj_exists(self, full: str) -> bool:
        """Return True iff `full` exists; any error but "not found" → `StorageException`.

        `os.path.exists` would read a permission or I/O failure as a missing file.
        """
        try:
            os.stat(full)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError as exc:
            raise StorageException(f"Error checking {full}: {exc}") from exc
        return True

    def _obj_same(self, full_a: str, full_b: str) -> bool:
        """Return True iff both paths name one file: a case or Unicode alias, or a hard link."""
        try:
            return os.path.samefile(full_a, full_b)
        except OSError:
            return False

    def _obj_rename(self, src_full: str, dst_full: str) -> bool:
        """Rename the file atomically; across devices → False, so the move copies instead.

        `os.replace` changes nothing when it fails, unlike `shutil.move`, whose copy fallback
        overwrites the destination before the failure surfaces.
        """
        try:
            os.replace(src_full, dst_full)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                return False
            raise
        return True

    def _obj_copy(self, src_full: str, dst_full: str) -> None:
        """Copy data + xattrs from `src_full` to `dst_full` (true replace)."""
        copy_with_xattrs(src_full, dst_full)

    def _obj_delete(self, full: str) -> None:
        """Hard-delete the file at `full`; an absent file is no error, as on S3.

        `os.remove` unlinks the final component itself, never a symlink's target:
        `_obj_checked_footmark` resolves only a listed footmark's directory and relies on it.
        """
        with suppress(FileNotFoundError):
            os.remove(full)

    def _obj_checked_footmark(self, rel: str) -> str:
        """Full path of listed footmark `rel` once it is confined and a regular file.

        `_obj_full` resolves symlinks, so a planted link would lend its *target* to the delete
        or copy. A footmark name is mintable by anyone who can write into the storage root, so
        the name is re-checked the way every public mutation is, and only its directory is
        resolved: the delete removes the footmark itself, and the copy's `regular_opener` refuses
        a link or FIFO swapped in later → [VALIDATION.md](VALIDATION.md#filename-safety).

        Args:
            rel: Relative path of the listed footmark.

        Raises:
            StorageNotFoundException: If the footmark is gone since the listing.
            StorageException: If `rel` escapes `base_dir` or is not a regular file.
        """
        self._validate_path(rel)
        path = os.path.join(self.base_dir, rel)
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError as exc:
            raise StorageNotFoundException(f"Footmark not found: {rel}") from exc
        except OSError as exc:
            raise StorageException(f"Refusing non-regular footmark: {rel}") from exc
        if not stat.S_ISREG(mode):
            raise StorageException(f"Refusing non-regular footmark: {rel}")
        return os.path.join(self._obj_full(os.path.dirname(rel)), os.path.basename(rel))

    def _obj_touch(self, full: str) -> None:
        """Create an empty marker file at `full`, refusing a link, FIFO, or device there."""
        os.close(regular_opener(full, os.O_WRONLY | os.O_CREAT))

    def _obj_check_reserved(self, rel: str) -> None:
        """Refuse a mutation of a reserved object or of the root, also through a symlink.

        `_obj_full` resolves symlinks, so `d/.transactions.lock` under a `d -> .` link, or a
        destination that resolves to the root itself, reaches a reserved object while its
        relative path does not name one → [VALIDATION.md](VALIDATION.md#filename-safety).

        Raises:
            StorageException: If `rel` names or resolves to a reserved object, the root, or a
                path outside it.
        """
        check_reserved_path(rel)
        resolved = os.path.relpath(self._obj_full(rel), os.path.realpath(self.base_dir))
        if resolved == os.curdir:
            raise StorageException(f"Refusing to write storage root: {rel!r}")
        if resolved == os.pardir or resolved.startswith(os.pardir + os.sep):
            raise StorageException(
                f"Path traversal detected: {rel!r} resolves outside storage root"
            )
        if is_reserved_path(resolved):
            raise StorageException(f"Refusing to write reserved storage path: {rel}")

    def _obj_prepare_parent(self, full: str) -> None:
        """Ensure the parent directory of `full` exists when the instance `auto_mkdir` is set."""
        if self.auto_mkdir:
            mkdir_parent(full)

    def _obj_check_transfer(self, verb: str, src_rel: str, dst_rel: str) -> None:
        """Refuse a directory source of `move` and a directory destination of either verb.

        Runs after the reserved checks, so the root, itself a directory, is refused as reserved
        → [VALIDATION.md](VALIDATION.md#filename-safety).

        Raises:
            StorageException: If `move` names a directory source, or `dst_rel` is a directory.
        """
        if verb == "move" and os.path.isdir(self._obj_full(src_rel)):
            # Outside a transaction the rename would carry the whole tree, footmarks included.
            raise StorageException(f"Refusing to move a directory: {src_rel}")
        if os.path.isdir(self._obj_full(dst_rel)):
            # The destination is written as a file; never replace or enter a directory.
            raise StorageException(f"Refusing to {verb} onto a directory: {dst_rel}")

    @contextmanager
    def _obj_lock(self) -> Iterator[None]:
        """Hold an exclusive ``flock`` on ``.transactions.lock`` under `base_dir`.

        Yields:
            None while the lock is held.

        Raises:
            StorageException: If the lock file cannot be created or locked, including a
                `base_dir` that does not exist and a lock path that is a symlink, FIFO, or
                device → [VALIDATION.md](VALIDATION.md#filename-safety).
        """
        path = os.path.join(self.base_dir, LOCK_FILENAME)
        try:
            # A planted link would put the flock on an arbitrary file, a device on a device.
            fd = open(path, "a+", encoding="utf-8", opener=regular_opener)
        except OSError as exc:
            raise StorageException(f"Failed to open history lock {path}: {exc}") from exc
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            fd.close()
            raise StorageException(f"Failed to acquire history lock {path}: {exc}") from exc
        logger.debug("Acquired FS history lock %s", path)
        try:
            yield
        finally:
            try:
                # Closing the fd drops the flock anyway, so an unlock failure is
                # not worth replacing the body's exception with.
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                logger.warning("Failed to unlock FS history lock %s", path, exc_info=True)
            finally:
                fd.close()
            logger.debug("Released FS history lock %s", path)

    def _obj_lock_identity(self) -> tuple[str, int, int]:
        """Return the root directory's device and inode → [STORAGE.md](STORAGE.md#locking).

        Raises:
            StorageException: If `base_dir` cannot be read, as the lock file then cannot be opened.
        """
        try:
            st = os.stat(self.base_dir)
        except OSError as exc:
            path = os.path.join(self.base_dir, LOCK_FILENAME)
            raise StorageException(f"Failed to open history lock {path}: {exc}") from exc
        return ("fs", st.st_dev, st.st_ino)

    def _obj_put_reserved(self, rel: str, data: bytes) -> None:
        """Write the reserved root object `rel` directly, replacing a link instead of following it.

        The umask applies, as for objects → [ROADMAP.md](ROADMAP.md#ideas).
        """
        self._write_file(os.path.join(self.base_dir, rel), io.BytesIO(data), None, None)

    def _obj_get_reserved(self, rel: str) -> bytes | None:
        """Read the reserved root object `rel` directly, refusing a link, FIFO, or device.

        Raises:
            StorageException: If `rel` is not a regular file or the read fails.
        """
        try:
            with open(os.path.join(self.base_dir, rel), "rb", opener=regular_opener) as f:
                return f.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageException(f"Error reading {rel}: {exc}") from exc

    # =========================================================================
    # Path validation
    # =========================================================================

    def _validate_path(self, path: str) -> None:
        """Reject absolute paths and any traversal that escapes `base_dir`."""
        validate_path(self.base_dir, path)

    # =========================================================================
    # Object Operations - Read
    # =========================================================================

    def list(self) -> builtins.list[str]:
        """List all files in the storage, accounting for uncommitted changes."""
        logger.debug("Listing files in %s", self.base_dir)
        return self._overlay_list()

    def get(self, path: str) -> bytes:
        """Return the complete contents of the file at `path`.

        Raises:
            StorageNotFoundException: If the file does not exist.
            StorageException: If the file is marked for deletion or cannot be read.
        """
        logger.debug("Reading file: %s", path)
        with self.get_stream(path) as f:
            try:
                data = f.read()
            except OSError as exc:
                self.metrics.read_errors_inc()
                raise StorageException(f"Error reading {path}: {exc}") from exc
        self.metrics.read_bytes_inc(len(data))
        return data

    def get_metadata(self, path: str) -> dict[str, Any]:
        """Return extended-attribute metadata for `path`.

        A filesystem without xattr support yields ``{}`` → [STORAGE.md](STORAGE.md#storage-abi).

        Raises:
            StorageNotFoundException: If `path` does not exist.
            StorageException: If the attributes cannot be read.
        """
        self._validate_path(path)
        fpath = self._source_target(path)
        metadata: dict[str, Any] = {}

        with xattr_errors("read", path, fpath, not_found=True):
            for key, value in get_xattrs(fpath):
                try:
                    metadata[key.decode()] = value.decode()
                except UnicodeDecodeError:
                    continue

        logger.debug("Metadata keys for %s: %s", path, sorted(metadata))
        return metadata

    def exists(self, path: str) -> bool:
        """Return True iff `path` exists, accounting for uncommitted changes."""
        logger.debug("Checking existence: %s", path)
        self._validate_path(path)
        return self._overlay_exists(path)

    def get_stream(self, path: str) -> IO[bytes]:
        """Open `path` for streaming read.

        The caller is responsible for closing the returned handle.

        Raises:
            StorageNotFoundException: If the file does not exist.
            StorageException: If the file is marked for deletion or cannot be opened.
        """
        logger.debug("Opening stream for reading: %s", path)
        self._validate_path(path)
        fpath = self._read_target(path)

        try:
            # The resolved path holds no link, so one swapped in since resolution fails the open,
            # and a FIFO or device fails it instead of blocking the reader.
            handle = open(fpath, "rb", opener=regular_opener)
        except FileNotFoundError as exc:
            self.metrics.read_errors_inc()
            raise StorageNotFoundException(f"File not found: {path}") from exc
        except PermissionError as exc:
            self.metrics.read_errors_inc()
            raise StorageException(f"Permission denied: {path}") from exc
        except OSError as exc:
            self.metrics.read_errors_inc()
            raise StorageException(f"Error opening {path}: {exc}") from exc
        self.metrics.read_objects_inc()
        return handle

    # =========================================================================
    # Object Operations - Write
    # =========================================================================

    def put_stream(
        self,
        path: str,
        stream: IO[bytes],
        metadata: dict[str, str] | None = None,
        acl: str | None = None,
    ) -> None:
        """Atomically stream-write `stream` to `path`.

        An `acl` of three or four octal digits sets the file mode, masked to the permission
        bits; any other `acl` is ignored.

        Raises:
            StorageException: If `path` is a reserved storage-root object or escapes the root
                → [VALIDATION.md](VALIDATION.md#filename-safety), or the write fails.
        """
        self._validate_path(path)
        logger.debug("Writing file from stream: %s (acl=%s)", path, acl)
        with self._stage_write(path) as fpath:
            try:
                nbytes = self._write_file(fpath, stream, metadata, acl)
            except StorageException:
                self.metrics.write_errors_inc()
                raise
        self.metrics.write_bytes_inc(nbytes)
        self.metrics.write_objects_inc()

    def _write_file(
        self,
        fpath: str,
        stream: IO[bytes],
        metadata: dict[str, str] | None,
        acl: str | None,
    ) -> int:
        """Write `stream` to `fpath` through an exclusive temp file and a rename.

        The temp file has an unpredictable name and is created with ``O_EXCL | O_NOFOLLOW``,
        so a planted file or symlink fails the write instead of redirecting it, and it gets its
        final mode at creation. Metrics are the caller's → [STORAGE.md](STORAGE.md#storage-abi).

        Returns:
            The number of bytes written.

        Raises:
            StorageException: If the parent is missing or the write or rename fails.
        """
        tmp_path = temp_path(fpath)
        explicit_mode = _FILE_MODE_RE.fullmatch(acl) if acl is not None else None
        mode = int(explicit_mode[0], 8) & FILE_MODE_MASK if explicit_mode else _DEFAULT_FILE_MODE

        try:
            try:
                with open_exclusive(tmp_path, mode) as f:
                    if explicit_mode:
                        # The umask masks the creation mode; an explicit mode is set as given.
                        os.fchmod(f.fileno(), mode)
                    shutil.copyfileobj(stream, f, CHUNK_SIZE)
                    nbytes = f.tell()
                os.rename(tmp_path, fpath)
            except BaseException:
                cleanup_tmp_file(tmp_path)
                raise
        except FileNotFoundError as exc:
            raise StorageException(f"Parent directory not found: {fpath}") from exc
        except PermissionError as exc:
            raise StorageException(f"Permission denied: {fpath}") from exc
        except OSError as exc:
            raise StorageException(f"Error writing {fpath}: {exc}") from exc
        except StorageException:
            raise
        except Exception as exc:
            # Only the source stream raises past OSError, e.g. urllib3 under an HTTP transfer.
            raise StorageException(f"Error reading the stream for {fpath}: {exc}") from exc

        if metadata:
            try:
                set_xattrs(fpath, metadata)
            except OSError as exc:
                # The bytes are already in place: metadata on a write is best-effort
                # → [STORAGE.md](STORAGE.md#storage-abi).
                logger.warning("Failed to set xattrs of %s: %s", fpath, exc)
        return nbytes

    def add_metadata(
        self,
        path: str,
        metadata: dict[str, str],
        acl: str | None = None,
    ) -> None:
        """Set extended-attribute `metadata` on `path`.

        The `acl` argument is accepted for API compatibility with `StorageS3` and ignored
        here; only `put` / `put_stream` honour an octal mode `acl`
        → [STORAGE.md](STORAGE.md#backends). A filesystem without xattr support skips the
        write with a warning → [STORAGE.md](STORAGE.md#storage-abi).

        Raises:
            StorageException: If `path` does not exist or the attributes cannot be set.
        """
        del acl  # Filesystem has no ACL concept.
        self._validate_path(path)
        fpath = self._metadata_target(path)
        logger.debug("Setting metadata keys on %s: %s", fpath, sorted(metadata))
        with xattr_errors("set", path, fpath):
            set_xattrs(fpath, metadata)

    # =========================================================================
    # Object Operations - Delete
    # =========================================================================

    def delete(self, path: str) -> None:
        """Delete `path`, or stage its deletion inside a transaction.

        Raises:
            StorageNotFoundException: If `path` does not exist.
            StorageException: If `path` is a reserved storage-root object
                → [VALIDATION.md](VALIDATION.md#filename-safety), or the delete fails.
        """
        logger.debug("Deleting file: %s", path)
        self._validate_path(path)
        self._stage_delete(path)
        self.metrics.delete_objects_inc()

    def delete_metadata(self, path: str) -> None:
        """Remove every ``user.`` xattr, the txstore metadata, from `path`.

        A filesystem without xattr support has none to remove: a warning, no error
        → [STORAGE.md](STORAGE.md#storage-abi).

        Raises:
            StorageException: If `path` does not exist or an attribute cannot be removed.
        """
        self._validate_path(path)
        fpath = self._metadata_target(path)

        with xattr_errors("delete", path, fpath):
            remove_all_xattrs(fpath)

    def delete_metadata_fields(self, path: str, keys: builtins.list[str]) -> None:
        """Remove the named extended attributes from `path`; absent keys are ignored.

        A filesystem without xattr support has none to remove: a warning, no error
        → [STORAGE.md](STORAGE.md#storage-abi).

        Raises:
            StorageException: If `path` does not exist or a key cannot be removed, including
                one the OS cannot encode.
        """
        if not keys:
            return
        self._validate_path(path)
        fpath = self._metadata_target(path)

        with xattr_errors("delete", path, fpath):
            for key in keys:
                remove_xattr(fpath, key, missing_ok=True)

    # =========================================================================
    # Object Operations - Copy/Move
    # =========================================================================

    def copy(self, path_src: str, path_dst: str) -> None:
        """Copy `path_src` to `path_dst` within the storage.

        A missing parent of `path_dst` is created after the source check when the instance
        `auto_mkdir` is set, as for `put` → [STORAGE.md](STORAGE.md#storage-abi).

        Raises:
            StorageException: If `path_dst` is reserved, the root, or a directory
                → [VALIDATION.md](VALIDATION.md#filename-safety); if its parent is missing and
                `auto_mkdir` is false; or if the copy fails.
            StorageNotFoundException: If `path_src` does not exist.
        """
        self._validate_path(path_src)
        self._validate_path(path_dst)
        self._copy_object(path_src, path_dst)

    def move(self, path_src: str, path_dst: str) -> None:
        """Move `path_src` to `path_dst`.

        During a transaction this is `copy` followed by `delete(path_src)`, so both legs are
        tracked with footmarks; otherwise a rename. Paths that resolve to one object make it a
        no-op → [STORAGE.md](STORAGE.md#storage-abi). A missing parent is handled as by `copy`.

        Raises:
            StorageException: If either path names or resolves to a reserved storage-root
                object or the root, or either path is a directory
                → [VALIDATION.md](VALIDATION.md#filename-safety), checked before either leg
                runs; if the parent of `path_dst` is missing and `auto_mkdir` is false; or if
                the move fails.
            StorageNotFoundException: If `path_src` does not exist.
        """
        # Validate first, as every other verb does: the reserved check resolves the path, and
        # a NUL byte or lone surrogate would fail there with a raw ValueError.
        self._validate_path(path_src)
        self._validate_path(path_dst)
        logger.debug("Moving: %s -> %s", path_src, path_dst)
        self._move_object(path_src, path_dst)

    # =========================================================================
    # Directory Operations
    # =========================================================================

    def mkdir(self, path: str) -> None:
        """Create the directory at `path` (and parents).

        Raises:
            StorageException: If `path` names or resolves to a reserved storage-root object or
                the root → [VALIDATION.md](VALIDATION.md#filename-safety), or the directory
                cannot be created, including when a file occupies `path` or one of its parents.
        """
        self._validate_path(path)
        self._obj_check_reserved(path)
        dir_path = self._obj_full(path)
        logger.debug("Ensuring directory: %s", dir_path)
        # `exist_ok` accepts an existing directory and still refuses a file → STORAGE.md.
        try:
            pathlib.Path(dir_path).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageException(f"Failed to create directory {dir_path}: {exc}") from exc

    def rmdir(self, path: str) -> None:
        """Recursively remove the directory at `path`.

        Raises:
            StorageException: If `path` names or resolves to a reserved storage-root object or
                the root → [VALIDATION.md](VALIDATION.md#filename-safety), or the removal fails.
        """
        self._validate_path(path)
        self._obj_check_reserved(path)
        dir_path = self._obj_full(path)

        logger.debug("Removing directory: %s", dir_path)

        try:
            shutil.rmtree(dir_path)
        except OSError as exc:
            raise StorageException(f"Failed to remove directory {dir_path}: {exc}") from exc

    def format(self) -> None:
        """Reset the filesystem storage (NOT IMPLEMENTED).

        Raises:
            StorageException: Always. There is no audited safe ``format``
                implementation; callers should remove ``base_dir`` out-of-band.
        """
        raise StorageException(
            "StorageFS.format() is not implemented; delete the base directory manually."
        )
