"""StorageS3: S3 storage backend implementation.

Concrete `Storage` subclass for S3-compatible object storage. Supports:

  - Standard CRUD operations
  - Transactional operations with commit/rollback semantics (the full
    algorithm lives in `FootmarkMixin`; this module only supplies the
    backend primitives)
  - Object metadata management
  - Stream-based I/O
  - Canned ACL preservation across transactional copies via a per-instance
    sidecar → [STORAGE.md](STORAGE.md#s3-acl-rules)
"""

from __future__ import annotations

import builtins
import io
import logging
import os
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from typing import IO, Any, TypeIs, cast, get_args
from urllib.parse import urlunparse

from botocore.exceptions import ClientError

from txstore._reserved import LOCK_FILENAME
from txstore._s3_acl import is_public
from txstore._s3_client import S3_BACKEND_ERRORS, BucketCannedACL, ObjectCannedACL, S3Client
from txstore._s3_errors import (
    is_s3_bucket_not_found,
    is_s3_metadata_too_large,
    is_s3_not_found,
    s3_errors,
)
from txstore._s3_lock import s3_lock
from txstore.base import (
    CHUNK_SIZE,
    Storage,
    StorageException,
    StorageNotFoundException,
    StorageType,
)
from txstore.transaction import FootmarkMixin, TransactionModel
from txstore.url import (
    has_query_or_fragment,
    normalize_s3_prefix,
    parse_url,
    redact_url,
    strip_userinfo,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Object canned ACLs that grant world-readable access. Post-PUT public
# verification only runs when the effective ACL is one of these.
_PUBLIC_OBJECT_ACLS = frozenset({"public-read", "public-read-write"})

# `DeleteObjects` accepts at most this many keys per request.
_DELETE_BATCH_SIZE = 1000
# Canned ACL of the history and lock objects unless `NO_ACL` → STORAGE.md#s3-acl-rules.
RESERVED_ACL: ObjectCannedACL = "private"

_BUCKET_ACL_CHOICES: frozenset[str] = frozenset(get_args(BucketCannedACL))
_OBJECT_ACL_CHOICES: frozenset[str] = frozenset(get_args(ObjectCannedACL))


def is_bucket_acl(value: str) -> TypeIs[BucketCannedACL]:
    """Return True iff `value` is a bucket canned ACL; narrows it to `BucketCannedACL`."""
    return value in _BUCKET_ACL_CHOICES


def is_object_acl(value: str) -> TypeIs[ObjectCannedACL]:
    """Return True iff `value` is an object canned ACL; narrows it to `ObjectCannedACL`."""
    return value in _OBJECT_ACL_CHOICES


def parse_bucket_acl(value: str) -> BucketCannedACL:
    """Validate a canned ACL value for S3 bucket creation.

    Args:
        value: Raw argument or environment string.

    Raises:
        ValueError: If ``value`` is not a recognised bucket canned ACL.

    Returns:
        The validated literal value.
    """
    if not is_bucket_acl(value):
        raise ValueError(
            f"invalid S3 bucket ACL: {value!r} (choose from {sorted(_BUCKET_ACL_CHOICES)})"
        )
    return value


def parse_object_acl(value: str) -> ObjectCannedACL:
    """Validate a canned ACL value for S3 object operations.

    Args:
        value: Raw argument or environment string.

    Raises:
        ValueError: If ``value`` is not a recognised object canned ACL.

    Returns:
        The validated literal value.
    """
    if not is_object_acl(value):
        raise ValueError(
            f"invalid S3 object ACL: {value!r} (choose from {sorted(_OBJECT_ACL_CHOICES)})"
        )
    return value


class _ByteCounter:
    """Read-only, non-seekable view of a stream that counts the bytes read through it."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        """Read from the wrapped stream and count the bytes."""
        chunk = self._stream.read(size)
        self.count += len(chunk)
        return chunk


def _remaining_size(stream: IO[bytes]) -> int | None:
    """Return the bytes left in a seekable *stream* without consuming them, else None."""
    seekable = getattr(stream, "seekable", None)
    if seekable is None or not seekable():
        return None
    start = stream.tell()
    end = stream.seek(0, os.SEEK_END)
    stream.seek(start)
    return end - start


class StorageS3(FootmarkMixin, Storage):
    """S3 storage backend implementation."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_token: str,
        secret_token: str,
        prefix: str | None = None,
        verify: bool = True,
        create_bucket: bool = False,
        bucket_acl: BucketCannedACL | None = None,
        verify_public_acl: bool = True,
        default_acl: ObjectCannedACL | None = None,
    ) -> None:
        """Initialise an `StorageS3` instance.

        Args:
            endpoint: S3 service endpoint URL; any userinfo is dropped, since SigV4 signs
                with the keys and botocore's logs and error text echo the endpoint.
            bucket: Bucket name.
            access_token: Access key.
            secret_token: Secret key.
            prefix: Optional key prefix applied to every operation; trailing slashes are
                dropped → VALIDATION.md.
            verify: Verify SSL certificate.
            create_bucket: Auto-create the bucket if missing.
            bucket_acl: Canned ACL to set on bucket creation.
            verify_public_acl: When True (default), every successful `put` / `put_stream`
                whose effective ACL is a public canned ACL re-reads the object's ACL and raises
                `StorageException` if the AllUsers READ grant is missing. Explicit non-public
                ACLs (e.g. ``private``) skip the probe. Set to False for buckets that do not
                require a public-read invariant (tests, private buckets).
            default_acl: Canned ACL applied by ``put`` / ``put_stream`` when the caller passes
                ``acl=None``, so a deployment-wide ACL (e.g. ``public-read``) is chosen once
                instead of at every write site. ``None`` sends no ACL. It also decides the ACL
                of the history and lock objects → [STORAGE.md](STORAGE.md#s3-acl-rules).

        Raises:
            StorageException: If `endpoint` names no host → [STORAGE.md](STORAGE.md#construction),
                or `create_bucket` is set and the bucket cannot be created.
            ValueError: If the authority of `endpoint` does not parse or it carries a query or
                fragment, without naming it → [VALIDATION.md](VALIDATION.md#storage-urls), or
                `prefix` holds an empty segment → VALIDATION.md.
        """
        parts = parse_url(endpoint)
        # botocore echoes the endpoint in its errors, query included.
        if has_query_or_fragment(endpoint):
            raise ValueError("S3 endpoint carries a query or fragment: it takes neither")
        if not parts.hostname:
            raise StorageException(f"Invalid S3 endpoint: {redact_url(endpoint)}")
        self.endpoint = urlunparse(parts._replace(netloc=strip_userinfo(parts.netloc)))
        self.bucket = bucket
        self.prefix = normalize_s3_prefix(prefix)
        self.bucket_acl = bucket_acl
        self._verify_public_acl = verify_public_acl
        self._default_acl: ObjectCannedACL | None = default_acl
        super().__init__()

        logger.debug(
            "Init S3 storage class: %s bucket=%s prefix=%s",
            redact_url(self.endpoint),
            bucket,
            self.prefix,
        )

        self._s3_client = S3Client(self.endpoint, access_token, secret_token, verify=verify)
        # Full key → the verbatim canned ACL written to it in the active transaction, so its
        # internal copies (`_backup`, the staged metadata copy, commit of a PUT footmark)
        # re-apply exactly what the caller asked for; cleared when the next transaction begins
        # → [STORAGE.md](STORAGE.md#s3-acl-rules).
        self._acls: dict[str, ObjectCannedACL] = {}

        if create_bucket:
            self.create_bucket()

    def __repr__(self) -> str:
        """Return a string representation of the S3 storage."""
        return f"{self.get_type()} {redact_url(self.endpoint)}/{self.bucket}"

    def get_type(self) -> StorageType:
        """Return the storage backend type identifier."""
        return StorageType.s3

    # =========================================================================
    # Key Resolution
    # =========================================================================

    def _apply_prefix(self, path: str) -> str:
        """Prepend the configured prefix (if any) to `path`."""
        if self.prefix is not None:
            return f"{self.prefix}/{path}"
        return path

    def _strip_prefix(self, path: str) -> str:
        """Strip the configured prefix (if any) from `path`."""
        if self.prefix is not None and path.startswith(f"{self.prefix}/"):
            return path[len(self.prefix) + 1 :]
        return path

    # =========================================================================
    # Backend primitives (consumed by FootmarkMixin)
    # =========================================================================

    def _obj_full(self, rel: str) -> str:
        """Return the prefixed S3 key for a relative path."""
        return self._apply_prefix(rel)

    def _obj_iter_rel_all(self) -> Iterable[str]:
        """Return every key under the prefix as a relative key (prefix stripped), fully listed.

        Lists `<prefix>/`, not the bare prefix: S3 matches prefixes as plain strings, so a bare
        `pkg` would also return a sibling root's `pkg2/...` keys.
        """
        with s3_errors("Error listing S3 bucket"):
            keys = list(self._s3_client.bucket_list(self.bucket, prefix=self._apply_prefix("")))
        return [self._strip_prefix(key) for key in keys]

    def _obj_exists(self, full: str) -> bool:
        """Return True iff `full` exists; any error but "not found" → `StorageException`."""
        with s3_errors(f"Error checking S3 object {full}"):
            try:
                self._s3_client.object_head(self.bucket, full)
            except ClientError as exc:
                if is_s3_not_found(exc):
                    return False
                raise
            return True

    def _obj_copy(self, src_full: str, dst_full: str) -> None:
        """Copy `src_full` to `dst_full`, carrying the source's canned ACL.

        Inside a transaction the ACL recorded for the source wins and is recorded for the
        destination; otherwise the copy carries the source's group canned ACL
        → [STORAGE.md](STORAGE.md#s3-acl-rules).
        """
        in_txn = self.in_transaction()
        acl = self._s3_client.object_copy(
            self.bucket,
            src_full,
            self.bucket,
            dst_full,
            acl=self._acls.get(src_full) if in_txn else None,
        )
        if in_txn:
            self._record_acl(dst_full, acl)

    def _obj_delete(self, full: str) -> None:
        """Hard-delete the object at `full`."""
        self._s3_client.object_delete(self.bucket, full)

    def _obj_reclaim_delete_many(
        self, rels: Sequence[str]
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Delete listed footmarks with ``DeleteObjects``, 1000 keys per request.

        One round trip per chunk instead of one per key, on the keys
        `_obj_checked_footmark` returns, as `_reclaim_footmark` deletes. A refused
        footmark or a per-key ``Errors`` entry becomes a failure; a key the response
        names in neither list is reported as failed rather than assumed gone, so the
        reclaim result and its orphan-sweep warning stay accurate. A chunk whose call
        fails as a whole falls back to the per-key default.

        Args:
            rels: Relative paths of footmark objects to remove.

        Returns:
            ``(removed, failed)`` as `FootmarkMixin._obj_reclaim_delete_many`.
        """
        removed: list[str] = []
        failed: list[tuple[str, str]] = []
        for start in range(0, len(rels), _DELETE_BATCH_SIZE):
            by_key: dict[str, str] = {}
            for rel in rels[start : start + _DELETE_BATCH_SIZE]:
                try:
                    by_key[self._obj_checked_footmark(rel)] = rel
                except StorageNotFoundException:
                    removed.append(rel)
                except StorageException as exc:
                    failed.append((rel, str(exc)))
            if not by_key:
                continue
            try:
                deleted, errors = self._s3_client.object_delete_many(self.bucket, list(by_key))
            except S3_BACKEND_ERRORS as exc:
                logger.warning(
                    "Batched footmark delete failed, deleting one key at a time: %s", exc
                )
                chunk_removed, chunk_failed = super()._obj_reclaim_delete_many(
                    list(by_key.values())
                )
                removed.extend(chunk_removed)
                failed.extend(chunk_failed)
                continue
            seen: set[str] = set()
            for key in deleted:
                if key in by_key:
                    removed.append(by_key[key])
                    seen.add(key)
            for key, reason in errors:
                if key in by_key:
                    failed.append((by_key[key], reason))
                    seen.add(key)
            failed.extend(
                (rel, "missing from DeleteObjects response")
                for key, rel in by_key.items()
                if key not in seen
            )
        return removed, failed

    def _obj_touch(self, full: str) -> None:
        """Create an empty marker object at `full`."""
        self._s3_client.object_put(self.bucket, full, b"")

    def _reserved_acl(self) -> ObjectCannedACL | None:
        """ACL every reserved object is written with: `RESERVED_ACL`, the default, or none.

        `make_storage` maps `NO_ACL` to no default ACL. Then the bucket's policy and ownership
        settings decide access. A bucket that enforces bucket-owner ownership refuses every ACL
        but ``bucket-owner-full-control``, so that default is kept rather than replaced by
        `RESERVED_ACL` → [STORAGE.md](STORAGE.md#s3-acl-rules).
        """
        if self._default_acl is None or self._default_acl == "bucket-owner-full-control":
            return self._default_acl
        return RESERVED_ACL

    def _obj_lock(self) -> AbstractContextManager[None]:
        """Return the conditional-write lock on ``.transactions.lock`` → `s3_lock`."""
        return s3_lock(
            self._s3_client,
            self.bucket,
            self._obj_full(LOCK_FILENAME),
            self._reserved_acl(),
        )

    def _obj_lock_identity(self) -> tuple[str, str, str, str]:
        """Return endpoint, bucket, and lock key → [STORAGE.md](STORAGE.md#locking)."""
        return ("s3", self.endpoint, self.bucket, self._obj_full(LOCK_FILENAME))

    def _obj_put_reserved(self, rel: str, data: bytes) -> None:
        """Write the reserved root object `rel` directly → `_reserved_acl`."""
        self._upload(rel, self._obj_full(rel), io.BytesIO(data), None, self._reserved_acl())

    def _obj_get_reserved(self, rel: str) -> bytes | None:
        """Read the reserved root object `rel` directly; ``None`` when it is missing."""
        with s3_errors(f"Error reading S3 object {rel}"):
            try:
                body, _etag = self._s3_client.object_get_with_etag(self.bucket, self._obj_full(rel))
            except ClientError as exc:
                if is_s3_not_found(exc):
                    return None
                raise
        return body

    def _record_acl(self, full_key: str, acl: ObjectCannedACL | None) -> None:
        """Remember the canned ACL written to `full_key` in this transaction.

        A write without one drops the entry, so an earlier ACL never outlives the object it was
        written with → [STORAGE.md](STORAGE.md#s3-acl-rules).
        """
        if acl:
            self._acls[full_key] = acl
        else:
            self._acls.pop(full_key, None)

    def transaction_begin(self, transaction_id: int = 0) -> TransactionModel:
        """Begin a transaction with an empty ACL sidecar → `FootmarkMixin.transaction_begin`."""
        model = super().transaction_begin(transaction_id)
        self._acls.clear()
        return model

    # =========================================================================
    # Public ACL probe
    # =========================================================================

    def verify_public(self, path: str) -> None:
        """Raise `StorageException` if `path` does not grant AllUsers READ."""
        self._require_public(path, self._apply_prefix(path))

    def _require_public(
        self, path: str, full_key: str, on_missing: Callable[[], None] | None = None
    ) -> None:
        """Raise unless `full_key`, the key `path` names now, grants AllUsers READ.

        `full_key` is the staged footmark inside a transaction, so a post-write probe checks
        what was written, not the committed original → [STORAGE.md](STORAGE.md#s3-acl-rules).
        `on_missing` runs only when the grant is absent, not when the probe itself fails.
        """
        if not self._is_public(full_key):
            if on_missing is not None:
                on_missing()
            raise StorageException(f"Object verify {path}: has not public ACL")

    def _is_public(self, full_key: str) -> bool:
        """Return True iff the live ACL on `full_key` grants AllUsers READ → `is_public`."""
        return is_public(self._s3_client, self.bucket, full_key)

    def _verify_public_if_required(
        self, path: str, full_key: str, acl: ObjectCannedACL | None
    ) -> None:
        """Raise if a public ACL was requested but AllUsers READ is missing."""
        if not self._verify_public_acl or acl not in _PUBLIC_OBJECT_ACLS:
            return
        self._require_public(path, full_key, self.metrics.write_errors_inc)

    # =========================================================================
    # Object Operations - Read
    # =========================================================================

    def list(self) -> builtins.list[str]:
        """List bucket objects, overlaying uncommitted changes."""
        logger.debug("list S3 objects from bucket %s ...", self.bucket)
        return self._overlay_list()

    def get(self, path: str) -> bytes:
        """Retrieve the content of `path` from the bucket."""
        logger.debug("S3 GET %s...", path)
        with (
            self.get_stream(path) as stream,
            s3_errors(f"Error reading S3 object {path}", on_error=self.metrics.read_errors_inc),
        ):
            data = stream.read()
        self.metrics.read_bytes_inc(len(data))
        return data

    def get_metadata(self, path: str) -> dict[str, Any]:
        """Return the metadata of `path`."""
        logger.debug("S3 GET metadata %s...", path)
        full_key = self._source_target(path)
        with s3_errors(
            f"Error reading S3 object metadata {path}", not_found=f"S3 object not found: {path}"
        ):
            return self._s3_client.object_get_metadata(self.bucket, full_key)

    def exists(self, path: str) -> bool:
        """Return True iff `path` exists in the bucket."""
        logger.debug("S3 EXISTS %s...", path)
        return self._overlay_exists(path)

    def get_stream(self, path: str) -> IO[bytes]:
        """Open a streaming read of `path`."""
        logger.debug("S3 GET (stream) %s...", path)
        full_key = self._read_target(path)
        with s3_errors(
            f"Error reading S3 object {path}",
            on_error=self.metrics.read_errors_inc,
            not_found=f"S3 object not found: {path}",
        ):
            stream = self._s3_client.object_get_stream(self.bucket, full_key)
        self.metrics.read_objects_inc()
        return cast(IO[bytes], stream)

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
        """Stream-put `stream` to the bucket at `path`.

        Whether the post-write public-ACL check runs is controlled by the
        instance-level `verify_public_acl` flag and the effective ACL
        (only public canned ACLs are probed).

        Raises:
            StorageException: If `path` is a reserved storage-root object
                → [VALIDATION.md](VALIDATION.md#filename-safety), or the write fails.
            ValueError: If `acl` is not an object canned ACL
                → [VALIDATION.md](VALIDATION.md#s3-canned-acls).
        """
        acl = self._default_acl if acl is None else parse_object_acl(acl)
        logger.debug("S3 PUT (stream) %s... (acl=%s)", path, acl)
        with self._stage_write(path) as full_key:
            try:
                nbytes = self._upload(path, full_key, stream, metadata, acl)
            except StorageException:
                self.metrics.write_errors_inc()
                raise
        self.metrics.write_bytes_inc(nbytes)
        self.metrics.write_objects_inc()
        if self.in_transaction():
            self._record_acl(full_key, acl)
        self._verify_public_if_required(path, full_key, acl)

    def _upload(
        self,
        path: str,
        full_key: str,
        stream: IO[bytes],
        metadata: dict[str, str] | None,
        acl: ObjectCannedACL | None,
    ) -> int:
        """Upload `stream` to `full_key`; shared by user and reserved writes.

        Metrics are the caller's, from the raised error and the returned size
        → [STORAGE.md](STORAGE.md#storage-abi).

        Returns:
            The number of bytes uploaded.
        """
        # A seekable stream is measured, since boto3 may seek and re-read it; any other is
        # counted as it is read.
        size = _remaining_size(stream)
        counter = _ByteCounter(stream)
        try:
            with s3_errors(f"Error writing S3 object {path}"):
                self._s3_client.object_upload(
                    stream if size is not None else cast(IO[bytes], counter),
                    self.bucket,
                    full_key,
                    metadata=metadata,
                    io_chunksize=CHUNK_SIZE,
                    acl=acl,
                )
        except StorageException:
            raise
        except Exception as exc:
            # Only the source stream raises past the backend errors, e.g. urllib3 under an HTTP
            # transfer.
            raise StorageException(f"Error reading the stream for S3 object {path}: {exc}") from exc
        return counter.count if size is None else size

    def add_metadata(
        self,
        path: str,
        metadata: dict[str, str],
        acl: str | None = None,
    ) -> None:
        """Merge `metadata` (and optional `acl`) onto `path` (supports >5 GB objects).

        When the merged metadata exceeds the S3 user-metadata limit (``MetadataTooLarge``), the
        existing keys are dropped and only `metadata` is written
        → [STORAGE.md](STORAGE.md#s3-metadata-rewrites).

        Without `acl` the object keeps its ACL, as the other metadata rewrites do; `default_acl`
        applies only to content writes → [STORAGE.md](STORAGE.md#s3-acl-rules).

        Raises:
            ValueError: If `acl` is not an object canned ACL
                → [VALIDATION.md](VALIDATION.md#s3-canned-acls).
        """
        acl = None if acl is None else parse_object_acl(acl)
        full_key = self._metadata_target(path)

        logger.debug("S3 add metadata keys %s to %s (acl=%s)...", sorted(metadata), full_key, acl)
        with s3_errors(f"Error writing metadata {path}"):
            try:
                self._s3_client.object_put_metadata(self.bucket, full_key, metadata, acl=acl)
            except ClientError as exc:
                if not is_s3_metadata_too_large(exc):
                    raise
                logger.warning(
                    "Merged metadata of %s exceeds the S3 limit; replacing it with keys %s",
                    path,
                    sorted(metadata),
                )
                self._s3_client.object_replace_metadata(self.bucket, full_key, metadata, acl=acl)
        # Recorded once the rewrite landed, and only an explicit ACL: unlike a content write, a
        # rewrite without `acl` keeps the key's sidecar entry, whichever write recorded it
        # → [STORAGE.md](STORAGE.md#s3-acl-rules).
        if acl is not None and self.in_transaction():
            self._record_acl(full_key, acl)

    # =========================================================================
    # Object Operations - Delete
    # =========================================================================

    def delete(self, path: str) -> None:
        """Delete `path` from the bucket (or stage a DELETE marker in a txn).

        Raises:
            StorageNotFoundException: If `path` does not exist.
            StorageException: If `path` is a reserved storage-root object
                → [VALIDATION.md](VALIDATION.md#filename-safety), or the delete fails.
        """
        logger.debug("S3 DELETE %s...", path)
        self._stage_delete(path)
        self.metrics.delete_objects_inc()

    def delete_metadata(self, path: str) -> None:
        """Remove every metadata field from `path`."""
        logger.debug("S3 delete all metadata in %s...", path)
        full_key = self._metadata_target(path)
        with s3_errors(f"Error deleting metadata {path}"):
            self._s3_client.object_delete_metadata(self.bucket, full_key, None)

    def delete_metadata_fields(self, path: str, keys: builtins.list[str]) -> None:
        """Remove the named metadata `keys` from `path`; absent keys are ignored."""
        logger.debug("S3 delete metadata %s in %s...", keys, path)
        if not keys:
            return
        full_key = self._metadata_target(path)
        with s3_errors(f"Error deleting metadata {path}"):
            self._s3_client.object_delete_metadata(self.bucket, full_key, keys)

    # =========================================================================
    # Object Operations - Copy/Move
    # =========================================================================

    def copy(self, path_src: str, path_dst: str) -> None:
        """Copy an object within the bucket, with its canned ACL.

        Raises:
            StorageNotFoundException: If `path_src` does not exist.
        """
        logger.debug("S3 copy %s to %s...", path_src, path_dst)
        self._copy_object(path_src, path_dst)

    def move(self, path_src: str, path_dst: str) -> None:
        """Move an object within the bucket (implemented as copy + delete).

        Moving a key onto itself is a no-op → [STORAGE.md](STORAGE.md#storage-abi).

        Raises:
            StorageException: If either path is a reserved storage-root object
                → [VALIDATION.md](VALIDATION.md#filename-safety), checked before the copy leg
                runs, or a leg fails.
            StorageNotFoundException: If `path_src` does not exist.
        """
        logger.debug("S3 move %s to %s...", path_src, path_dst)
        self._move_object(path_src, path_dst)

    # =========================================================================
    # Directory Operations (no-op on S3)
    # =========================================================================

    def mkdir(self, path: str) -> None:
        """No-op: S3 directories are virtual (key prefixes)."""
        logger.debug("S3 mkdir %s (no-op)", path)

    def rmdir(self, path: str) -> None:
        """No-op: S3 directories are virtual (key prefixes)."""
        logger.debug("S3 rmdir %s (no-op)", path)

    # =========================================================================
    # Bucket Operations
    # =========================================================================

    def exists_bucket(self) -> bool:
        """Return True iff the bucket exists."""
        with s3_errors(f"Error checking S3 bucket {self.bucket}"):
            try:
                self._s3_client.bucket_head(self.bucket)
            except ClientError as exc:
                if is_s3_bucket_not_found(exc):
                    return False
                raise
            return True

    def create_bucket(self) -> None:
        """Create the bucket if missing."""
        if self.exists_bucket():
            return
        logger.info("Create S3 bucket %s.", self.bucket)
        with s3_errors(f"Error creating S3 bucket {self.bucket}"):
            self._s3_client.bucket_create(self.bucket, acl=self.bucket_acl)

    def delete_bucket(self) -> None:
        """Delete the bucket (incl. non-empty contents)."""
        if not self.exists_bucket():
            return
        logger.debug("Delete S3 bucket %s.", self.bucket)
        with s3_errors(f"Error deleting S3 bucket {self.bucket}"):
            self._s3_client.bucket_delete(self.bucket, delete_nonempty=True)

    def format(self) -> None:
        """Reset the bucket (delete + recreate).

        Raises:
            StorageException: If a prefix is set: the reset would also delete every key outside
                it → [STORAGE.md](STORAGE.md#backends).
        """
        if self.prefix is not None:
            raise StorageException(
                f"Refusing to format a prefixed storage: deleting bucket {self.bucket} would "
                f"also delete every key outside {self.prefix}/"
            )
        self.delete_bucket()
        self.create_bucket()
