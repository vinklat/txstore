"""Thin boto3 wrapper with the S3 operations `StorageS3` needs.

ACL arguments are `ObjectCannedACL` names: `StorageS3` parses the plain strings the `Storage` ABI
passes before they reach this module.

One `S3Client` per `StorageS3` instance holds a boto3 S3 service resource and exposes bucket and
object calls by name. Errors surface as boto3 raises them (`ClientError`, `BotoCoreError`), plus
an `OSError` for a malformed multipart response; callers catch `S3_BACKEND_ERRORS` and map them to
`StorageException` → [EXCEPTIONS.md](EXCEPTIONS.md#storage-exceptions).

Metadata rewrites copy an object onto itself with ``MetadataDirective="REPLACE"``, which resets
system headers and the ACL unless they are passed again, so both are carried over. Objects at or
above `LARGE_OBJECT_THRESHOLD` exceed the single-request copy limit and are rewritten with a
multipart copy → [STORAGE.md](STORAGE.md#s3-metadata-rewrites).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import IO, TYPE_CHECKING, Any, Final, Literal, TypedDict

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from botocore.response import StreamingBody
    from mypy_boto3_s3.client import S3Client as BotoS3Client
    from mypy_boto3_s3.service_resource import Object, S3ServiceResource
    from mypy_boto3_s3.type_defs import (
        CompletedPartTypeDef,
        GrantTypeDef,
        ObjectIdentifierTypeDef,
    )

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

BucketCannedACL = Literal["authenticated-read", "private", "public-read", "public-read-write"]
ObjectCannedACL = Literal[
    "authenticated-read",
    "aws-exec-read",
    "bucket-owner-full-control",
    "bucket-owner-read",
    "private",
    "public-read",
    "public-read-write",
]

# Everything an `S3Client` call raises for a backend failure, so every caller wraps the same set;
# a caller that classifies `ClientError` first re-raises the rest inside `s3_errors`.
S3_BACKEND_ERRORS: Final = (ClientError, BotoCoreError, OSError)

KIB = 1024
MIB = KIB * KIB
GIB = MIB * KIB

# Single-request CopyObject accepts objects below 5 GiB.
LARGE_OBJECT_THRESHOLD = 5 * GIB
# Part size of a multipart metadata rewrite; small enough to finish within the read timeout.
MULTIPART_COPY_PART_SIZE = 256 * MIB

_TIMEOUT_S = 60
_POOL_SIZE = 10
_TOTAL_MAX_ATTEMPTS = 3

_UPLOAD_CONCURRENCY = 10
_UPLOAD_PART_SIZE = 50 * MIB
_UPLOAD_MULTIPART_THRESHOLD = 100 * MIB


class _AclArgs(TypedDict, total=False):
    """The ``ACL`` keyword of a boto object write; absent means no ACL header."""

    ACL: ObjectCannedACL


def _acl_args(acl: ObjectCannedACL | None) -> _AclArgs:
    """Return ``{"ACL": acl}``, or no keyword when *acl* is None so no ACL header is sent."""
    return {} if acl is None else {"ACL": acl}


# Headers a REPLACE copy would reset: (resource attribute, request parameter).
_SYSTEM_HEADERS: tuple[tuple[str, str], ...] = (
    ("content_type", "ContentType"),
    ("content_encoding", "ContentEncoding"),
    ("content_disposition", "ContentDisposition"),
    ("content_language", "ContentLanguage"),
    ("cache_control", "CacheControl"),
)

_ALL_USERS_URI = "http://acs.amazonaws.com/groups/global/AllUsers"
_AUTHENTICATED_USERS_URI = "http://acs.amazonaws.com/groups/global/AuthenticatedUsers"

# Group grant → the canned ACL that produces it, broadest first: S3 lists `public-read-write`
# as AllUsers READ then AllUsers WRITE, so the whole set decides, not the first grant.
_GRANT_TO_CANNED_ACL: tuple[tuple[tuple[str, str], ObjectCannedACL], ...] = (
    ((_ALL_USERS_URI, "WRITE"), "public-read-write"),
    ((_ALL_USERS_URI, "READ"), "public-read"),
    ((_AUTHENTICATED_USERS_URI, "READ"), "authenticated-read"),
)


def _group_grant(grant: GrantTypeDef) -> tuple[str, str] | None:
    """Return a group grant's ``(URI, Permission)``, else None; other grantee keys are ignored."""
    grantee = grant.get("Grantee", {})
    if grantee.get("Type") != "Group":
        return None
    return grantee.get("URI", ""), grant.get("Permission", "")


