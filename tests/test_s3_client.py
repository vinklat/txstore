"""`txstore._s3_client.S3Client` against the in-process moto server."""

from __future__ import annotations

import io
import logging
import uuid
from collections.abc import Iterator

import pytest
from botocore.exceptions import ClientError
from helpers.s3 import client_error

from txstore import ObjectCannedACL, _s3_client
from txstore._s3_client import (
    S3Client,
    canned_acl_from_grants,
    grants_all_users_read,
)

_ALL_USERS = "http://acs.amazonaws.com/groups/global/AllUsers"
_AUTH_USERS = "http://acs.amazonaws.com/groups/global/AuthenticatedUsers"


@pytest.fixture
def client(moto_server: str) -> S3Client:
    return S3Client(moto_server, "testing", "testing", verify=False)


@pytest.fixture
def bucket(client: S3Client) -> Iterator[str]:
    name = f"client-{uuid.uuid4().hex[:12]}"
    client.bucket_create(name)
    yield name
    if _bucket_exists(client, name):
        client.bucket_delete(name, delete_nonempty=True)


def _bucket_exists(client: S3Client, bucket: str) -> bool:
    try:
        client.bucket_head(bucket)
    except ClientError:
        return False
    return True


def _object_exists(client: S3Client, bucket: str, key: str) -> bool:
    try:
        client.object_head(bucket, key)
    except ClientError:
        return False
    return True


def _head(client: S3Client, bucket: str, key: str) -> dict[str, object]:
    return dict(client._client.head_object(Bucket=bucket, Key=key))


# Grants


@pytest.mark.parametrize(
    ("grants", "expected"),
    [
        pytest.param([], None, id="empty"),
        pytest.param(
            [{"Grantee": {"Type": "CanonicalUser", "ID": "x"}, "Permission": "FULL_CONTROL"}],
            None,
            id="owner-only",
        ),
        pytest.param(
            [{"Grantee": {"Type": "Group", "URI": _ALL_USERS}, "Permission": "READ"}],
            "public-read",
            id="public-read",
        ),
        pytest.param(
            [
                {"Grantee": {"Type": "Group", "URI": _ALL_USERS}, "Permission": "READ"},
                {"Grantee": {"Type": "Group", "URI": _ALL_USERS}, "Permission": "WRITE"},
            ],
            "public-read-write",
            id="public-read-write",
        ),
        pytest.param(
            [
                {"Grantee": {"Type": "Group", "URI": _ALL_USERS}, "Permission": "WRITE"},
                {"Grantee": {"Type": "Group", "URI": _ALL_USERS}, "Permission": "READ"},
            ],
            "public-read-write",
            id="public-read-write-reordered",
        ),
        pytest.param(
            [
                {"Grantee": {"Type": "Group", "URI": _AUTH_USERS}, "Permission": "READ"},
                {"Grantee": {"Type": "Group", "URI": _ALL_USERS}, "Permission": "READ"},
            ],
            "public-read",
            id="authenticated-then-public",
        ),
        pytest.param(
            [{"Grantee": {"Type": "Group", "URI": _AUTH_USERS}, "Permission": "READ"}],
            "authenticated-read",
            id="authenticated-read",
        ),
    ],
)
def test_canned_acl_from_grants(grants: list[dict[str, object]], expected: str | None) -> None:
    assert canned_acl_from_grants(grants) == expected  # type: ignore[arg-type]


_OWNER = {"Grantee": {"Type": "CanonicalUser", "ID": "x"}, "Permission": "FULL_CONTROL"}
_AUTH_READ = {"Grantee": {"Type": "Group", "URI": _AUTH_USERS}, "Permission": "READ"}


@pytest.mark.parametrize(
    ("grants", "expected"),
    [
        pytest.param(
            [_OWNER, {"Grantee": {"Type": "Group", "URI": _ALL_USERS}, "Permission": "READ"}],
            True,
            id="all-users-read",
        ),
        pytest.param(
            [
                {
                    "Grantee": {"Type": "Group", "URI": _ALL_USERS, "DisplayName": "AllUsers"},
                    "Permission": "READ",
                }
            ],
            True,
            id="extra-grantee-key",
        ),
        pytest.param(
            [_AUTH_READ, {"Grantee": {"Type": "Group", "URI": _ALL_USERS}, "Permission": "READ"}],
            True,
            id="after-another-group-grant",
        ),
        pytest.param(
            [{"Grantee": {"Type": "Group", "URI": _ALL_USERS}, "Permission": "WRITE"}],
            False,
            id="write-only",
        ),
        pytest.param([_OWNER, _AUTH_READ], False, id="no-all-users"),
    ],
)
def test_grants_all_users_read(grants: list[dict[str, object]], expected: bool) -> None:
    """An AllUsers READ grant is found in any position and with extra grantee keys; not WRITE."""
    assert grants_all_users_read(grants) is expected  # type: ignore[arg-type]


