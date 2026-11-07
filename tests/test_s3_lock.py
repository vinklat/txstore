"""Tests for txstore/_s3_lock.py: the conditional-write history lock on S3
(STORAGE.md#locking), against an in-process moto S3 server.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Self
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from helpers.backends import backend_unavailable
from helpers.s3 import client_error, s3_bucket, s3_storage
from helpers.storage import LOCK_FILENAME, put_then_fail

from txstore import (
    StorageException,
    StorageS3,
    TransactionHistory,
    history_lock,
    maybe_transaction,
)
from txstore import _s3_lock as lock_mod
from txstore._s3_lock import (
    _body_is_breakable,
    _conditional_write_lost,
    _delete,
    _LockObject,
    _read_state,
    _ReadFailureLog,
    _release,
    _try_create,
    _try_steal_breakable,
    _try_take_over,
)


@pytest.fixture
def storage(moto_server: str) -> Iterator[StorageS3]:
    with s3_bucket(moto_server) as s:
        yield s


def _lock(storage: StorageS3) -> _LockObject:
    """The lock object of *storage*, as `StorageS3._obj_lock` names it."""
    return _LockObject(
        storage._s3_client,
        storage.bucket,
        storage._obj_full(LOCK_FILENAME),
        storage._reserved_acl(),
    )


def test_lock_constants_match_the_contract() -> None:
    """Behaviour tests override these to run fast, so only this pins the STORAGE.md values."""
    assert lock_mod.LOCK_TTL == timedelta(hours=12)
    assert lock_mod.LOCK_MAX_AHEAD == 2 * lock_mod.LOCK_TTL
    assert lock_mod.LOCK_ACQUIRE_TIMEOUT_S == 300
    assert lock_mod.LOCK_RETRY_SLEEP_S == 0.25
    assert lock_mod.LOCK_RELEASE_ATTEMPTS == 3
    assert lock_mod.LOCK_RELEASE_SLEEP_S == 0.5


def test_s3_history_lock_serialises_maybe_transaction(storage_public_verify: StorageS3) -> None:
    """Two sequential maybe_transaction commits both appear in S3 history."""
    with maybe_transaction(storage_public_verify):
        storage_public_verify.put("a.deb", b"a")
    with maybe_transaction(storage_public_verify):
        storage_public_verify.put("b.deb", b"b")

    records = TransactionHistory(storage_public_verify).load()
    assert len(records) == 2
    assert {m.id for m in records} == {1, 2}
    assert not storage_public_verify.exists(LOCK_FILENAME)


def test_s3_history_lock_released_on_failure(storage: StorageS3) -> None:

    with pytest.raises(RuntimeError, match="boom"):
        put_then_fail(storage)

    assert not storage.exists(LOCK_FILENAME)

    with maybe_transaction(storage):
        storage.put("y.deb", b"y")

    records = TransactionHistory(storage).load()
    assert len(records) == 1
    assert records[0].id == 1


def test_s3_history_lock_if_none_match_blocks_second_holder(storage: StorageS3) -> None:
    """While a lock is held, a second acquire attempt must not create a second lock."""
    with history_lock(storage):
        assert storage.exists(LOCK_FILENAME)
        assert _try_create(_lock(storage), owner="other") is None
    assert not storage.exists(LOCK_FILENAME)
    # A create that wins returns the version tag the holder releases against.
    assert isinstance(_try_create(_lock(storage), owner="solo"), str)


def test_s3_history_lock_serialises_concurrent_writers(
    storage: StorageS3, moto_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two real threads racing on the S3 IfNoneMatch lock must both commit distinctly.

    The peer S3 lock tests drive the private helpers synchronously; this one proves the
    conditional-put actually serialises genuine concurrent writers, as the FS backend is
    covered in `test_history.py`.
    """
    # No pytest-timeout plugin is installed and the default acquire timeout is 300s,
    # so cap it: a serialisation regression must fail fast instead of hanging the suite.
    monkeypatch.setattr(lock_mod, "LOCK_ACQUIRE_TIMEOUT_S", 20.0)

    errors: list[BaseException] = []
    started = threading.Barrier(2)

    def worker(name: str) -> None:
        try:
            # Instances are not thread-safe -> one per thread (STORAGE.md footmarks).
            local = s3_storage(moto_server, storage.bucket)
            started.wait(timeout=10)
            with maybe_transaction(local):
                # Hold the critical section so the peer genuinely contends on the lock.
                time.sleep(0.05)
                local.put(f"{name}.deb", name.encode())
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
    records = TransactionHistory(storage).load()
    assert len(records) == 2
    assert {m.id for m in records} == {1, 2}
    assert storage.exists("a.deb")
    assert storage.exists("b.deb")
    assert not storage.exists(LOCK_FILENAME)


