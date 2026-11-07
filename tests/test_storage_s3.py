"""Contract test for `StorageS3` against an in-process moto S3 server.

Required cases → `TESTING.md#required-coverage`.
"""

from __future__ import annotations

import io
import json
import logging
import traceback
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from helpers.s3 import client_error, is_public, s3_bucket, s3_storage
from helpers.storage import HISTORY_FILENAME, LOCK_FILENAME, FailingStream, Pipe
from urllib3.exceptions import ProtocolError

from txstore import (
    ObjectOperation,
    StorageException,
    StorageNotFoundException,
    StorageS3,
    StorageType,
    Timestamp,
    TransactionHistory,
    TransactionModel,
    maybe_transaction,
    parse_bucket_acl,
    parse_object_acl,
)


@pytest.fixture
def storage(moto_server: str) -> Iterator[StorageS3]:
    with s3_bucket(moto_server, bucket_acl="public-read") as s:
        yield s


def _put(s: StorageS3, path: str, data: bytes) -> None:
    """Convenience wrapper: put a regular object on a verify-public-acl=False fixture."""
    s.put(path, data)


# --------------------------------------------------------------------------- #
# Init / type
# --------------------------------------------------------------------------- #


def test_storage_s3_get_type(storage: StorageS3) -> None:
    assert storage.get_type() is StorageType.s3


def test_storage_s3_repr_contains_endpoint(storage: StorageS3) -> None:
    r = repr(storage)
    assert storage.endpoint in r
    assert storage.bucket in r


def test_storage_s3_drops_endpoint_userinfo_on_direct_construction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="txstore.storage_s3"):
        storage = StorageS3(
            "https://u:hunter2@s3.example.invalid", "b", "ak", "sk", verify_public_acl=False
        )
    assert storage.endpoint == "https://s3.example.invalid"
    assert "s3.example.invalid" in repr(storage)
    assert "hunter2" not in repr(storage) + caplog.text


@pytest.mark.parametrize(
    "endpoint",
    ["https://u:p@[hunter2]/x", "https://u:hunter2\uff20x@s3.example.invalid"],
    ids=["bracketed-host", "nfkc"],
)
def test_storage_s3_refuses_an_endpoint_that_does_not_parse_without_naming_it(
    endpoint: str,
) -> None:
    with pytest.raises(ValueError, match="authority does not parse") as raised:
        StorageS3(endpoint, "b", "ak", "sk", verify_public_acl=False)
    assert "hunter2" not in "".join(traceback.format_exception_only(raised.value))
    assert raised.value.__context__ is None


@pytest.mark.parametrize("endpoint", ["https://", "https://u:hunter2@:9000"], ids=["empty", "port"])
def test_storage_s3_refuses_an_endpoint_without_host(endpoint: str) -> None:
    """Refused before botocore, whose `ValueError` would name the endpoint → STORAGE.md."""
    with pytest.raises(StorageException, match="Invalid S3 endpoint") as raised:
        StorageS3(endpoint, "b", "ak", "sk", verify_public_acl=False)
    assert "hunter2" not in str(raised.value)


@pytest.mark.parametrize(
    "endpoint", ["https://s3.example.invalid/?tok=hunter2", "https://s3.example.invalid#hunter2"]
)
def test_storage_s3_refuses_an_endpoint_with_a_query_or_fragment(endpoint: str) -> None:
    """botocore would echo the endpoint, query included, in every connection error."""
    with pytest.raises(ValueError, match="query or fragment") as raised:
        StorageS3(endpoint, "b", "ak", "sk", verify_public_acl=False)
    assert "hunter2" not in str(raised.value)


@pytest.mark.parametrize("verify", [True, False])
def test_storage_s3_passes_verify_to_boto3(verify: bool) -> None:
    """The fixtures run with verification off, so only this pins both hops to boto3."""
    with patch("txstore._s3_client.boto3.resource") as resource:
        StorageS3("https://s3.example.invalid", "b", "ak", "sk", verify=verify)
    assert resource.call_args.kwargs["verify"] is verify


# --------------------------------------------------------------------------- #
# put / get / exists
# --------------------------------------------------------------------------- #


def test_storage_s3_put_then_get_roundtrip(storage: StorageS3) -> None:
    _put(storage, "foo.txt", b"hello")
    assert storage.get("foo.txt") == b"hello"
    assert storage.exists("foo.txt")