def canned_acl_from_grants(grants: list[GrantTypeDef]) -> ObjectCannedACL | None:
    """Return the canned ACL a grant list was created with, if it is a group canned ACL.

    Args:
        grants: The ``Grants`` list of a ``GetObjectAcl`` response.

    Returns:
        The broadest canned ACL the group grants produce, in any order, else None (owner-only
        or individually granted objects).
    """
    groups = {_group_grant(grant) for grant in grants}
    for group, acl in _GRANT_TO_CANNED_ACL:
        if group in groups:
            return acl
    return None


def grants_all_users_read(grants: list[GrantTypeDef]) -> bool:
    """Return True iff any grant in `grants` gives the AllUsers group READ."""
    return any(_group_grant(grant) == (_ALL_USERS_URI, "READ") for grant in grants)


class S3Client:
    """Bucket and object operations on one S3 endpoint.

    Callers use these methods only; the boto3 resource and client stay inside this module.
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        verify: bool | str = True,
    ) -> None:
        """Connect to `endpoint` with static credentials.

        Args:
            endpoint: S3 endpoint URL.
            access_key: Access key id.
            secret_key: Secret access key.
            verify: Verify TLS (True/False) or the path of a CA bundle.
        """
        config = Config(
            connect_timeout=_TIMEOUT_S,
            read_timeout=_TIMEOUT_S,
            max_pool_connections=_POOL_SIZE,
            retries={"total_max_attempts": _TOTAL_MAX_ATTEMPTS},
        )
        self._s3: S3ServiceResource = boto3.resource(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            verify=verify,
            config=config,
        )

    @property
    def _client(self) -> BotoS3Client:
        return self._s3.meta.client

    # Buckets

    def bucket_head(self, bucket: str) -> None:
        """HEAD `bucket`; a missing bucket raises `ClientError` like any other failure."""
        self._client.head_bucket(Bucket=bucket)

    def bucket_create(self, bucket: str, acl: BucketCannedACL | None = None) -> None:
        """Create `bucket`, with canned `acl` when given."""
        if acl is None:
            self._s3.create_bucket(Bucket=bucket)
        else:
            self._s3.create_bucket(Bucket=bucket, ACL=acl)

    def bucket_delete(self, bucket: str, delete_nonempty: bool = False) -> None:
        """Delete `bucket`; with `delete_nonempty`, delete its objects first."""
        target = self._s3.Bucket(bucket)
        if delete_nonempty:
            target.objects.all().delete()
        target.delete()

    def bucket_list(self, bucket: str, prefix: str = "") -> Iterator[str]:
        """Yield every key in `bucket` that starts with `prefix`, paginating lazily."""
        objects = self._s3.Bucket(bucket).objects
        listing = objects.filter(Prefix=prefix) if prefix else objects.all()
        return (obj.key for obj in listing)

    # Objects: read

    def object_head(self, bucket: str, key: str) -> None:
        """HEAD `key`; a missing key raises `ClientError` like any other failure."""
        self._client.head_object(Bucket=bucket, Key=key)

    def object_get_stream(self, bucket: str, key: str) -> StreamingBody:
        """Return an unread streaming body of `key`."""
        return self._s3.Object(bucket, key).get()["Body"]

    def object_get_metadata(self, bucket: str, key: str) -> dict[str, str]:
        """Return the user metadata of `key`."""
        return dict(self._client.head_object(Bucket=bucket, Key=key).get("Metadata", {}))

    def object_get_acl(self, bucket: str, key: str) -> list[GrantTypeDef]:
        """Return the ACL grants of `key`."""
        return list(self._client.get_object_acl(Bucket=bucket, Key=key).get("Grants", []))

    # Objects: write

    def object_put(
        self,
        bucket: str,
        key: str,
        data: bytes | IO[bytes],
        metadata: dict[str, str] | None = None,
        acl: ObjectCannedACL | None = None,
    ) -> None:
        """Write `data` to `key` in one request."""
        self._s3.Object(bucket, key).put(Body=data, Metadata=metadata or {}, **_acl_args(acl))

    def object_upload(
        self,
        data: IO[bytes],
        bucket: str,
        key: str,
        metadata: dict[str, str] | None = None,
        acl: ObjectCannedACL | None = None,
        io_chunksize: int = 256 * KIB,
    ) -> None:
        """Stream `data` to `key`, switching to a threaded multipart upload for large bodies.

        Args:
            data: Readable binary stream.
            bucket: Target bucket.
            key: Target key.
            metadata: User metadata.
            acl: Canned ACL.
            io_chunksize: Read size from `data`.
        """
        extra_args: dict[str, Any] = {"Metadata": metadata or {}, **_acl_args(acl)}
        config = TransferConfig(
            max_concurrency=_UPLOAD_CONCURRENCY,
            multipart_chunksize=_UPLOAD_PART_SIZE,
            multipart_threshold=_UPLOAD_MULTIPART_THRESHOLD,
            io_chunksize=io_chunksize,
        )
        self._s3.Bucket(bucket).upload_fileobj(data, Key=key, ExtraArgs=extra_args, Config=config)

    def object_copy(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
        acl: ObjectCannedACL | None = None,
    ) -> ObjectCannedACL | None:
        """Copy an object server-side with canned `acl`, else the source's group canned ACL.

        Returns:
            The canned ACL the copy was made with, or None when it carries none.
        """
        if acl is None:
            acl = canned_acl_from_grants(self.object_get_acl(src_bucket, src_key))
        self._client.copy(
            {"Bucket": src_bucket, "Key": src_key},
            dst_bucket,
            dst_key,
            ExtraArgs=dict(_acl_args(acl)),
        )
        return acl

    def object_delete(self, bucket: str, key: str) -> None:
        """Delete `key`; deleting an absent key succeeds."""
        self._s3.Object(bucket, key).delete()

    def object_delete_many(
        self, bucket: str, keys: list[str]
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Delete up to 1000 `keys` with one ``DeleteObjects`` request.

        Returns:
            ``(deleted, errors)``: the keys the response reports deleted, and ``(key, reason)``
            for every per-key error it reports. A key in neither list is the caller's to judge.
        """
        objects: list[ObjectIdentifierTypeDef] = [{"Key": key} for key in keys]
        resp = self._client.delete_objects(
            Bucket=bucket, Delete={"Objects": objects, "Quiet": False}
        )
        deleted = [d["Key"] for d in resp.get("Deleted", []) if "Key" in d]
        errors = [
            (e["Key"], f"{e.get('Code')}: {e.get('Message')}")
            for e in resp.get("Errors", [])
            if "Key" in e
        ]
        return deleted, errors

    # Objects: conditional writes (the history lock)

    def object_put_if_absent(
        self, bucket: str, key: str, data: bytes, acl: ObjectCannedACL | None
    ) -> str:
        """Create `key` only if it does not exist (``IfNoneMatch=*``); `acl=None` sends none.

        Returns:
            The new object's ETag as reported, or ``""`` when the endpoint omitted it.
        """
        resp = self._client.put_object(
            Bucket=bucket, Key=key, Body=data, IfNoneMatch="*", **_acl_args(acl)
        )
        return str(resp.get("ETag") or "")

    def object_put_if_match(
        self, bucket: str, key: str, data: bytes, acl: ObjectCannedACL | None, etag: str
    ) -> str:
        """Overwrite `key` only while its ETag is still `etag` (``IfMatch``); `acl=None` sends none.

        Returns:
            The new object's ETag as reported, or ``""`` when the endpoint omitted it.
        """
        resp = self._client.put_object(
            Bucket=bucket, Key=key, Body=data, IfMatch=etag, **_acl_args(acl)
        )
        return str(resp.get("ETag") or "")

    def object_get_with_etag(self, bucket: str, key: str) -> tuple[bytes, str]:
        """Return the body of `key` and the ETag it was read with, from one response.

        Returns:
            ``(body, etag)``; ``etag`` is ``""`` when the endpoint omitted it.
        """
        resp = self._client.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read(), str(resp.get("ETag") or "")

    def object_delete_if_match(self, bucket: str, key: str, etag: str) -> None:
        """Delete `key` only while its ETag is still `etag` (``IfMatch``)."""
        self._client.delete_object(Bucket=bucket, Key=key, IfMatch=etag)

    def object_put_metadata(
        self,
        bucket: str,
        key: str,
        metadata: dict[str, str],
        acl: ObjectCannedACL | None = None,
    ) -> None:
        """Merge `metadata` into the user metadata of `key`.

        Args:
            bucket: Bucket.
            key: Object key.
            metadata: Entries to add or overwrite.
            acl: Canned ACL to set; None keeps the object's group canned ACL.
        """
        obj = self._s3.Object(bucket, key)
        merged = {**obj.metadata, **metadata}
        self._rewrite_metadata(obj, merged, acl)

    def object_replace_metadata(
        self,
        bucket: str,
        key: str,
        metadata: dict[str, str],
        acl: ObjectCannedACL | None = None,
    ) -> None:
        """Replace the user metadata of `key` with exactly `metadata`, in one rewrite.

        Args:
            bucket: Bucket.
            key: Object key.
            metadata: The complete new user metadata.
            acl: Canned ACL to set; None keeps the object's group canned ACL.
        """
        self._rewrite_metadata(self._s3.Object(bucket, key), metadata, acl)

    def object_delete_metadata(
        self,
        bucket: str,
        key: str,
        items: list[str] | None = None,
    ) -> None:
        """Remove `items` from the user metadata of `key`; None removes all.

        The object keeps its group canned ACL.

        Args:
            bucket: Bucket.
            key: Object key.
            items: Metadata keys to remove; absent keys are ignored, and an empty list removes
                nothing.
        """
        obj = self._s3.Object(bucket, key)
        if items is None:
            kept: dict[str, str] = {}
        else:
            drop = set(items)
            kept = {k: v for k, v in obj.metadata.items() if k not in drop}
        self._rewrite_metadata(obj, kept, None)

    def _rewrite_metadata(
        self, obj: Object, metadata: dict[str, str], acl: ObjectCannedACL | None
    ) -> None:
        """Copy `obj` onto itself with `metadata`, keeping system headers and the ACL."""
        params: dict[str, Any] = {"Metadata": metadata}
        for attr, param in _SYSTEM_HEADERS:
            if value := getattr(obj, attr, None):
                params[param] = value
        effective_acl = acl if acl is not None else canned_acl_from_grants(obj.Acl().grants)
        params.update(_acl_args(effective_acl))
        if obj.content_length < LARGE_OBJECT_THRESHOLD:
            obj.copy_from(
                CopySource={"Bucket": obj.bucket_name, "Key": obj.key},
                MetadataDirective="REPLACE",
                **params,
            )
            return
        self._multipart_self_copy(obj, params)

    def _multipart_self_copy(self, obj: Object, params: dict[str, Any]) -> None:
        """Rewrite a large object in `MULTIPART_COPY_PART_SIZE` ranges; abort on any failure."""
        bucket, key, size = obj.bucket_name, obj.key, obj.content_length
        upload_id = self._client.create_multipart_upload(Bucket=bucket, Key=key, **params)[
            "UploadId"
        ]
        try:
            parts: list[CompletedPartTypeDef] = []
            for number, start in enumerate(range(0, size, MULTIPART_COPY_PART_SIZE), start=1):
                end = min(start + MULTIPART_COPY_PART_SIZE, size) - 1
                response = self._client.upload_part_copy(
                    Bucket=bucket,
                    Key=key,
                    CopySource={"Bucket": bucket, "Key": key},
                    CopySourceRange=f"bytes={start}-{end}",
                    PartNumber=number,
                    UploadId=upload_id,
                )
                etag = response.get("CopyPartResult", {}).get("ETag")
                if not etag:
                    # An OSError, so callers' boto3-failure handling wraps it like any other.
                    raise OSError(f"S3 part {number} of {bucket}/{key} returned no ETag")
                parts.append({"PartNumber": number, "ETag": etag})
            self._client.complete_multipart_upload(
                Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
            )
        except BaseException:
            try:
                self._client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            except S3_BACKEND_ERRORS as abort_exc:
                logger.warning(
                    "Abort of multipart rewrite %s/%s failed: %s", bucket, key, abort_exc
                )
            raise