def test_s3_lock_transient_read_does_not_count_as_expired(storage: StorageS3) -> None:
    """A non-404 read failure must not be treated as a breakable lock.

    The lock is read through the raw client (body and ETag must arrive together),
    so the patch has to target `get_object` — patching `storage.get` would
    intercept nothing and leave this test vacuous.
    """
    throttled = client_error("SlowDown", 503, "GetObject")
    with history_lock(storage):
        before = storage.get(LOCK_FILENAME)
        client = storage._s3_client._client
        with patch.object(client, "get_object", side_effect=throttled):
            with pytest.raises(StorageException, match="Failed to read history lock"):
                _read_state(_lock(storage))
            assert _try_steal_breakable(_lock(storage), "thief", _ReadFailureLog()) is None
        # Byte-identical: the failed read must not have issued any take-over.
        assert storage.get(LOCK_FILENAME) == before
        assert json.loads(before)["owner"]


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(b"not-json", id="not-json"),
        pytest.param(b"[" * 100_000 + b"]" * 100_000, id="too-deep"),
        pytest.param(b'{"expires_at": ' + b"1" * 5000 + b"}", id="int-digit-limit"),
        # valid JSON that is not an object; `null` is left out, since it already reads as None
        pytest.param(b"[]", id="array"),
        pytest.param(b'"x"', id="string"),
        pytest.param(b"1", id="number"),
        # an object stamped beyond the cap, which STORAGE.md#locking reads as corrupt
        pytest.param(
            b'{"owner": "ghost", "expires_at": "9999-12-31T23:59:59+00:00"}', id="far-future"
        ),
    ],
)
def test_s3_lock_corrupt_body_is_breakable(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch, corrupt: bytes
) -> None:
    """Corrupt lock JSON is breakable, so a waiter recovers by overwriting it."""
    monkeypatch.setattr(lock_mod, "LOCK_ACQUIRE_TIMEOUT_S", 20.0)
    storage._obj_put_reserved(LOCK_FILENAME, corrupt)
    with history_lock(storage):
        assert storage.exists(LOCK_FILENAME)
        body = json.loads(storage.get(LOCK_FILENAME))
        assert "owner" in body
        assert "expires_at" in body