def test_storage_s3_exists_false_for_missing(storage: StorageS3) -> None:
    assert not storage.exists("missing.txt")


def test_storage_s3_get_missing_raises(storage: StorageS3) -> None:
    with pytest.raises(StorageNotFoundException, match="not found"):
        storage.get("missing.txt")
    assert storage.metrics.read_errors_total == 1


def test_storage_s3_get_stream_missing_raises_not_found(storage: StorageS3) -> None:
    with pytest.raises(StorageNotFoundException, match="not found"):
        storage.get_stream("missing.txt")
    assert storage.metrics.read_errors_total == 1


def test_storage_s3_metrics_on_put_and_get(storage: StorageS3) -> None:
    _put(storage, "a.bin", b"abcd")
    storage.get("a.bin")
    assert storage.metrics.write_objects_total == 1
    assert storage.metrics.write_bytes == 4
    assert storage.metrics.read_objects_total == 1
    assert storage.metrics.read_bytes == 4


# --------------------------------------------------------------------------- #
# put_stream / get_stream
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seekable", [True, False], ids=["seekable", "pipe"])
def test_storage_s3_put_stream_counts_the_written_bytes(storage: StorageS3, seekable: bool) -> None:
    body = b"x" * 5000
    stream = io.BytesIO(body) if seekable else Pipe(body)

    storage.put_stream("s.bin", stream)  # type: ignore[arg-type]

    assert storage.metrics.write_bytes == 5000
    assert storage.get("s.bin") == body


def test_storage_s3_put_stream_counts_only_the_unread_rest(storage: StorageS3) -> None:
    stream = io.BytesIO(b"headbody")
    stream.read(4)

    storage.put_stream("s.bin", stream)

    assert storage.metrics.write_bytes == 4
    assert storage.get("s.bin") == b"body"


def test_storage_s3_put_stream_then_get_stream(storage: StorageS3) -> None:
    storage.put_stream("s.bin", io.BytesIO(b"streamed"))
    stream = storage.get_stream("s.bin")
    assert stream.read() == b"streamed"


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


def test_storage_s3_list_returns_only_regular_keys(storage: StorageS3) -> None:
    _put(storage, "a.txt", b"a")
    _put(storage, "sub/b.txt", b"b")
    assert set(storage.list()) == {"a.txt", "sub/b.txt"}


def test_storage_s3_list_with_prefix(moto_server: str) -> None:
    with s3_bucket(moto_server, prefix="repo/factory") as s:
        s.put("a.txt", b"a")
        assert set(s.list()) == {"a.txt"}
        assert s.get("a.txt") == b"a"


def test_storage_s3_prefix_sees_no_sibling_prefix_keys(storage: StorageS3) -> None:
    def at(prefix: str) -> StorageS3:
        return s3_storage(storage.endpoint, storage.bucket, prefix=prefix)

    pkg, pkg2 = at("pkg"), at("pkg2")
    pkg.put("a.deb", b"a")
    storage.put("pkgfoo.txt", b"x")
    with maybe_transaction(pkg2):
        pkg2.put("b.deb", b"b")
        pkg2.put("b.deb", b"c")

    assert pkg.list() == ["a.deb"]
    assert pkg.hidden_objects() == []
    assert pkg.find_orphaned_footmarks([]) == []


def test_storage_s3_prefix_trailing_slash_reaches_the_same_keys(storage: StorageS3) -> None:
    """`dir/` and `dir` name one key space, so neither sees the other's footmarks as orphans."""
    plain = s3_storage(storage.endpoint, storage.bucket, prefix="dir")
    slashed = s3_storage(storage.endpoint, storage.bucket, prefix="dir/")
    with maybe_transaction(slashed):
        slashed.put("a.txt", b"a")
        slashed.put("a.txt", b"b")

    assert slashed.prefix == "dir"
    assert plain.list() == ["a.txt"]
    assert plain.get("a.txt") == b"b"
    assert plain.find_orphaned_footmarks([]) == slashed.find_orphaned_footmarks([])


@pytest.mark.parametrize("prefix", ["", "/", "//"])
def test_storage_s3_empty_prefix_is_the_bucket_root(storage: StorageS3, prefix: str) -> None:
    """An empty or slash-only prefix is no prefix, not keys starting with `/`."""
    rooted = s3_storage(storage.endpoint, storage.bucket, prefix=prefix)
    rooted.put("a.txt", b"a")

    assert rooted.prefix is None
    assert storage.list() == ["a.txt"]