# Buckets


def test_bucket_head_and_delete_nonempty(client: S3Client, bucket: str) -> None:
    client.object_put(bucket, "a/b.txt", b"x")
    assert _bucket_exists(client, bucket)
    client.bucket_delete(bucket, delete_nonempty=True)
    assert not _bucket_exists(client, bucket)


def test_bucket_delete_nonempty_refused_without_flag(client: S3Client, bucket: str) -> None:
    client.object_put(bucket, "a.txt", b"x")
    with pytest.raises(ClientError):
        client.bucket_delete(bucket)


def test_bucket_list_filters_by_prefix(client: S3Client, bucket: str) -> None:
    for key in ("a/1", "a/2", "b/1"):
        client.object_put(bucket, key, b"")
    assert sorted(client.bucket_list(bucket)) == ["a/1", "a/2", "b/1"]
    assert sorted(client.bucket_list(bucket, prefix="a/")) == ["a/1", "a/2"]


# Objects


def test_object_head_raises_for_a_missing_key(client: S3Client, bucket: str) -> None:
    """The client classifies nothing; `_s3_errors` owns what counts as not found."""
    with pytest.raises(ClientError):
        client.object_head(bucket, "missing")
    client.object_put(bucket, "present", b"x")
    client.object_head(bucket, "present")


def test_object_head_reraises_non_404(client: S3Client, monkeypatch: pytest.MonkeyPatch) -> None:
    denied = client_error("403", op="HeadObject")

    def head_object(**_: object) -> None:
        raise denied

    monkeypatch.setattr(client._client, "head_object", head_object)
    with pytest.raises(ClientError) as excinfo:
        client.object_head("any", "key")
    assert excinfo.value is denied


def test_put_get_round_trip(client: S3Client, bucket: str) -> None:
    client.object_put(bucket, "k", b"payload", metadata={"m": "1"}, acl="public-read")
    assert client.object_get_stream(bucket, "k").read() == b"payload"
    assert client.object_get_metadata(bucket, "k") == {"m": "1"}
    assert canned_acl_from_grants(client.object_get_acl(bucket, "k")) == "public-read"


def test_object_upload_streams_with_metadata_and_acl(client: S3Client, bucket: str) -> None:
    client.object_upload(io.BytesIO(b"x" * 1000), bucket, "up", metadata={"m": "2"}, acl="private")
    assert client.object_get_stream(bucket, "up").read() == b"x" * 1000
    assert client.object_get_metadata(bucket, "up") == {"m": "2"}
    assert canned_acl_from_grants(client.object_get_acl(bucket, "up")) is None


@pytest.mark.parametrize(("acl", "expected"), [("public-read", "public-read"), ("private", None)])
def test_object_copy_carries_group_acl(
    client: S3Client, bucket: str, acl: ObjectCannedACL, expected: str | None
) -> None:
    client.object_put(bucket, "src", b"data", metadata={"m": "1"}, acl=acl)
    client.object_copy(bucket, "src", bucket, "dst")
    assert client.object_get_stream(bucket, "dst").read() == b"data"
    assert client.object_get_metadata(bucket, "dst") == {"m": "1"}
    assert canned_acl_from_grants(client.object_get_acl(bucket, "dst")) == expected


@pytest.mark.parametrize(("acl", "expected"), [("public-read", "public-read"), ("private", None)])
def test_object_copy_returns_the_acl_it_copied_with(
    client: S3Client, bucket: str, acl: ObjectCannedACL, expected: str | None
) -> None:
    client.object_put(bucket, "src", b"data", acl=acl)
    assert client.object_copy(bucket, "src", bucket, "dst") == expected