def test_s3_lock_past_ttl_body_is_broken_by_new_holder(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid body whose `expires_at` has passed is taken over by a waiter.

    This is the TTL branch `TESTING.md` § Transaction history requires; the
    corrupt-body test never reaches the timestamp comparison.
    """
    monkeypatch.setattr(lock_mod, "LOCK_ACQUIRE_TIMEOUT_S", 20.0)
    stale = json.dumps(
        {"owner": "ghost", "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    ).encode("utf-8")
    storage._obj_put_reserved(LOCK_FILENAME, stale)

    with history_lock(storage):
        body = json.loads(storage.get(LOCK_FILENAME))
        assert body["owner"] != "ghost"
        ttl = datetime.fromisoformat(str(body["expires_at"])) - datetime.now(UTC)
        assert timedelta(hours=11, minutes=59) < ttl <= timedelta(hours=12)
    assert not storage.exists(LOCK_FILENAME)


def test_s3_lock_live_body_is_never_broken(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock still inside its TTL is waited for, then the acquire times out."""
    monkeypatch.setattr(lock_mod, "LOCK_ACQUIRE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(lock_mod, "LOCK_RETRY_SLEEP_S", 0.01)
    live = json.dumps(
        {"owner": "live", "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    ).encode("utf-8")
    storage._obj_put_reserved(LOCK_FILENAME, live)

    with pytest.raises(StorageException, match="Timed out acquiring history lock"):
        with history_lock(storage):
            pass  # pragma: no cover - the acquire must never succeed

    assert json.loads(storage.get(LOCK_FILENAME))["owner"] == "live"


@pytest.mark.parametrize("condition", ["IfNoneMatch", "IfMatch"])
def test_s3_lock_adopts_its_own_body_after_a_lost_write_response(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    """A committed create or take-over whose replay is refused is held, not waited on.

    botocore retries a write whose response was lost; the replay of a committed conditional
    write answers 412, and treating that as a peer's lock blocked every writer for the TTL.
    """
    monkeypatch.setattr(lock_mod, "LOCK_ACQUIRE_TIMEOUT_S", 2.0)
    monkeypatch.setattr(lock_mod, "LOCK_RETRY_SLEEP_S", 0.05)
    if condition == "IfMatch":
        stale = json.dumps(
            {"owner": "ghost", "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
        ).encode("utf-8")
        storage._obj_put_reserved(LOCK_FILENAME, stale)

    client = storage._s3_client._client
    real_put = client.put_object
    replayed = {"done": False}

    def commit_then_refuse_replay(**kwargs: Any) -> dict[str, object]:
        resp = real_put(**kwargs)  # passthrough of botocore kwargs
        if condition in kwargs and not replayed["done"]:
            replayed["done"] = True
            raise client_error("PreconditionFailed", 412, "PutObject")
        return dict(resp)

    with patch.object(client, "put_object", side_effect=commit_then_refuse_replay):
        with history_lock(storage):
            assert json.loads(storage.get(LOCK_FILENAME))["owner"] != "ghost"

    assert replayed["done"]
    assert not storage.exists(LOCK_FILENAME)


def test_s3_lock_takeover_is_atomic_against_peer_breaker(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A peer that acquires between our read and our write keeps its lock.

    Deterministic stand-in for two waiters breaking the same stale lock. The
    injected peer takes the lock over after we have read it, so our own
    take-over is left holding a stale ETag: it must be refused, not applied.
    Before the conditional take-over this path deleted the peer's live lock and
    granted us the section, leaving two holders.
    """
    monkeypatch.setattr(lock_mod, "LOCK_ACQUIRE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(lock_mod, "LOCK_RETRY_SLEEP_S", 0.01)
    stale = json.dumps(
        {"owner": "ghost", "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    ).encode("utf-8")
    storage._obj_put_reserved(LOCK_FILENAME, stale)

    real_read = lock_mod._read_state
    injected = {"done": False}

    def read_then_peer_takes_over(st: lock_mod._LockObject) -> object:
        state = real_read(st)
        if state is not None and not injected["done"]:
            injected["done"] = True
            assert lock_mod._try_take_over(st, "peer", state.etag) is not None
        return state  # deliberately stale ETag

    monkeypatch.setattr(lock_mod, "_read_state", read_then_peer_takes_over)

    with pytest.raises(StorageException, match="Timed out acquiring history lock"):
        with history_lock(storage):
            pass  # pragma: no cover - the peer holds the lock

    assert json.loads(storage.get(LOCK_FILENAME))["owner"] == "peer"


def test_s3_delete_lock_is_conditional(storage: StorageS3) -> None:
    """Release deletes only the holder's own version of the lock object."""
    _require_conditional_delete(storage)
    etag = _try_create(_lock(storage), owner="me")
    assert etag is not None

    # A peer replaced the body: our stale ETag must not delete their lock.
    storage._obj_put_reserved(LOCK_FILENAME, b'{"owner":"peer"}')
    assert _delete(_lock(storage), etag) is False
    assert storage.exists(LOCK_FILENAME)

    # Clear it the way the lock code does; `storage.delete` refuses reserved
    # paths → tests/test_reserved.py.
    storage._s3_client._client.delete_object(
        Bucket=storage.bucket, Key=storage._apply_prefix(LOCK_FILENAME)
    )
    # Already gone: benign, reported as "not ours to delete".
    assert _delete(_lock(storage), etag) is False

    # End to end: a body swapped inside the section survives our release.
    with history_lock(storage):
        storage._obj_put_reserved(LOCK_FILENAME, b'{"owner":"peer2"}')
    assert storage.exists(LOCK_FILENAME)


def test_s3_lock_concurrent_breakers_of_expired_lock(
    storage: StorageS3, moto_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two real threads breaking one stale lock must still serialise."""
    monkeypatch.setattr(lock_mod, "LOCK_ACQUIRE_TIMEOUT_S", 20.0)
    stale = json.dumps(
        {"owner": "ghost", "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    ).encode("utf-8")
    storage._obj_put_reserved(LOCK_FILENAME, stale)

    errors: list[BaseException] = []
    started = threading.Barrier(2)

    def worker(name: str) -> None:
        try:
            # Instances are not thread-safe -> one per thread (STORAGE.md footmarks).
            local = s3_storage(moto_server, storage.bucket)
            started.wait(timeout=10)
            with maybe_transaction(local):
                time.sleep(0.05)
                local.put(f"{name}.deb", name.encode())
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
    records = TransactionHistory(storage).load()
    assert {m.id for m in records} == {1, 2}
    assert not storage.exists(LOCK_FILENAME)


def test_s3_lock_conditional_conflict_is_retried_not_fatal(storage: StorageS3) -> None:
    """A 409 ConditionalRequestConflict means "retry", so acquire must not raise.

    S3 answers a conditional write racing another conditional write on the same
    key with 409 instead of 412; treating it as a hard error aborted the whole
    insert/sync instead of looping.
    """
    conflict = client_error("ConditionalRequestConflict", 409, "PutObject")
    assert _conditional_write_lost(conflict) is True

    client = storage._s3_client._client
    with patch.object(client, "put_object", side_effect=conflict):
        assert _try_create(_lock(storage), owner="racer") is None
    assert not storage.exists(LOCK_FILENAME)


def test_s3_lock_untaggable_create_does_not_orphan_the_lock(storage: StorageS3) -> None:
    """A write whose response carries no ETag must not leave a live lock behind."""
    real_put = storage._s3_client._client.put_object

    def put_without_etag(**kwargs: Any) -> dict[str, object]:
        real_put(**kwargs)  # passthrough of botocore kwargs
        return {}

    client = storage._s3_client._client
    with patch.object(client, "put_object", side_effect=put_without_etag):
        with pytest.raises(StorageException, match="carried no ETag"):
            _try_create(_lock(storage), owner="untaggable")

    # Without the cleanup the object would sit here for the whole TTL.
    assert not storage.exists(LOCK_FILENAME)


def test_s3_release_lock_retries_transient_delete_failure(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient delete error must be retried, not swallowed after one try.

    An undeleted lock blocks every peer until `LOCK_TTL` expires, so the holder
    is the only party that can still fix it.
    """
    monkeypatch.setattr(lock_mod, "LOCK_RELEASE_SLEEP_S", 0.0)

    etag = _try_create(_lock(storage), owner="me")
    assert etag is not None

    client = storage._s3_client._client
    real_delete = client.delete_object
    attempts: list[int] = []

    def flaky_delete(**kwargs: Any) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            raise client_error("SlowDown", 503)
        return real_delete(**kwargs)

    with patch.object(client, "delete_object", side_effect=flaky_delete):
        _release(_lock(storage), etag, owner="me")

    assert len(attempts) == 2
    assert not storage.exists(LOCK_FILENAME)


def test_s3_release_lock_gives_up_after_all_attempts(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Exhausted release attempts log the recovery hint instead of raising."""
    monkeypatch.setattr(lock_mod, "LOCK_RELEASE_SLEEP_S", 0.0)
    client = storage._s3_client._client

    with (
        patch.object(client, "delete_object", side_effect=client_error("SlowDown", 503)) as delete,
        caplog.at_level(logging.ERROR),
    ):
        _release(_lock(storage), '"deadbeef"', owner="me")

    assert delete.call_count == 3
    assert LOCK_FILENAME in caplog.text


def test_s3_lock_connection_error_is_mapped_to_storage_exception(storage: StorageS3) -> None:
    """A BotoCoreError on any lock write surfaces as StorageException.

    Connection failures are `BotoCoreError`, not `ClientError`; callers handle
    only `StorageException`, so an unwrapped one escaped every transactional
    write on S3 as a traceback.
    """
    client = storage._s3_client._client
    down = EndpointConnectionError(endpoint_url=storage.endpoint)

    with patch.object(client, "put_object", side_effect=down):
        with pytest.raises(StorageException, match="Failed to acquire history lock"):
            _try_create(_lock(storage), owner="me")
        with pytest.raises(StorageException, match="Failed to take over history lock"):
            _try_take_over(_lock(storage), owner="me", etag='"deadbeef"')

    with (
        patch.object(client, "delete_object", side_effect=down),
        pytest.raises(StorageException, match="Failed to release history lock"),
    ):
        _delete(_lock(storage), '"deadbeef"')


def _require_conditional_delete(storage: StorageS3) -> None:
    """Skip unless the server refuses ``DeleteObject`` with a stale ``If-Match``.

    The lock requires that endpoint feature (STORAGE.md#locking); older moto releases, such as
    some distributions package, accept the delete regardless.
    """
    client = storage._s3_client._client
    client.put_object(Bucket=storage.bucket, Key="probe", Body=b"x")
    try:
        client.delete_object(Bucket=storage.bucket, Key="probe", IfMatch='"0"')
    except ClientError:
        client.delete_object(Bucket=storage.bucket, Key="probe")
        return
    backend_unavailable("S3 server does not enforce If-Match on DeleteObject")


# --------------------------------------------------------------------------- #
# Expiry and error paths
# --------------------------------------------------------------------------- #


def _stamp(delta: timedelta, *, aware: bool) -> str:
    when = datetime.now(UTC) + delta
    return (when if aware else when.replace(tzinfo=None)).isoformat()


@pytest.mark.parametrize(
    ("body", "breakable"),
    [
        ({}, True),
        ({"expires_at": 123}, True),
        ({"expires_at": "yesterday"}, True),
        ({"expires_at": _stamp(timedelta(hours=-1), aware=False)}, True),
        ({"expires_at": _stamp(timedelta(hours=1), aware=False)}, False),
        ({"expires_at": _stamp(timedelta(hours=1), aware=True)}, False),
        ({"expires_at": _stamp(lock_mod.LOCK_MAX_AHEAD - timedelta(hours=1), aware=True)}, False),
        ({"expires_at": _stamp(lock_mod.LOCK_MAX_AHEAD + timedelta(hours=1), aware=True)}, True),
        ({"expires_at": "9999-12-31T23:59:59+00:00"}, True),
    ],
    ids=[
        "missing",
        "non-string",
        "unparseable",
        "naive-past",
        "naive-future",
        "aware-future",
        "within-cap",
        "beyond-cap",
        "far-future",
    ],
)
def test_s3_lock_body_expiry_decides_breakability(body: dict[str, object], breakable: bool) -> None:
    """A corrupt or over-capped `expires_at` is breakable; a naive one is read as UTC.

    See STORAGE.md#locking.
    """
    assert _body_is_breakable(body) is breakable


def test_s3_lock_stamp_cap_is_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stamp exactly `LOCK_MAX_AHEAD` ahead is live; one microsecond more is corrupt."""
    now = datetime(2026, 1, 1, tzinfo=UTC)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            del tz
            return cls.fromisoformat(now.isoformat())

    monkeypatch.setattr(lock_mod, "datetime", _Frozen)
    at_cap = now + lock_mod.LOCK_MAX_AHEAD
    assert _body_is_breakable({"expires_at": at_cap.isoformat()}) is False
    over_cap = at_cap + timedelta(microseconds=1)
    assert _body_is_breakable({"expires_at": over_cap.isoformat()}) is True


def test_s3_lock_read_failure_warns_once_per_acquire(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An endpoint failing every read must not log one warning per 0.25 s retry."""
    monkeypatch.setattr(lock_mod, "LOCK_ACQUIRE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(lock_mod, "LOCK_RETRY_SLEEP_S", 0.01)
    assert _try_create(_lock(storage), owner="peer") is not None
    client = storage._s3_client._client

    with (
        patch.object(client, "get_object", side_effect=client_error("SlowDown", 503)) as get,
        caplog.at_level(logging.WARNING, logger="txstore._s3_lock"),
        pytest.raises(StorageException, match="Timed out acquiring") as raised,
    ):
        with history_lock(storage):
            pass

    assert get.call_count > 1
    assert sum("Cannot read S3 history lock" in r.getMessage() for r in caplog.records) == 1
    # Without logging configured, the timeout alone still names the read failure.
    assert "last lock read failed" in str(raised.value)
    assert "SlowDown" in str(raised.value)
    assert isinstance(raised.value.__cause__, StorageException)


def test_s3_lock_read_and_steal_of_an_absent_lock_return_none(storage: StorageS3) -> None:
    assert _read_state(_lock(storage)) is None
    assert _try_steal_breakable(_lock(storage), owner="me", read_failures=_ReadFailureLog()) is None


def test_s3_lock_read_without_an_etag_raises(storage: StorageS3) -> None:
    """Without a version tag the lock can neither be taken over nor released safely."""
    with (
        patch.object(storage._s3_client, "object_get_with_etag", return_value=(b"{}", "")),
        pytest.raises(StorageException, match="carried no ETag"),
    ):
        _read_state(_lock(storage))


def test_s3_lock_read_connection_error_is_mapped(storage: StorageS3) -> None:
    client = storage._s3_client._client
    down = EndpointConnectionError(endpoint_url=storage.endpoint)
    with (
        patch.object(client, "get_object", side_effect=down),
        pytest.raises(StorageException, match="Failed to read history lock"),
    ):
        _read_state(_lock(storage))


def test_s3_lock_refused_writes_other_than_a_lost_race_abort(storage: StorageS3) -> None:
    """403 is not "retry": create, take-over, and release raise instead of waiting."""
    client = storage._s3_client._client
    denied = client_error("AccessDenied", 403)

    with patch.object(client, "put_object", side_effect=denied):
        with pytest.raises(StorageException, match="Failed to acquire history lock"):
            _try_create(_lock(storage), owner="me")
        with pytest.raises(StorageException, match="Failed to take over history lock"):
            _try_take_over(_lock(storage), owner="me", etag='"deadbeef"')
    with (
        patch.object(client, "delete_object", side_effect=denied),
        pytest.raises(StorageException, match="Failed to release history lock"),
    ):
        _delete(_lock(storage), '"deadbeef"')


def test_s3_lock_refused_read_aborts_the_acquire(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 403 on the lock read cannot clear within the timeout, so it aborts like a refused write."""
    monkeypatch.setattr(lock_mod, "LOCK_ACQUIRE_TIMEOUT_S", 20.0)
    assert _try_create(_lock(storage), owner="peer") is not None
    client = storage._s3_client._client

    with (
        patch.object(client, "get_object", side_effect=client_error("AccessDenied", 403)) as get,
        pytest.raises(StorageException, match="Failed to read history lock") as raised,
    ):
        with history_lock(storage):
            pass

    assert get.call_count == 1
    assert "Timed out" not in str(raised.value)


def test_s3_lock_untaggable_create_raises_even_when_the_drop_fails(
    storage: StorageS3, caplog: pytest.LogCaptureFixture
) -> None:
    client = storage._s3_client._client
    with (
        patch.object(client, "put_object", return_value={}),
        patch.object(client, "delete_object", side_effect=client_error("SlowDown", 503)),
        caplog.at_level(logging.ERROR, logger="txstore._s3_lock"),
        pytest.raises(StorageException, match="carried no ETag"),
    ):
        _try_create(_lock(storage), owner="untaggable")
    assert "Failed to drop un-taggable" in caplog.text