@pytest.mark.parametrize("prefix", ["/x", "a//b"])
def test_storage_s3_prefix_with_an_empty_segment_raises(moto_server: str, prefix: str) -> None:
    with pytest.raises(ValueError, match="empty segment"):
        s3_storage(moto_server, "test", prefix=prefix)


# --------------------------------------------------------------------------- #
# delete / copy / move
# --------------------------------------------------------------------------- #


def test_storage_s3_delete_removes_object(storage: StorageS3) -> None:
    _put(storage, "d.txt", b"x")
    storage.delete("d.txt")
    assert not storage.exists("d.txt")


def test_storage_s3_copy_creates_destination(storage: StorageS3) -> None:
    _put(storage, "src.txt", b"data")
    storage.copy("src.txt", "dst.txt")
    assert storage.get("dst.txt") == b"data"
    assert storage.exists("src.txt")


# --------------------------------------------------------------------------- #
# mkdir / rmdir / format
# --------------------------------------------------------------------------- #


def test_storage_s3_mkdir_rmdir_noop(storage: StorageS3) -> None:
    # These are no-ops on S3 but must not raise.
    storage.mkdir("anything")
    storage.rmdir("anything")


def test_storage_s3_format_recreates_bucket(storage: StorageS3) -> None:
    _put(storage, "before.txt", b"x")
    storage.format()
    assert storage.list() == []


def test_storage_s3_format_refuses_a_prefix(moto_server: str) -> None:
    """The bucket holds keys outside the prefix, which a reset would delete."""
    with s3_bucket(moto_server, prefix="a") as prefixed:
        sibling = s3_storage(moto_server, prefixed.bucket, prefix="b")
        sibling.put("y.txt", b"y")
        with pytest.raises(StorageException, match="Refusing to format a prefixed storage"):
            prefixed.format()
        assert sibling.get("y.txt") == b"y"


# --------------------------------------------------------------------------- #
# ACL preservation across `copy()` and `put`
# --------------------------------------------------------------------------- #
#
# `S3Client.object_copy` maps the source's group grants to a canned ACL on the
# destination, or applies the sidecar's ACL inside a transaction
# → STORAGE.md#s3-acl-rules. The first test locks the public-read carry-over;
# the second guards against spuriously granting public-read when the source is
# private.


def test_copy_preserves_public_read_acl(storage: StorageS3) -> None:
    storage.put("src.txt", b"data", acl="public-read")
    storage.copy("src.txt", "dst.txt")
    assert is_public(storage, "dst.txt")


def test_copy_source_without_public_acl_does_not_grant_public(moto_server: str) -> None:
    # Use a private bucket (no `bucket_acl="public-read"`) so objects do not
    # inherit a public grant by default.
    with s3_bucket(moto_server) as s:
        s.put("src.txt", b"data")
        assert not is_public(s, "src.txt")
        s.copy("src.txt", "dst.txt")
        assert not is_public(s, "dst.txt")


def test_put_records_acl_in_sidecar_inside_a_transaction(storage: StorageS3) -> None:
    storage.transaction_begin()
    storage.put("a.txt", b"x", acl="bucket-owner-full-control")
    put_key = storage._add_footmark(storage._obj_full("a.txt"), ObjectOperation.PUT)
    assert storage._acls.get(put_key) == "bucket-owner-full-control"


def test_put_without_an_acl_inside_a_transaction_records_nothing(storage: StorageS3) -> None:
    storage.transaction_begin()
    storage.put("a.txt", b"x", acl=None)
    assert storage._acls == {}


def test_put_outside_a_transaction_leaves_the_sidecar_empty(storage: StorageS3) -> None:
    storage.put("a.txt", b"x", acl="public-read")
    assert storage._acls.get(storage._obj_full("a.txt")) is None


def test_reserved_write_inside_a_transaction_leaves_the_sidecar_empty(storage: StorageS3) -> None:
    """A reserved write bypasses the overlay, so it carries no per-transaction ACL."""
    storage.transaction_begin()
    storage._obj_put_reserved(HISTORY_FILENAME, b"[]")
    assert storage._acls.get(storage._obj_full(HISTORY_FILENAME)) is None