def test_object_copy_with_an_explicit_acl_skips_the_grant_read(
    client: S3Client, bucket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.object_put(bucket, "src", b"data", acl="private")
    monkeypatch.setattr(client, "object_get_acl", lambda *_: pytest.fail("grant read"))

    assert client.object_copy(bucket, "src", bucket, "dst", acl="public-read") == "public-read"


def test_object_delete_many_reports_deleted_keys(client: S3Client, bucket: str) -> None:
    client.object_put(bucket, "a", b"x")
    deleted, errors = client.object_delete_many(bucket, ["a", "never-existed"])
    assert (sorted(deleted), errors) == (["a", "never-existed"], [])
    assert not _object_exists(client, bucket, "a")


def test_object_delete_many_reports_per_key_errors(
    client: S3Client, bucket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = {
        "Deleted": [{"Key": "a"}],
        "Errors": [{"Key": "b", "Code": "AccessDenied", "Message": "denied"}],
    }
    monkeypatch.setattr(client._client, "delete_objects", lambda **_: response)

    assert client.object_delete_many(bucket, ["a", "b"]) == (
        ["a"],
        [("b", "AccessDenied: denied")],
    )


def test_object_conditional_writes_and_reads(client: S3Client, bucket: str) -> None:
    etag = client.object_put_if_absent(bucket, "lock", b"one", "private")
    with pytest.raises(ClientError):
        client.object_put_if_absent(bucket, "lock", b"two", "private")

    body, read_etag = client.object_get_with_etag(bucket, "lock")
    assert (body, read_etag) == (b"one", etag)

    new_etag = client.object_put_if_match(bucket, "lock", b"three", "private", etag)
    assert client.object_get_with_etag(bucket, "lock") == (b"three", new_etag)
    with pytest.raises(ClientError):
        client.object_put_if_match(bucket, "lock", b"four", "private", etag)


def test_object_delete_is_idempotent(client: S3Client, bucket: str) -> None:
    client.object_put(bucket, "k", b"x")
    client.object_delete(bucket, "k")
    client.object_delete(bucket, "k")
    assert not _object_exists(client, bucket, "k")


def test_object_put_with_acl_sets_the_canned_acl(client: S3Client, bucket: str) -> None:
    client.object_put(bucket, "k", b"x", acl="public-read")
    assert canned_acl_from_grants(client.object_get_acl(bucket, "k")) == "public-read"


# Metadata rewrites


def _put_with_headers(client: S3Client, bucket: str, key: str) -> None:
    client._s3.Object(bucket, key).put(
        Body=b"body",
        Metadata={"a": "1", "b": "2"},
        ContentType="text/plain",
        CacheControl="max-age=60",
        ACL="public-read",
    )


def test_put_metadata_merges_and_keeps_headers_and_acl(client: S3Client, bucket: str) -> None:
    _put_with_headers(client, bucket, "k")
    client.object_put_metadata(bucket, "k", {"b": "20", "c": "3"})
    assert client.object_get_metadata(bucket, "k") == {"a": "1", "b": "20", "c": "3"}
    head = _head(client, bucket, "k")
    assert head["ContentType"] == "text/plain"
    assert head["CacheControl"] == "max-age=60"
    assert canned_acl_from_grants(client.object_get_acl(bucket, "k")) == "public-read"
    assert client.object_get_stream(bucket, "k").read() == b"body"


def test_replace_metadata_drops_old_keys_and_keeps_headers_and_acl(
    client: S3Client, bucket: str
) -> None:
    _put_with_headers(client, bucket, "k")
    client.object_replace_metadata(bucket, "k", {"c": "3"})
    assert client.object_get_metadata(bucket, "k") == {"c": "3"}
    assert _head(client, bucket, "k")["ContentType"] == "text/plain"
    assert canned_acl_from_grants(client.object_get_acl(bucket, "k")) == "public-read"


def test_put_metadata_explicit_acl_wins(client: S3Client, bucket: str) -> None:
    _put_with_headers(client, bucket, "k")
    client.object_put_metadata(bucket, "k", {"c": "3"}, acl="private")
    assert canned_acl_from_grants(client.object_get_acl(bucket, "k")) is None


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        pytest.param(["a", "missing"], {"b": "2"}, id="subset"),
        pytest.param([], {"a": "1", "b": "2"}, id="empty-list-keeps-all"),
        pytest.param(None, {}, id="none-clears"),
    ],
)
def test_delete_metadata(
    client: S3Client, bucket: str, items: list[str] | None, expected: dict[str, str]
) -> None:
    _put_with_headers(client, bucket, "k")
    client.object_delete_metadata(bucket, "k", items)
    assert client.object_get_metadata(bucket, "k") == expected
    assert _head(client, bucket, "k")["ContentType"] == "text/plain"
    assert canned_acl_from_grants(client.object_get_acl(bucket, "k")) == "public-read"


