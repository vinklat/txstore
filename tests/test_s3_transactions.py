"""`StorageS3`-specific transaction behavior: ACL carry-over and the batched reclaim.

The lifecycle every backend shares lives in `test_transactions.py`. moto keeps a source's ACL
on a server-side copy where real S3 resets it, so the ACL tests also spy on the ACL each copy
is made with.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from helpers.s3 import client_error, is_public, s3_bucket, s3_storage
from helpers.storage import (
    abandon_transaction,
    commit_transaction,
    footmarks_of,
    iso_timestamp,
    seed_history,
    seed_old_and_new,
    transaction_record,
)

from txstore import (
    HistoryRetention,
    ObjectCannedACL,
    StorageException,
    StorageNotFoundException,
    StorageS3,
    Timestamp,
    TransactionHistory,
    maybe_transaction,
)


@pytest.fixture
def storage(moto_server: str) -> Iterator[StorageS3]:
    with s3_bucket(moto_server, name="test-txn") as s:
        yield s


def _raw_keys(s: StorageS3) -> set[str]:
    """Every key in the bucket (including footmarks), prefix stripped."""
    return set(s._obj_iter_rel_all())


def _hidden_keys(s: StorageS3) -> list[str]:
    return sorted(k for k in _raw_keys(s) if k.rsplit("/", 1)[-1].startswith("."))


def _copy_acls(storage: StorageS3) -> Any:
    """Spy on `S3Client.object_copy`; each call's ``acl`` is the ACL the copy was made with."""
    return patch.object(
        storage._s3_client, "object_copy", side_effect=storage._s3_client.object_copy
    )


# --------------------------------------------------------------------------- #
# ACL carry-over
# --------------------------------------------------------------------------- #


def test_commit_copies_the_put_footmark_with_the_requested_acl(storage: StorageS3) -> None:
    """A non-group canned ACL cannot be read back from grants, so the sidecar carries it."""
    storage.transaction_begin()
    storage.put("a.txt", b"x", acl="bucket-owner-full-control")

    with _copy_acls(storage) as spy:
        storage.transaction_commit()

    assert [c.kwargs["acl"] for c in spy.call_args_list] == ["bucket-owner-full-control"]


def test_copy_in_a_transaction_uses_the_acl_recorded_for_the_staged_source(
    storage: StorageS3,
) -> None:
    storage.transaction_begin()
    storage.put("b.txt", b"x", acl="authenticated-read")

    with _copy_acls(storage) as spy:
        storage.copy("b.txt", "c.txt")

    assert [c.kwargs["acl"] for c in spy.call_args_list] == ["authenticated-read"]


def test_copy_outside_a_transaction_derives_the_acl_from_the_source_grants(
    storage: StorageS3,
) -> None:
    storage.put("src.txt", b"data", acl="public-read")

    with _copy_acls(storage) as spy:
        storage.copy("src.txt", "dst.txt")

    assert [c.kwargs["acl"] for c in spy.call_args_list] == [None]
    assert is_public(storage, "dst.txt")


@pytest.mark.parametrize("rewrite", ["put", "copy"])
def test_rewrite_without_acl_drops_the_recorded_acl(storage: StorageS3, rewrite: str) -> None:
    """A stale sidecar entry would widen the rewritten object to public at commit."""
    storage.put("private.txt", b"p")
    storage.transaction_begin()
    storage.put("a.txt", b"x", acl="public-read")
    if rewrite == "put":
        storage.put("a.txt", b"y")
    else:
        storage.copy("private.txt", "a.txt")
    storage.transaction_commit()
    assert not is_public(storage, "a.txt")


def test_commit_put_preserves_public_read_acl(storage: StorageS3) -> None:
    storage.transaction_begin()
    storage.put("a.txt", b"x", acl="public-read")
    storage.transaction_commit()
    assert is_public(storage, "a.txt")


def test_commit_put_stream_preserves_public_read_acl(storage: StorageS3) -> None:
    storage.transaction_begin()
    storage.put_stream("s.txt", io.BytesIO(b"x"), acl="public-read")
    storage.transaction_commit()
    assert is_public(storage, "s.txt")