def test_transaction_begin_clears_the_sidecar(storage: StorageS3) -> None:
    storage.transaction_begin()
    storage.put("a.txt", b"x", acl="public-read")
    storage.transaction_commit()
    storage.transaction_begin()
    assert storage._acls.get(storage._obj_full("a.txt")) is None


def test_verify_public_passes_for_a_public_object(storage_public_verify: StorageS3) -> None:
    storage_public_verify.put("pub.txt", b"x")
    storage_public_verify.verify_public("pub.txt")


def test_verify_public_raises_for_a_private_object(storage: StorageS3) -> None:
    storage.put("priv.txt", b"x", acl="private")
    with pytest.raises(StorageException, match="has not public ACL"):
        storage.verify_public("priv.txt")


def test_put_with_a_non_public_acl_skips_the_public_probe(
    storage_public_verify: StorageS3,
) -> None:
    """Only a `public-read` / `public-read-write` write is probed → STORAGE.md#s3-acl-rules."""
    storage_public_verify.put("priv.txt", b"x", acl="private")
    assert storage_public_verify.get("priv.txt") == b"x"
    assert storage_public_verify.metrics.write_errors_total == 0


def test_put_that_is_not_public_raises_and_counts_a_write_error(
    storage_public_verify: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_only = [{"Grantee": {"Type": "CanonicalUser", "ID": "o"}, "Permission": "FULL_CONTROL"}]
    monkeypatch.setattr(
        storage_public_verify._s3_client, "object_get_acl", lambda _bucket, _key: owner_only
    )
    with pytest.raises(StorageException, match="has not public ACL"):
        storage_public_verify.put("pub.txt", b"x")
    assert storage_public_verify.metrics.write_errors_total == 1


def test_put_in_a_transaction_probes_the_staged_footmark(storage_public_verify: StorageS3) -> None:
    """The original is untouched until commit, so probing it would check the wrong object."""
    model = storage_public_verify.transaction_begin()
    probed: list[str] = []
    real = storage_public_verify._s3_client.object_get_acl

    def spy(bucket: str, key: str) -> Any:
        probed.append(key)
        return real(bucket, key)

    with patch.object(storage_public_verify._s3_client, "object_get_acl", side_effect=spy):
        storage_public_verify.put("pub.txt", b"x")
    staged = storage_public_verify._add_footmark(
        storage_public_verify._obj_full("pub.txt"), ObjectOperation.PUT
    )
    assert probed == [staged]
    assert model.footmarks[ObjectOperation.PUT.value] in staged
    storage_public_verify.transaction_discard()


@pytest.mark.parametrize(
    "error",
    [
        # A dropped connection under an HTTP transfer: not a backend error.
        ProtocolError("Connection broken: IncompleteRead"),
        ValueError("caller stream bug"),
        StorageException("source read failed"),
    ],
    ids=["urllib3", "other", "storage"],
)
def test_put_stream_wraps_a_failing_source_and_counts_one_write_error(
    storage: StorageS3, error: Exception
) -> None:
    with pytest.raises(StorageException) as raised:
        storage.put_stream("out.bin", FailingStream(error=error))  # type: ignore[arg-type]
    assert storage.metrics.write_errors_total == 1
    assert not storage.exists("out.bin")
    if isinstance(error, StorageException):
        assert raised.value is error


def test_create_bucket_is_a_no_op_when_the_bucket_exists(storage: StorageS3) -> None:
    storage.put("keep.txt", b"x")
    storage.create_bucket()
    assert storage.get("keep.txt") == b"x"


def test_delete_bucket_is_a_no_op_when_the_bucket_is_missing(storage: StorageS3) -> None:
    storage.delete_bucket()
    assert storage.exists_bucket() is False
    storage.delete_bucket()


# --------------------------------------------------------------------------- #
# _obj_exists error handling (classifiers → tests/test_s3_errors.py)
# --------------------------------------------------------------------------- #


def test_obj_exists_404_returns_false(storage: StorageS3, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        storage._s3_client,
        "object_head",
        lambda b, k: (_ for _ in ()).throw(client_error("404")),
    )
    assert storage._obj_exists("any.txt") is False


def test_obj_exists_http_404_without_code_returns_false(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        storage._s3_client,
        "object_head",
        lambda b, k: (_ for _ in ()).throw(client_error("", status=404)),
    )
    assert storage._obj_exists("any.txt") is False


def test_obj_exists_nosuchkey_returns_false(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        storage._s3_client,
        "object_head",
        lambda b, k: (_ for _ in ()).throw(client_error("NoSuchKey")),
    )
    assert storage._obj_exists("any.txt") is False


def test_obj_exists_notfound_returns_false(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        storage._s3_client,
        "object_head",
        lambda b, k: (_ for _ in ()).throw(client_error("NotFound")),
    )
    assert storage._obj_exists("any.txt") is False


def test_obj_get_reserved_returns_none_only_for_a_missing_key(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused read is a failure, never an absent history file."""
    assert storage._obj_get_reserved(".transactions.json") is None
    exc = client_error("AccessDenied", status=403)
    monkeypatch.setattr(
        storage._s3_client, "object_get_with_etag", lambda b, k: (_ for _ in ()).throw(exc)
    )
    with pytest.raises(StorageException, match="Error reading S3 object") as exc_info:
        storage._obj_get_reserved(".transactions.json")
    assert exc_info.value.__cause__ is exc


def test_obj_exists_treats_a_missing_bucket_as_a_failure(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`NoSuchBucket` is not a missing key: reporting it as absent would hide a misconfiguration."""
    exc = client_error("NoSuchBucket", status=404)
    monkeypatch.setattr(storage._s3_client, "object_head", lambda b, k: (_ for _ in ()).throw(exc))
    with pytest.raises(StorageException, match="Error checking S3 object") as exc_info:
        storage._obj_exists("any.txt")
    assert type(exc_info.value) is StorageException
    assert exc_info.value.__cause__ is exc


@pytest.mark.parametrize(
    "exc",
    [client_error("NoSuchBucket", status=404), client_error("", status=404)],
    ids=["no-such-bucket", "bare-404"],
)
def test_exists_bucket_false_when_the_bucket_is_missing(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch, exc: ClientError
) -> None:
    monkeypatch.setattr(storage._s3_client, "bucket_head", lambda b: (_ for _ in ()).throw(exc))
    assert storage.exists_bucket() is False


def test_obj_exists_access_denied_raises(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    exc = client_error("AccessDenied")
    monkeypatch.setattr(storage._s3_client, "object_head", lambda b, k: (_ for _ in ()).throw(exc))
    with pytest.raises(StorageException, match="Error checking S3 object") as exc_info:
        storage._obj_exists("any.txt")
    assert type(exc_info.value) is StorageException
    assert exc_info.value.__cause__ is exc


def test_obj_exists_internal_error_raises(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    exc = client_error("InternalError")
    monkeypatch.setattr(storage._s3_client, "object_head", lambda b, k: (_ for _ in ()).throw(exc))
    with pytest.raises(StorageException, match="Error checking S3 object") as exc_info:
        storage._obj_exists("any.txt")
    assert type(exc_info.value) is StorageException
    assert exc_info.value.__cause__ is exc


@pytest.mark.parametrize(
    "error",
    [ConnectionError("network gone"), EndpointConnectionError(endpoint_url="http://s3")],
    ids=["os-error", "botocore-error"],
)
def test_obj_exists_wraps_a_non_client_error(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(
        storage._s3_client, "object_head", lambda b, k: (_ for _ in ()).throw(error)
    )
    with pytest.raises(StorageException, match="Error checking S3 object") as exc_info:
        storage._obj_exists("any.txt")
    assert exc_info.value.__cause__ is error


# --------------------------------------------------------------------------- #
# delete-in-txn regression: non-404 from object_head must surface
# --------------------------------------------------------------------------- #


def test_delete_in_txn_surfaces_non_404_errors(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AccessDenied from object_head must raise StorageException, not drop the PUT footmark."""
    _put(storage, "foo.txt", b"original")
    txn = storage.transaction_begin()
    del txn

    exc = client_error("AccessDenied")
    monkeypatch.setattr(storage._s3_client, "object_head", lambda b, k: (_ for _ in ()).throw(exc))
    with pytest.raises(StorageException, match="Error checking S3 object") as exc_info:
        storage.delete("foo.txt")
    # Not a `StorageNotFoundException`: AccessDenied must never read as a missing object.
    assert type(exc_info.value) is StorageException
    assert exc_info.value.__cause__ is exc


# --------------------------------------------------------------------------- #
# Transaction history ACL
# --------------------------------------------------------------------------- #


def test_transaction_history_save_is_private_with_public_default(
    storage_public_verify: StorageS3,
) -> None:
    """History must stay private even when default_acl is public-read + verify on."""
    history = TransactionHistory(storage_public_verify)
    model = TransactionModel(1, Timestamp.from_json("2026-01-01T00:00:00Z"))
    history.append(model)
    assert history.load() == [model]
    assert not is_public(storage_public_verify, HISTORY_FILENAME)


@pytest.fixture
def storage_with_default_acl(
    moto_server: str, request: pytest.FixtureRequest
) -> Iterator[StorageS3]:
    """A `StorageS3` with ``default_acl=request.param``; ``None`` is what `NO_ACL` maps to."""
    with s3_bucket(moto_server, default_acl=request.param) as s:
        yield s


@pytest.mark.parametrize(
    ("storage_with_default_acl", "reserved_acl"),
    [
        (None, None),
        ("public-read", "private"),
        ("private", "private"),
        ("bucket-owner-full-control", "bucket-owner-full-control"),
    ],
    ids=["no-acl", "public-read", "private", "bucket-owner-full-control"],
    indirect=["storage_with_default_acl"],
)
def test_storage_s3_writes_carry_no_acl_under_no_acl_and_reserved_objects_the_reserved_acl(
    storage_with_default_acl: StorageS3, reserved_acl: str | None
) -> None:
    """`NO_ACL` sends no ACL on any write: object, commit copy, lock, or history.

    A bucket that enforces bucket-owner ownership refuses every ACL but
    `bucket-owner-full-control`, so that default is kept on reserved objects, and an explicit
    `private` on the lock made transactions fail where plain puts worked
    (STORAGE.md#s3-acl-rules). An expired lock is seeded so the take-over write is checked
    too; moto does not enforce bucket-owner ownership, so the ACL reaching boto is asserted.
    """
    storage = storage_with_default_acl
    default_acl = storage._default_acl
    stale = json.dumps(
        {"owner": "ghost", "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    ).encode("utf-8")
    storage._obj_put_reserved(LOCK_FILENAME, stale)

    boto = storage._s3_client._client
    with (
        patch.object(boto, "put_object", wraps=boto.put_object) as put,
        patch.object(boto, "copy", wraps=boto.copy) as copy,
        maybe_transaction(storage),
    ):
        storage.put("a.txt", b"x")
        footmark = storage._add_footmark(storage._obj_full("a.txt"), ObjectOperation.PUT)

    def put_acls(key: str) -> list[str | None]:
        return [c.kwargs.get("ACL") for c in put.call_args_list if c.kwargs["Key"] == key]

    assert put_acls(footmark) == [default_acl]
    assert put_acls(storage._obj_full(HISTORY_FILENAME)) == [reserved_acl]
    lock_key = storage._obj_full(LOCK_FILENAME)
    take_overs = [
        c for c in put.call_args_list if c.kwargs["Key"] == lock_key and "IfMatch" in c.kwargs
    ]
    assert take_overs, "the expired lock was never taken over"
    assert set(put_acls(lock_key)) == {reserved_acl}
    commits = [c for c in copy.call_args_list if c.args[2] == storage._obj_full("a.txt")]
    assert [c.kwargs["ExtraArgs"].get("ACL") for c in commits] == [default_acl]


# --- parse_bucket_acl


@pytest.mark.parametrize(
    "value",
    ["authenticated-read", "private", "public-read", "public-read-write"],
)
def test_parse_bucket_acl_accepts_valid_values(value: str) -> None:
    assert parse_bucket_acl(value) == value


@pytest.mark.parametrize("value", ["public-write", "foo", "", "Public-Read"])
def test_parse_bucket_acl_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="invalid S3"):
        parse_bucket_acl(value)


# --- parse_object_acl


@pytest.mark.parametrize(
    "value",
    [
        "authenticated-read",
        "aws-exec-read",
        "bucket-owner-full-control",
        "bucket-owner-read",
        "private",
        "public-read",
        "public-read-write",
    ],
)
def test_parse_object_acl_accepts_valid_values(value: str) -> None:
    assert parse_object_acl(value) == value


@pytest.mark.parametrize("value", ["public-write", "foo", "", "Private"])
def test_parse_object_acl_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="invalid S3"):
        parse_object_acl(value)


@pytest.mark.parametrize("acl", ["0640", "none", "public-write"])
@pytest.mark.parametrize(
    ("existing", "call"),
    [
        pytest.param(False, lambda s, acl: s.put("k", b"x", acl=acl), id="put"),
        pytest.param(True, lambda s, acl: s.add_metadata("k", {"m": "v"}, acl=acl), id="metadata"),
    ],
)
def test_a_per_call_acl_that_is_no_canned_acl_fails_before_any_request(
    storage: StorageS3, existing: bool, call: Callable[[StorageS3, str], None], acl: str
) -> None:
    """Refused locally as `ValueError`, never as a late server-side `StorageException`."""
    if existing:
        storage.put("k", b"x")
    writes_before = storage.metrics.write_objects_total
    with pytest.raises(ValueError, match="invalid S3 object ACL"):
        call(storage, acl)
    assert storage.metrics.write_objects_total == writes_before
    assert storage.exists("k") is existing


# --------------------------------------------------------------------------- #
# add_metadata fallback
# --------------------------------------------------------------------------- #


def test_add_metadata_replaces_metadata_that_would_exceed_the_limit(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    storage.put("a.txt", b"x", metadata={"old": "1"})
    too_large = client_error("MetadataTooLarge", status=400)
    monkeypatch.setattr(
        storage._s3_client, "object_put_metadata", lambda *_, **__: (_ for _ in ()).throw(too_large)
    )
    rewrites: list[str] = []
    real_rewrite = storage._s3_client._rewrite_metadata

    def counting_rewrite(obj: Any, *args: Any) -> None:
        rewrites.append(obj.key)
        real_rewrite(obj, *args)

    monkeypatch.setattr(storage._s3_client, "_rewrite_metadata", counting_rewrite)
    with caplog.at_level(logging.WARNING):
        storage.add_metadata("a.txt", {"new": "2"})

    assert rewrites == ["a.txt"]  # one rewrite: no window without metadata
    assert storage.get_metadata("a.txt") == {"new": "2"}
    assert "exceeds the S3 limit" in caplog.text


def test_add_metadata_replacing_rewrite_applies_the_acl(
    moto_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback rewrite applies the caller's `acl` as the merge does; the bucket is private."""
    with s3_bucket(moto_server) as storage:
        storage.put("a.txt", b"x", metadata={"old": "1"})
        too_large = client_error("MetadataTooLarge", status=400)
        monkeypatch.setattr(
            storage._s3_client,
            "object_put_metadata",
            lambda *_, **__: (_ for _ in ()).throw(too_large),
        )

        storage.add_metadata("a.txt", {"new": "2"}, acl="public-read")

        assert storage.get_metadata("a.txt") == {"new": "2"}
        assert is_public(storage, "a.txt")


def test_add_metadata_keeps_metadata_when_the_replacing_rewrite_fails(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.put("a.txt", b"x", metadata={"old": "1"})
    too_large = client_error("MetadataTooLarge", status=400)
    slow_down = client_error("SlowDown", status=503)
    monkeypatch.setattr(
        storage._s3_client, "object_put_metadata", lambda *_, **__: (_ for _ in ()).throw(too_large)
    )
    monkeypatch.setattr(
        storage._s3_client,
        "object_replace_metadata",
        lambda *_, **__: (_ for _ in ()).throw(slow_down),
    )

    with pytest.raises(StorageException, match="Error writing metadata a.txt"):
        storage.add_metadata("a.txt", {"new": "2"})

    assert storage.get_metadata("a.txt") == {"old": "1"}


def test_add_metadata_keeps_metadata_on_any_other_error(
    storage: StorageS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.put("a.txt", b"x", metadata={"old": "1"})
    denied = client_error("AccessDenied", status=403)
    monkeypatch.setattr(
        storage._s3_client, "object_put_metadata", lambda *_, **__: (_ for _ in ()).throw(denied)
    )

    with pytest.raises(StorageException, match="Error writing metadata a.txt") as exc_info:
        storage.add_metadata("a.txt", {"new": "2"})

    assert exc_info.value.__cause__ is denied
    assert storage.get_metadata("a.txt") == {"old": "1"}


# --------------------------------------------------------------------------- #
# Error mapping: every boto3 failure surfaces as StorageException
# --------------------------------------------------------------------------- #


_DENIED = client_error("AccessDenied", status=403)
_DOWN = EndpointConnectionError(endpoint_url="http://s3")


@pytest.mark.parametrize("error", [_DENIED, _DOWN], ids=["client-error", "connection"])
@pytest.mark.parametrize(
    ("client_method", "call", "message", "metric"),
    [
        pytest.param(
            "object_get_stream", lambda s: s.get("a.txt"), "Error reading", "read", id="get"
        ),
        pytest.param(
            "object_get_stream",
            lambda s: s.get_stream("a.txt"),
            "Error reading",
            "read",
            id="get_stream",
        ),
        pytest.param(
            "object_get_metadata",
            lambda s: s.get_metadata("a.txt"),
            "Error reading S3 object metadata",
            None,
            id="get_metadata",
        ),
        pytest.param(
            "object_upload",
            lambda s: s.put("b.txt", b"x"),
            "Error writing S3 object",
            "write",
            id="put",
        ),
        pytest.param(
            "object_delete_metadata",
            lambda s: s.delete_metadata("a.txt"),
            "Error deleting metadata",
            None,
            id="delete_metadata",
        ),
        pytest.param("bucket_list", lambda s: s.list(), "Error listing S3 bucket", None, id="list"),
        pytest.param(
            "object_head",
            lambda s: s.exists("a.txt"),
            "Error checking S3 object",
            None,
            id="exists",
        ),
        # `delete` probes the object before deleting it, so both calls are failure points.
        pytest.param(
            "object_head",
            lambda s: s.delete("a.txt"),
            "Error checking S3 object",
            None,
            id="delete-head",
        ),
        pytest.param(
            "object_delete",
            lambda s: s.delete("a.txt"),
            "Failed to delete a.txt",
            None,
            id="delete",
        ),
        pytest.param(
            "object_copy",
            lambda s: s.copy("a.txt", "b.txt"),
            "Failed to copy a.txt -> b.txt",
            None,
            id="copy",
        ),
        pytest.param(
            "object_copy",
            lambda s: s.move("a.txt", "b.txt"),
            "Failed to copy a.txt -> b.txt",
            None,
            id="move",
        ),
        pytest.param(
            "object_delete_metadata",
            lambda s: s.delete_metadata_fields("a.txt", ["k"]),
            "Error deleting metadata",
            None,
            id="delete_metadata_fields",
        ),
        pytest.param(
            "bucket_head",
            lambda s: s.exists_bucket(),
            "Error checking S3 bucket",
            None,
            id="exists_bucket",
        ),
        # `create_bucket` checks the bucket first, so both calls are failure points.
        pytest.param(
            "bucket_head",
            lambda s: s.create_bucket(),
            "Error checking S3 bucket",
            None,
            id="create_bucket-head",
        ),
        pytest.param(
            "bucket_delete",
            lambda s: s.delete_bucket(),
            "Error deleting S3 bucket",
            None,
            id="delete_bucket",
        ),
    ],
)
def test_storage_s3_maps_a_backend_failure(
    storage: StorageS3,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    client_method: str,
    call: Callable[[StorageS3], object],
    message: str,
    metric: str | None,
) -> None:
    storage.put("a.txt", b"x")
    # Scoped, so the fixture's bucket delete runs on the real client.
    with monkeypatch.context() as patched:
        patched.setattr(
            storage._s3_client, client_method, lambda *_, **__: (_ for _ in ()).throw(error)
        )
        with pytest.raises(StorageException, match=message) as exc_info:
            call(storage)

    assert type(exc_info.value) is StorageException
    assert exc_info.value.__cause__ is error
    errors = storage.metrics.dump()
    assert errors["read_errors_total"] == (1 if metric == "read" else 0)
    assert errors["write_errors_total"] == (1 if metric == "write" else 0)


def test_create_bucket_maps_a_bucket_create_failure(storage: StorageS3) -> None:
    """The matrix fails the existence check; this reaches the create call itself."""
    storage.delete_bucket()
    with (
        patch.object(storage._s3_client, "bucket_create", side_effect=_DENIED),
        pytest.raises(StorageException, match="Error creating S3 bucket") as raised,
    ):
        storage.create_bucket()
    assert raised.value.__cause__ is _DENIED
