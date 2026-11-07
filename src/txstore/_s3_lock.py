"""S3 writer lock for the history: conditional writes on one lock object.

`s3_lock` creates the lock object with ``IfNoneMatch=*``; a crashed holder is taken over after
`LOCK_TTL` by overwriting its body under ``IfMatch``, so breaking and acquiring are one atomic
step and a peer that acquires first is never robbed. Release deletes under ``IfMatch`` →
[STORAGE.md](STORAGE.md#locking).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from botocore.exceptions import ClientError

from txstore._s3_client import ObjectCannedACL, S3Client
from txstore._s3_errors import (
    is_s3_access_denied,
    is_s3_conditional_conflict,
    is_s3_not_found,
    is_s3_precondition_failed,
    s3_errors,
)
from txstore.base import StorageException

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

LOCK_TTL = timedelta(hours=12)
# A stamp further ahead than this is corrupt: one TTL of headroom for a fast holder clock.
LOCK_MAX_AHEAD = 2 * LOCK_TTL
LOCK_ACQUIRE_TIMEOUT_S = 300.0
LOCK_RETRY_SLEEP_S = 0.25
# Releasing is the only step that cannot be retried by the next writer: an
# undeleted lock blocks every peer until the TTL expires, so retry the delete.
LOCK_RELEASE_ATTEMPTS = 3
LOCK_RELEASE_SLEEP_S = 0.5


class _LockObject(NamedTuple):
    """Where the lock object lives."""

    client: S3Client
    bucket: str
    key: str
    acl: ObjectCannedACL | None


class _ReadFailureLog:
    """Logs a failed lock read at WARNING once per acquire, then at DEBUG, and keeps the last.

    The acquire retries every `LOCK_RETRY_SLEEP_S`, so an endpoint failing every read would
    otherwise log one warning per retry until the acquire timeout. The last failure is carried
    by the timeout, so a caller without logging still learns why the lock was never read.
    """

    def __init__(self) -> None:
        self.count = 0
        self.last: StorageException | None = None

    def report(self, key: str, exc: StorageException) -> None:
        self.count += 1
        self.last = exc
        level = logging.WARNING if self.count == 1 else logging.DEBUG
        logger.log(level, "Cannot read S3 history lock %s (will retry): %s", key, exc)


class _LockState(NamedTuple):
    """One consistent read of the lock object: its version tag and its body.

    Args:
        etag: Version the body was read with; the take-over writes against it.
        body: Parsed lock body, or ``None`` when present but unparseable —
            which still counts as breakable.
    """

    etag: str
    body: dict[str, object] | None


@contextmanager
def s3_lock(client: S3Client, bucket: str, key: str, acl: ObjectCannedACL | None) -> Iterator[None]:
    """Hold the lock object *key*: conditional create, else take over a breakable lock.

    Args:
        client: S3 client of the storage.
        bucket: Bucket holding the lock object.
        key: Full key of the lock object.
        acl: Canned ACL every lock write carries; ``None`` sends no ACL header.

    Yields:
        None while the lock is held.

    Raises:
        StorageException: If the lock is not acquired within `LOCK_ACQUIRE_TIMEOUT_S`, chained
            from the last failed lock read when there was one; or if a write fails for a reason
            other than a lost race.
    """
    lock = _LockObject(client, bucket, key, acl)
    owner = str(uuid.uuid4())
    deadline = time.monotonic() + LOCK_ACQUIRE_TIMEOUT_S
    read_failures = _ReadFailureLog()
    while True:
        etag = _try_create(lock, owner)
        if etag is not None:
            break
        etag = _try_steal_breakable(lock, owner, read_failures)
        if etag is not None:
            break
        if time.monotonic() >= deadline:
            message = f"Timed out acquiring history lock {key} after {LOCK_ACQUIRE_TIMEOUT_S:.0f}s"
            last = read_failures.last
            if last is not None:
                message += f"; last lock read failed: {last}"
            raise StorageException(message) from last
        time.sleep(LOCK_RETRY_SLEEP_S)

    logger.debug("Acquired S3 history lock %s owner=%s", key, owner)
    try:
        yield
    finally:
        _release(lock, etag, owner)


def _lock_body(owner: str) -> bytes:
    """Serialise a lock body claiming ownership until ``now + LOCK_TTL``."""
    return json.dumps(
        {"owner": owner, "expires_at": (datetime.now(UTC) + LOCK_TTL).isoformat()},
        separators=(",", ":"),
    ).encode("utf-8")


def _require_etag(lock: _LockObject, etag: str) -> str:
    """Return *etag*, failing loudly when the endpoint omitted it.

    Raises:
        StorageException: If *etag* is empty. Without a version tag the lock can neither be
            taken over nor released safely, so never fall back to an unconditional write.
    """
    if not etag:
        raise StorageException(f"S3 response for {lock.key} carried no ETag")
    return etag


def _etag_or_drop(lock: _LockObject, etag: str) -> str:
    """Return the ETag of a lock write, deleting the object when none was reported.

    The write already happened, so raising here would abandon a lock body that is inside its
    TTL and block every peer until it expires. The just-written body cannot be broken by anyone
    yet, so an unconditional best-effort delete is safe and leaves the root usable.

    Raises:
        StorageException: If the endpoint omitted the ETag.
    """
    if not etag:
        try:
            lock.client.object_delete(lock.bucket, lock.key)
        except Exception:
            logger.exception("Failed to drop un-taggable S3 history lock %s", lock.key)
    return _require_etag(lock, etag)


def _conditional_write_lost(exc: ClientError) -> bool:
    """Return True if a conditional write lost the race for the lock.

    Covers a refused precondition, a concurrent-write conflict, and a vanished object: a
    conditional write against a missing key answers ``NoSuchKey`` on some endpoints and 412 on
    others. All mean a peer touched the lock, so retry from the top.
    """
    return is_s3_precondition_failed(exc) or is_s3_conditional_conflict(exc) or is_s3_not_found(exc)


def _try_create(lock: _LockObject, owner: str) -> str | None:
    """Create the lock object if absent.

    Returns:
        The new ETag, or ``None`` when a lock object already exists or a concurrent conditional
        write conflicted; both mean "retry".

    Raises:
        StorageException: On any failure other than the refused write, including the
            connection errors botocore reports as ``BotoCoreError``.
    """
    with s3_errors("Failed to acquire history lock"):
        try:
            etag = lock.client.object_put_if_absent(
                lock.bucket, lock.key, _lock_body(owner), lock.acl
            )
        except ClientError as exc:
            if is_s3_precondition_failed(exc) or is_s3_conditional_conflict(exc):
                return None
            raise
    return _etag_or_drop(lock, etag)


def _read_state(lock: _LockObject) -> _LockState | None:
    """Read the lock body and its ETag from one response.

    Both must come from the same read: judging expiry on one version and authorising the
    take-over with another reopens the race this design closes.

    Returns:
        The lock state, or ``None`` when the object is absent. A present but unparseable body
        yields ``body=None`` and stays breakable.

    Raises:
        StorageException: On transient / non-404 read failures. Callers must not treat those as
            a breakable lock — retry instead of taking over.
    """
    with s3_errors("Failed to read history lock"):
        try:
            raw, etag = lock.client.object_get_with_etag(lock.bucket, lock.key)
        except ClientError as exc:
            if is_s3_not_found(exc):
                return None
            raise
    etag = _require_etag(lock, etag)
    try:
        data = json.loads(raw)
    # Not only JSONDecodeError: too-deep nesting and an int past the digit limit raise too.
    except (ValueError, RecursionError):
        return _LockState(etag, None)
    return _LockState(etag, data if isinstance(data, dict) else None)


def _body_is_breakable(data: dict[str, object]) -> bool:
    """Return True if *data* is past ``expires_at`` or corrupt: no, unparseable, or over-cap."""
    expires_raw = data.get("expires_at")
    if not isinstance(expires_raw, str):
        return True
    try:
        expires_at = datetime.fromisoformat(expires_raw)
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    return now >= expires_at or expires_at - now > LOCK_MAX_AHEAD


def _try_take_over(lock: _LockObject, owner: str, etag: str) -> str | None:
    """Replace the lock body while it still matches *etag*.

    Returns:
        The new ETag on success, or ``None`` when a peer changed the lock first.

    Raises:
        StorageException: On any failure other than the refused write, including the
            connection errors botocore reports as ``BotoCoreError``.
    """
    with s3_errors("Failed to take over history lock"):
        try:
            new_etag = lock.client.object_put_if_match(
                lock.bucket, lock.key, _lock_body(owner), lock.acl, etag
            )
        except ClientError as exc:
            if _conditional_write_lost(exc):
                return None
            raise
    return _etag_or_drop(lock, new_etag)


def _try_steal_breakable(
    lock: _LockObject, owner: str, read_failures: _ReadFailureLog
) -> str | None:
    """Take the lock over if one consistent read shows it is breakable.

    A missing, corrupt, or past-TTL body is breakable. The take-over is a single conditional
    write against the ETag read alongside that body, so a peer that acquires in between refuses
    it instead of losing its own lock. A live body carrying *owner* is already held: a retried
    create or take-over whose first attempt committed answers 412 to its replay.

    Args:
        lock: The lock object.
        owner: Owner id of this acquire, unique per `s3_lock` call.
        read_failures: The acquire's read-failure log; it warns once, then logs at DEBUG.

    Returns:
        The ETag when the lock was taken over or found held by *owner*, otherwise ``None``.

    Raises:
        StorageException: If the read is refused with a 403: no retry within the acquire timeout
            can clear it, as for a refused write.
    """
    try:
        state = _read_state(lock)
    except StorageException as exc:
        # `s3_errors` chains the boto3 failure, so the cause carries the status.
        if is_s3_access_denied(exc.__cause__):
            raise
        read_failures.report(lock.key, exc)
        return None
    if state is None:
        return None
    if state.body is not None and state.body.get("owner") == owner:
        # A retried write whose first attempt committed reads back as refused.
        logger.warning("Adopted own S3 history lock %s after a lost write response", lock.key)
        return state.etag
    if state.body is not None and not _body_is_breakable(state.body):
        return None
    etag = _try_take_over(lock, owner, state.etag)
    if etag is not None:
        logger.warning("Broke stale S3 history lock %s owner=%s", lock.key, owner)
    return etag


def _delete(lock: _LockObject, etag: str) -> bool:
    """Delete the lock object while it still matches *etag*.

    Returns:
        True when our own version was deleted; False when the object is gone already or now
        holds another version, so a peer's lock is left alone.

    Raises:
        StorageException: On any other delete failure, including the connection errors botocore
            reports as ``BotoCoreError``. `_release` retries those.
    """
    with s3_errors("Failed to release history lock"):
        try:
            lock.client.object_delete_if_match(lock.bucket, lock.key, etag)
        except ClientError as exc:
            if is_s3_not_found(exc):
                return False
            if is_s3_precondition_failed(exc):
                logger.warning("S3 history lock %s changed version; skip delete", lock.key)
                return False
            raise
    return True


def _release(lock: _LockObject, etag: str, owner: str) -> None:
    """Release the lock, retrying a failed delete before giving up.

    A lock the holder fails to delete is not recoverable by the next writer: its body is still
    inside the TTL, so every peer waits out the acquire timeout and fails until `LOCK_TTL`
    expires. A transient delete error must therefore be retried rather than logged once.
    """
    for attempt in range(1, LOCK_RELEASE_ATTEMPTS + 1):
        try:
            if _delete(lock, etag):
                logger.debug("Released S3 history lock %s owner=%s", lock.key, owner)
            return
        except Exception:
            if attempt < LOCK_RELEASE_ATTEMPTS:
                logger.warning(
                    "Failed to release S3 history lock %s (attempt %d/%d) -- retrying",
                    lock.key,
                    attempt,
                    LOCK_RELEASE_ATTEMPTS,
                    exc_info=True,
                )
                time.sleep(LOCK_RELEASE_SLEEP_S)
                continue
            logger.exception(
                "Failed to release S3 history lock %s after %d attempts; writers on this root "
                "stay blocked until the lock expires -- delete %s to recover",
                lock.key,
                LOCK_RELEASE_ATTEMPTS,
                lock.key,
            )