def test_revert_restores_public_read_acl_after_modify(storage: StorageS3) -> None:
    storage.put("a.txt", b"orig", acl="public-read")
    txn = storage.transaction_begin()
    storage.put("a.txt", b"changed", acl="public-read")
    storage.transaction_commit()
    assert is_public(storage, "a.txt")

    storage.transaction_revert(txn)
    assert storage.get("a.txt") == b"orig"
    assert is_public(storage, "a.txt")


def test_revert_restores_public_read_acl_after_delete(storage: StorageS3) -> None:
    storage.put("a.txt", b"orig", acl="public-read")
    txn = storage.transaction_begin()
    storage.delete("a.txt")
    storage.transaction_commit()
    assert not storage.exists("a.txt")

    storage.transaction_revert(txn)
    assert storage.exists("a.txt")
    assert is_public(storage, "a.txt")


def test_add_metadata_records_an_explicit_acl_for_commit(storage: StorageS3) -> None:
    """A non-group ACL only the sidecar carries reaches the commit copy of the staged rewrite."""
    storage.put("a.txt", b"x")

    storage.transaction_begin()
    storage.add_metadata("a.txt", {"k": "v"}, acl="bucket-owner-full-control")

    with _copy_acls(storage) as spy:
        storage.transaction_commit()

    assert [c.kwargs["acl"] for c in spy.call_args_list] == ["bucket-owner-full-control"]


def test_add_metadata_preserves_source_acl_without_explicit_acl(
    storage: StorageS3,
) -> None:
    """Even without re-passing acl, the source's public-read survives commit."""
    storage.put("a.txt", b"x", acl="public-read")

    storage.transaction_begin()
    storage.add_metadata("a.txt", {"k": "v"})
    storage.transaction_commit()

    assert is_public(storage, "a.txt")


def test_add_metadata_keeps_the_acl_a_staged_put_recorded(storage: StorageS3) -> None:
    """A non-group ACL only the sidecar carries survives a later rewrite without `acl`."""
    storage.transaction_begin()
    storage.put("a.txt", b"x", acl="bucket-owner-full-control")
    storage.add_metadata("a.txt", {"k": "v"})

    with _copy_acls(storage) as spy:
        storage.transaction_commit()

    assert [c.kwargs["acl"] for c in spy.call_args_list] == ["bucket-owner-full-control"]


@pytest.mark.parametrize("default", [None, "private"], ids=["no-default", "private-default"])
def test_add_metadata_outside_a_transaction_keeps_a_public_acl(
    moto_server: str, default: ObjectCannedACL | None
) -> None:
    """Outside a transaction no commit copy re-applies the ACL, so the rewrite must keep it."""
    with s3_bucket(moto_server, default_acl=default) as storage:
        storage.put("a.txt", b"x", acl="public-read")
        storage.add_metadata("a.txt", {"k": "v"})

        assert is_public(storage, "a.txt")


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "staged"])
def test_add_metadata_keeps_a_private_acl_under_a_public_default(
    moto_server: str, in_transaction: bool
) -> None:
    """A metadata change is no content write: the default ACL never widens the object."""
    with s3_bucket(moto_server, default_acl="public-read") as storage:
        storage.put("a.txt", b"x", acl="private")
        if in_transaction:
            storage.transaction_begin()
        storage.add_metadata("a.txt", {"k": "v"})
        if in_transaction:
            storage.transaction_commit()

        assert not is_public(storage, "a.txt")
        assert storage.get_metadata("a.txt")["k"] == "v"


def test_a_failed_add_metadata_records_no_acl(storage: StorageS3) -> None:
    """The ACL of a rewrite that never landed must not reach the object at commit."""
    storage.put("a.txt", b"x")
    storage.transaction_begin()
    with (
        patch.object(
            storage._s3_client, "object_put_metadata", side_effect=client_error("InternalError")
        ),
        pytest.raises(StorageException, match="Error writing metadata a.txt"),
    ):
        storage.add_metadata("a.txt", {"k": "v"}, acl="public-read")
    storage.transaction_commit()

    assert not is_public(storage, "a.txt")


