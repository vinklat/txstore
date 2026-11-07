"""Storage construction from a URL.

`make_storage` routes a storage URL to its backend and resolves each setting from an explicit
argument, then its ``TXSTORE_*`` environment variable where one exists, then the default →
[CONFIG.md](CONFIG.md#storage-construction). Callers never need to import the concrete
backend classes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final, Literal

from txstore._env import env_bool, env_str
from txstore._s3_client import BucketCannedACL, ObjectCannedACL
from txstore.base import Storage, StorageException, StorageType
from txstore.storage_fs import StorageFS
from txstore.storage_http import StorageHTTP
from txstore.storage_s3 import StorageS3, parse_bucket_acl, parse_object_acl
from txstore.url import parse_s3_url_path, parse_storage_url, redact_url

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# `s3_default_acl` value that sends no ACL on writes and skips the public-ACL probe.
NO_ACL: Final = "none"


def make_storage(
    url: str,
    *,
    create_path: bool = False,
    verify_ssl: bool | None = None,
    s3_access: str | None = None,
    s3_secret: str | None = None,
    s3_default_acl: ObjectCannedACL | Literal["none"] | None = None,
    s3_bucket_acl: BucketCannedACL | None = None,
) -> Storage:
    """Build a `Storage` instance for `url`.

    Each ``None`` argument falls back to its ``TXSTORE_*`` variable, then to its default.

    Args:
        url: Storage URL (``fs://``, ``s3://``, ``http://``, ``https://``).
        create_path: Create the destination bucket / directory if missing.
        verify_ssl: Verify TLS certificates (S3 and HTTP).
        s3_access: S3 access key; required for ``s3://``.
        s3_secret: S3 secret key; required for ``s3://``.
        s3_default_acl: Canned ACL for S3 writes that pass none; `NO_ACL`, the default, sends
            no ACL.
        s3_bucket_acl: Canned ACL for bucket creation; unset sends none (the S3 default).

    Raises:
        StorageException: If the URL is empty, its HTTP or S3 form names no host, an ``s3://`` URL
            resolves no access or secret key, or `create_path` is set and the directory or
            bucket cannot be created.
        ValueError: If the URL scheme is not supported, its authority does not parse
            (unencoded `/`, `?`, `#`, or non-ASCII in credentials), the URL carries a query or
            fragment, an ``s3://`` URL holds ``@``, names no bucket, or has an empty prefix
            segment, an ``fs://`` URL path is not absolute, or a setting or its variable holds an
            invalid value.

    Returns:
        Storage: A backend instance routed by the URL scheme.
    """
    if not url:
        raise StorageException("Empty storage URL.")

    verify = _first(verify_ssl, lambda: env_bool("TXSTORE_VERIFY_SSL"), True)

    storage_type, url_scheme, url_netloc, url_path = parse_storage_url(url)
    logger.debug("Build storage: type=%s url=%s", storage_type, redact_url(url))
    if not verify and storage_type in (StorageType.s3, StorageType.http):
        logger.warning("TLS certificate verification is disabled for %s", redact_url(url))

    match storage_type:
        case StorageType.s3:
            return _make_s3(
                f"{url_scheme}://{url_netloc}",
                url_path,
                verify=verify,
                create_path=create_path,
                s3_access=s3_access,
                s3_secret=s3_secret,
                s3_default_acl=s3_default_acl,
                s3_bucket_acl=s3_bucket_acl,
            )
        case StorageType.fs:
            return StorageFS(url_path, create_base=create_path)
        case StorageType.http:
            return StorageHTTP(f"{url_scheme}://{url_netloc}{url_path}", verify=verify)
        case StorageType.null:  # pragma: no cover - `parse_storage_url` never routes to null
            raise AssertionError("StorageType.null is not URL-routable")


def _make_s3(
    endpoint: str,
    url_path: str,
    *,
    verify: bool,
    create_path: bool,
    s3_access: str | None,
    s3_secret: str | None,
    s3_default_acl: str | None,
    s3_bucket_acl: str | None,
) -> StorageS3:
    """Resolve the S3 settings of `make_storage` and build the `StorageS3`.

    The ACL arguments are typed `str`: a variable may hold any string until the parsers
    check it → [VALIDATION.md](VALIDATION.md#s3-canned-acls).

    Raises:
        ValueError: If the URL path names no bucket or has an empty prefix segment, or an ACL
            setting is not a canned ACL.
        StorageException: If no access or secret key resolves.
    """
    bucket, prefix = parse_s3_url_path(url_path)
    acl_setting = _first(s3_default_acl, lambda: env_str("TXSTORE_S3_DEFAULT_ACL"), NO_ACL)
    default_acl = None if acl_setting == NO_ACL else parse_object_acl(acl_setting)
    bucket_setting = _first(s3_bucket_acl, lambda: env_str("TXSTORE_S3_BUCKET_ACL"), None)
    # Never derived from the default ACL: on a bucket even `public-read` lists every key
    # → [STORAGE.md](STORAGE.md#construction).
    bucket_acl = None if bucket_setting is None else parse_bucket_acl(bucket_setting)
    # Keys last: a malformed path or ACL stays `ValueError` whether or not they resolve
    # → [EXCEPTIONS.md](EXCEPTIONS.md#rules).
    access = _first(s3_access, lambda: env_str("TXSTORE_S3_ACCESS"), None)
    secret = _first(s3_secret, lambda: env_str("TXSTORE_S3_SECRET"), None)
    if not access or not secret:
        names = (("s3_access", access), ("s3_secret", secret))
        missing = [name for name, value in names if not value]
        raise StorageException("Missing S3 parameters: " + ", ".join(missing))
    return StorageS3(
        endpoint,
        bucket,
        access,
        secret,
        verify=verify,
        prefix=prefix,
        create_bucket=create_path,
        bucket_acl=bucket_acl,
        default_acl=default_acl,
    )


def _first[T](explicit: T | None, read_env: Callable[[], T | None], default: T) -> T:
    """Return the first of `explicit`, `read_env()`, `default` that is not None.

    The variable is read only when `explicit` is None, so an invalid value never fails a call
    that passes the argument → [CONFIG.md](CONFIG.md#resolution).
    """
    if explicit is not None:
        return explicit
    from_env = read_env()
    if from_env is not None:
        return from_env
    return default
