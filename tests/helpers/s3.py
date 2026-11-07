"""S3 test helpers: moto-backed storages and botocore errors (TESTING.md#harness-and-gates)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from botocore.exceptions import ClientError

from txstore import BucketCannedACL, ObjectCannedACL, StorageS3


def client_error(code: str | None = None, status: int | None = None, op: str = "Op") -> ClientError:
    """A `ClientError` shaped like a botocore response.

    Args:
        code: ``Error.Code``; ``None`` leaves the ``Error`` block out, ``""`` keeps an empty code.
        status: ``ResponseMetadata.HTTPStatusCode``; ``None`` leaves the block out.
        op: Operation name botocore puts in the message.
    """
    response: dict[str, object] = {}
    if code is not None:
        response["Error"] = {"Code": code, "Message": code or "error"}
    if status is not None:
        response["ResponseMetadata"] = {"HTTPStatusCode": status}
    return ClientError(response, op)  # type: ignore[arg-type]


def is_public(storage: StorageS3, path: str) -> bool:
    """Run the production public-ACL probe on `path`."""
    return storage._is_public(storage._obj_full(path))


def s3_storage(
    endpoint: str,
    bucket: str,
    *,
    prefix: str | None = None,
    create_bucket: bool = False,
    bucket_acl: BucketCannedACL | None = None,
    default_acl: ObjectCannedACL | None = None,
    verify_public_acl: bool = False,
) -> StorageS3:
    """A `StorageS3` on the moto `endpoint` with the test credentials; probe off by default."""
    return StorageS3(
        endpoint=endpoint,
        bucket=bucket,
        access_token="testing",
        secret_token="testing",
        prefix=prefix,
        verify=False,
        create_bucket=create_bucket,
        bucket_acl=bucket_acl,
        default_acl=default_acl,
        verify_public_acl=verify_public_acl,
    )


@contextmanager
def s3_bucket(
    endpoint: str,
    *,
    name: str = "test",
    prefix: str | None = None,
    bucket_acl: BucketCannedACL | None = None,
    default_acl: ObjectCannedACL | None = None,
    verify_public_acl: bool = False,
) -> Iterator[StorageS3]:
    """A `s3_storage` on a fresh ``<name>-<hex>`` bucket, deleted on exit.

    The delete runs through a second instance on the same bucket: the autouse `monkeypatch`
    outlives a fixture built on this, so a test-wide patch of the yielded instance or its client
    is still active at exit and would otherwise skip or break the delete.
    """
    storage = s3_storage(
        endpoint,
        f"{name}-{uuid.uuid4().hex[:12]}",
        prefix=prefix,
        create_bucket=True,
        bucket_acl=bucket_acl,
        default_acl=default_acl,
        verify_public_acl=verify_public_acl,
    )
    cleanup = s3_storage(endpoint, storage.bucket)
    try:
        yield storage
    finally:
        cleanup.delete_bucket()