def test_transaction_forget_batches_the_s3_deletes(storage: StorageS3) -> None:
    """Every footmark of a reclaim goes out in one `DeleteObjects` call, not one delete per key."""
    first = commit_transaction(storage, "a.deb")
    second = commit_transaction(storage, "b.deb")
    client = storage._s3_client._client

    with (
        patch.object(client, "delete_objects", side_effect=client.delete_objects) as many,
        patch.object(client, "delete_object", side_effect=client.delete_object) as one,
    ):
        removed = storage.transaction_forget([first, second])

    assert many.call_count == 1
    assert one.call_count == 0
    assert len(removed) == 2
    hidden = _hidden_keys(storage)
    for model in (first, second):
        assert not footmarks_of(hidden, model)


def test_transaction_discard_batches_the_s3_deletes(storage: StorageS3) -> None:
    """A discard is reclamation of its own footmarks: one `DeleteObjects`, no per-key delete."""
    storage.put("old.deb", b"old")
    storage.transaction_begin()
    for name in ("old.deb", "a.deb", "b.deb"):
        storage.put(name, b"staged")
    client = storage._s3_client._client
    s3 = storage._s3_client

    with (
        patch.object(client, "delete_objects", side_effect=client.delete_objects) as many,
        patch.object(s3, "object_delete", side_effect=s3.object_delete) as one,
    ):
        storage.transaction_discard()

    assert many.call_count == 1
    assert one.call_count == 0
    assert _hidden_keys(storage) == []
    assert storage.get("old.deb") == b"old"


def test_reclaim_delete_many_chunks_at_the_s3_limit(storage: StorageS3) -> None:
    """1001 keys need two requests; a missing key deletes as a success, as on S3."""
    rels = [f".x.{i}" for i in range(1001)]
    client = storage._s3_client._client

    with patch.object(client, "delete_objects", side_effect=client.delete_objects) as many:
        removed, failed = storage._obj_reclaim_delete_many(rels)

    assert [len(c.kwargs["Delete"]["Objects"]) for c in many.call_args_list] == [1000, 1]
    # moto ignores `Quiet`; on S3 a quiet response lists no `Deleted` key at all.
    assert all(c.kwargs["Delete"]["Quiet"] is False for c in many.call_args_list)
    assert sorted(removed) == sorted(rels)
    assert failed == []


def test_reclaim_delete_many_reports_a_key_the_response_omits_as_failed(
    storage: StorageS3,
) -> None:
    """A key in neither `Deleted` nor `Errors` is not assumed gone → STORAGE.md."""
    client = storage._s3_client._client
    with patch.object(client, "delete_objects", return_value={"Deleted": [], "Errors": []}):
        removed, failed = storage._obj_reclaim_delete_many([".x.1"])

    assert removed == []
    assert failed == [(".x.1", "missing from DeleteObjects response")]


def _check_refusing_and_losing(storage: StorageS3) -> AbstractContextManager[Any]:
    """Patch the footmark check to refuse `.x.refused` and report `.x.gone` as vanished."""
    checked = storage._obj_checked_footmark

    def check(rel: str) -> str:
        if rel == ".x.refused":
            raise StorageException("refused")
        if rel == ".x.gone":
            raise StorageNotFoundException("gone")
        return checked(rel)

    return patch.object(storage, "_obj_checked_footmark", side_effect=check)


@pytest.mark.parametrize("batch", ["deletes", "fails"])
def test_reclaim_delete_many_resolves_every_key_through_the_footmark_check(
    storage: StorageS3, batch: str
) -> None:
    """The batch, and its per-key fallback, delete what `_reclaim_footmark` would, once each."""
    storage.put(".x.refused", b"keep")
    storage.put(".x.ok", b"drop")
    client = storage._s3_client._client
    outcome = ConnectionResetError("reset") if batch == "fails" else client.delete_objects

    with (
        _check_refusing_and_losing(storage),
        patch.object(client, "delete_objects", side_effect=outcome),
    ):
        removed, failed = storage._obj_reclaim_delete_many([".x.refused", ".x.gone", ".x.ok"])

    assert sorted(removed) == [".x.gone", ".x.ok"]
    assert failed == [(".x.refused", "refused")]
    assert storage.get(".x.refused") == b"keep"