def test_large_object_metadata_rewrite_uses_multipart_copy(
    client: S3Client, bucket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At or above the threshold the rewrite copies ranges; moto requires 5 MiB parts."""
    part = 5 * _s3_client.MIB
    monkeypatch.setattr(_s3_client, "LARGE_OBJECT_THRESHOLD", 1)
    monkeypatch.setattr(_s3_client, "MULTIPART_COPY_PART_SIZE", part)
    body = bytes(range(256)) * (part * 2 // 256) + b"tail"
    client._s3.Object(bucket, "big").put(
        Body=body, Metadata={"a": "1"}, ContentType="application/x-big", ACL="public-read"
    )
    calls: list[str] = []
    real = client._client.upload_part_copy

    def counting_upload_part_copy(**kwargs: object) -> object:
        calls.append(str(kwargs["CopySourceRange"]))
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(client._client, "upload_part_copy", counting_upload_part_copy)
    client.object_put_metadata(bucket, "big", {"b": "2"})
    assert calls == [
        f"bytes=0-{part - 1}",
        f"bytes={part}-{2 * part - 1}",
        f"bytes={2 * part}-{len(body) - 1}",
    ]
    assert client.object_get_metadata(bucket, "big") == {"a": "1", "b": "2"}
    assert client.object_get_stream(bucket, "big").read() == body
    assert _head(client, bucket, "big")["ContentType"] == "application/x-big"
    assert canned_acl_from_grants(client.object_get_acl(bucket, "big")) == "public-read"


def test_large_object_rewrite_aborts_when_a_part_returns_no_etag(
    client: S3Client, bucket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed part response raises an OSError, which callers wrap as a boto3 failure."""
    monkeypatch.setattr(_s3_client, "LARGE_OBJECT_THRESHOLD", 1)
    client.object_put(bucket, "big", b"x" * 10, metadata={"a": "1"})
    monkeypatch.setattr(client._client, "upload_part_copy", lambda **_: {"CopyPartResult": {}})
    with pytest.raises(OSError, match="returned no ETag"):
        client.object_put_metadata(bucket, "big", {"b": "2"})
    assert client._client.list_multipart_uploads(Bucket=bucket).get("Uploads", []) == []


def test_large_object_rewrite_aborts_on_part_failure(
    client: S3Client, bucket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_s3_client, "LARGE_OBJECT_THRESHOLD", 1)
    client.object_put(bucket, "big", b"x" * 10, metadata={"a": "1"})
    boom = client_error("500", op="UploadPartCopy")

    def failing_upload_part_copy(**_: object) -> None:
        raise boom

    monkeypatch.setattr(client._client, "upload_part_copy", failing_upload_part_copy)
    with pytest.raises(ClientError):
        client.object_put_metadata(bucket, "big", {"b": "2"})
    uploads = client._client.list_multipart_uploads(Bucket=bucket).get("Uploads", [])
    assert uploads == []
    assert client.object_get_metadata(bucket, "big") == {"a": "1"}


def test_large_object_rewrite_raises_the_part_failure_when_the_abort_fails(
    client: S3Client, bucket: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(_s3_client, "LARGE_OBJECT_THRESHOLD", 1)
    client.object_put(bucket, "big", b"x" * 10, metadata={"a": "1"})
    boom = client_error("500", op="UploadPartCopy")

    def failing_upload_part_copy(**_: object) -> None:
        raise boom

    def failing_abort(**_: object) -> None:
        raise client_error("503", op="AbortMultipartUpload")

    monkeypatch.setattr(client._client, "upload_part_copy", failing_upload_part_copy)
    monkeypatch.setattr(client._client, "abort_multipart_upload", failing_abort)
    with caplog.at_level(logging.WARNING), pytest.raises(ClientError) as info:
        client.object_put_metadata(bucket, "big", {"b": "2"})
    assert info.value is boom
    assert "Abort of multipart rewrite" in caplog.text