def test_reclaim_delete_many_sends_no_request_for_a_chunk_with_no_live_key(
    storage: StorageS3,
) -> None:
    """S3 rejects an empty ``DeleteObjects``, so a chunk the check emptied is skipped."""
    client = storage._s3_client._client
    with (
        _check_refusing_and_losing(storage),
        patch.object(client, "delete_objects", side_effect=client.delete_objects) as many,
    ):
        removed, failed = storage._obj_reclaim_delete_many([".x.gone"])

    assert many.call_count == 0
    assert (removed, failed) == ([".x.gone"], [])


def test_reclaim_delete_many_ignores_a_key_outside_the_chunk(storage: StorageS3) -> None:
    """A response entry for a key the chunk never sent is neither removed nor failed."""
    client = storage._s3_client._client
    response = {
        "Deleted": [{"Key": ".x.1"}, {"Key": ".stray"}],
        "Errors": [{"Key": ".other", "Code": "AccessDenied", "Message": "denied"}],
    }
    with patch.object(client, "delete_objects", return_value=response):
        removed, failed = storage._obj_reclaim_delete_many([".x.1"])

    assert removed == [".x.1"]
    assert failed == []


def test_prune_leaves_a_key_the_batch_rejects_for_the_orphan_sweep(
    storage: StorageS3, caplog: pytest.LogCaptureFixture
) -> None:
    """A per-key `Errors` entry is a failure: logged, kept out of the result, swept later."""
    old = storage.transaction_begin()
    storage.put("old.deb", b"payload")
    storage.put("old2.deb", b"payload")
    storage.transaction_commit()
    new = commit_transaction(storage, "new.deb")
    seed_history(storage, [transaction_record(old), transaction_record(new)])
    doomed = footmarks_of(_hidden_keys(storage), old)
    stubborn = doomed[0]
    client = storage._s3_client._client
    real = client.delete_objects

    def inject(**kwargs: Any) -> Any:
        delete = kwargs["Delete"]
        assert isinstance(delete, dict)
        objects = [o for o in delete["Objects"] if o["Key"] != stubborn]
        resp = real(Bucket=kwargs["Bucket"], Delete={"Objects": objects, "Quiet": False})
        resp.setdefault("Errors", []).append(
            {"Key": stubborn, "Code": "AccessDenied", "Message": "denied"}
        )
        return resp

    history = TransactionHistory(storage)
    later = datetime.now(UTC) + timedelta(days=400)
    with (
        caplog.at_level(logging.WARNING),
        patch.object(client, "delete_objects", side_effect=inject),
    ):
        result = history.prune(HistoryRetention(keep=1, max_age_days=90), now=later)

    assert stubborn in _raw_keys(storage)
    assert result.footmark_paths == doomed[1:]
    assert "Failed to remove footmark" in caplog.text
    assert "resisted removal" in caplog.text
    assert [m.id for m in history.load()] == [new.id]
    preview = history.prune(HistoryRetention(keep=1, max_age_days=90), orphans=True, dry_run=True)
    assert preview.orphans == [stubborn]


@pytest.mark.parametrize(
    "boom",
    [
        client_error("InternalError", op="DeleteObjects"),
        EndpointConnectionError(endpoint_url="http://s3"),
        ConnectionResetError("reset"),
    ],
    ids=["client-error", "botocore-error", "os-error"],
)
def test_reclaim_falls_back_to_single_deletes_when_the_batch_call_fails(
    storage: StorageS3, boom: Exception
) -> None:
    model = storage.transaction_begin()
    storage.put("a.deb", b"payload")
    storage.put("b.deb", b"payload")
    storage.transaction_commit()
    client = storage._s3_client._client

    with (
        patch.object(client, "delete_objects", side_effect=boom),
        patch.object(client, "delete_object", side_effect=client.delete_object) as one,
    ):
        removed = storage.transaction_forget([model])

    assert len(removed) == 2
    assert one.call_count == 2
    assert not footmarks_of(_hidden_keys(storage), model)


def test_reclaim_delete_many_strips_the_prefix(moto_server: str) -> None:
    """Returned paths are relative, as the caller passed them, not prefixed keys."""
    with s3_bucket(moto_server, name="test-txn", prefix="repo") as prefixed:
        model = commit_transaction(prefixed, "a.deb")
        removed = prefixed.transaction_forget([model])

        assert removed
        assert not [rel for rel in removed if rel.startswith("repo/")]
        assert not footmarks_of(prefixed._obj_iter_rel_all(), model)


def test_prune_rewrites_history_and_reclaims_footmarks_on_s3(storage: StorageS3) -> None:
    old, new = seed_old_and_new(storage)
    history = TransactionHistory(storage)
    later = datetime.now(UTC) + timedelta(days=400)

    result = history.prune(HistoryRetention(keep=1, max_age_days=90), now=later)

    assert [m.id for m in result.records] == [old.id]
    assert result.orphans == []
    hidden = _hidden_keys(storage)
    assert footmarks_of(hidden, old) == []
    assert footmarks_of(hidden, new)
    assert [m.id for m in history.load()] == [new.id]
    # The S3 lock is released by deleting its object.
    assert ".transactions.lock" not in _raw_keys(storage)
    # Pruning is not a revert: the committed object stays.
    assert storage.get("old.deb") == b"old"


def test_maybe_transaction_prunes_under_the_lock_on_s3(storage: StorageS3) -> None:
    """Auto-prune reuses the commit's listing and holds the S3 lock it took for the commit."""
    # A frozen begin timestamp ages the record while its tokens still regenerate.
    with patch(
        "txstore.transaction.now_timestamp", return_value=Timestamp.from_json(iso_timestamp(400))
    ):
        with maybe_transaction(storage):
            storage.put("old.deb", b"old")
    old = TransactionHistory(storage).load()[0]
    assert footmarks_of(_hidden_keys(storage), old)
    lock_held: list[bool] = []
    real_forget = storage.transaction_forget

    def forget(*args: Any, **kwargs: Any) -> list[str]:
        lock_held.append(".transactions.lock" in _raw_keys(storage))
        return real_forget(*args, **kwargs)

    with patch.object(storage, "transaction_forget", side_effect=forget):
        with maybe_transaction(storage, HistoryRetention(keep=1, max_age_days=90)):
            storage.put("new.deb", b"new")

    assert lock_held == [True]
    assert [m.id for m in TransactionHistory(storage).load()] == [old.id + 1]
    assert footmarks_of(_hidden_keys(storage), old) == []
    assert ".transactions.lock" not in _raw_keys(storage)


def test_a_root_locked_by_this_thread_is_refused_at_once_on_s3(
    storage: StorageS3, moto_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested acquire through a second instance fails at once, not after the acquire timeout.

    Each acquire writes a fresh owner id, so the lock object alone never recognises its holder.
    """
    monkeypatch.setattr("txstore._s3_lock.LOCK_ACQUIRE_TIMEOUT_S", 1.0)
    nested = s3_storage(moto_server, storage.bucket)

    with maybe_transaction(storage):
        storage.put("a.deb", b"a")
        with pytest.raises(StorageException, match="this thread already holds"):
            with maybe_transaction(nested):
                pass

    assert [m.id for m in TransactionHistory(storage).load()] == [1]
    assert ".transactions.lock" not in _raw_keys(storage)


def test_prune_with_orphans_sweeps_residue_on_s3(storage: StorageS3) -> None:
    recorded = commit_transaction(storage, "kept.deb")
    seed_history(storage, [transaction_record(recorded)])
    crashed = storage.transaction_begin()
    storage.put("ghost.deb", b"lost")
    abandon_transaction(storage)

    result = TransactionHistory(storage).prune(
        HistoryRetention(keep=1, max_age_days=90), orphans=True
    )

    assert result.records == []
    assert result.orphans
    hidden = _hidden_keys(storage)
    assert footmarks_of(hidden, crashed) == []
    assert footmarks_of(hidden, recorded)


# --------------------------------------------------------------------------- #
# Real client errors through the overlay
# --------------------------------------------------------------------------- #


def test_commit_maps_a_client_error_and_stays_retryable(storage: StorageS3) -> None:
    """A real botocore failure, not only an injected primitive one, crosses the overlay."""
    storage.transaction_begin()
    storage.put("a.txt", b"v")
    client = storage._s3_client._client

    with (
        patch.object(client, "copy", side_effect=client_error("AccessDenied", 403)),
        pytest.raises(StorageException, match="Failed to commit PUT") as raised,
    ):
        storage.transaction_commit()

    assert type(raised.value) is StorageException
    assert isinstance(raised.value.__cause__, ClientError)
    assert storage.in_transaction()
    assert storage.transaction_commit() is True
    assert not storage.in_transaction()
    assert storage.get("a.txt") == b"v"


def _commit_delete(s: StorageS3) -> Callable[[], object]:
    s.put("a.txt", b"v")
    s.transaction_begin()
    s.delete("a.txt")
    return s.transaction_commit


def _revert_restore(s: StorageS3) -> Callable[[], object]:
    s.put("a.txt", b"orig")
    model = commit_transaction(s, "a.txt", b"new")
    return lambda: s.transaction_revert(model)


def _revert_created(s: StorageS3) -> Callable[[], object]:
    model = commit_transaction(s, "new.txt")
    return lambda: s.transaction_revert(model)


def _discard(s: StorageS3) -> Callable[[], object]:
    s.transaction_begin()
    s.put("a.txt", b"v")
    return s.transaction_discard


def _stage_backup(s: StorageS3) -> Callable[[], object]:
    s.put("a.txt", b"orig")
    s.transaction_begin()
    return lambda: s.put("a.txt", b"new")


def _committed_delete(s: StorageS3, result: object) -> bool:
    return result is True and not s.in_transaction() and not s.exists("a.txt")


def _restored(s: StorageS3, _result: object) -> bool:
    return s.get("a.txt") == b"orig" and s.hidden_objects() == []


def _created_removed(s: StorageS3, _result: object) -> bool:
    return not s.exists("new.txt") and s.hidden_objects() == []


def _discarded(s: StorageS3, _result: object) -> bool:
    return not s.in_transaction() and s.hidden_objects() == [] and not s.exists("a.txt")


def _staged(s: StorageS3, _result: object) -> bool:
    return s.in_transaction() and s.get("a.txt") == b"new"


@pytest.mark.parametrize(
    ("arrange", "calls", "message", "was_active", "settled"),
    [
        pytest.param(
            _commit_delete,
            ["delete_object"],
            "Failed to commit DELETE",
            True,
            _committed_delete,
            id="commit-delete",
        ),
        pytest.param(
            _revert_restore, ["copy"], "Failed to restore", False, _restored, id="revert-restore"
        ),
        pytest.param(
            _revert_created,
            ["delete_object"],
            "Failed to remove newly created",
            False,
            _created_removed,
            id="revert-created",
        ),
        pytest.param(
            _discard,
            ["delete_objects", "delete_object"],
            "Failed to remove footmark",
            True,
            _discarded,
            id="discard",
        ),
        pytest.param(
            _stage_backup, ["copy"], "Failed to create backup", True, _staged, id="stage-backup"
        ),
    ],
)
def test_client_error_on_a_transaction_path_is_chained_and_retryable(
    storage: StorageS3,
    arrange: Callable[[StorageS3], Callable[[], object]],
    calls: list[str],
    message: str,
    was_active: bool,
    settled: Callable[[StorageS3, object], bool],
) -> None:
    """The S3 primitive runs on each path, so a real botocore error crosses `_wrap` unchanged."""
    operation = arrange(storage)
    client = storage._s3_client._client
    boom = client_error("SlowDown", 503)

    with ExitStack() as stack:
        for call in calls:
            stack.enter_context(patch.object(client, call, side_effect=boom))
        with pytest.raises(StorageException, match=message) as raised:
            operation()

    assert type(raised.value) is StorageException
    assert raised.value.__cause__ is boom
    if was_active:
        assert storage.in_transaction()
    # The retry must finish the job, not merely stop raising.
    assert settled(storage, operation())


def test_direct_delete_maps_a_client_error(storage: StorageS3) -> None:
    storage.put("a.txt", b"v")
    client = storage._s3_client._client

    with (
        patch.object(client, "delete_object", side_effect=client_error("AccessDenied", 403)),
        pytest.raises(StorageException, match="Failed to delete a.txt") as raised,
    ):
        storage.delete("a.txt")

    assert isinstance(raised.value.__cause__, ClientError)
    assert storage.exists("a.txt")
