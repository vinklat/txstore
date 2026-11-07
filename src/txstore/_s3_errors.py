"""Shared S3 ``ClientError`` classifiers and the translation to `StorageException`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from botocore.exceptions import ClientError

from txstore._s3_client import S3_BACKEND_ERRORS
from txstore.base import StorageException, StorageNotFoundException

_S3_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
_S3_BUCKET_NOT_FOUND_CODES = frozenset({"404", "NoSuchBucket", "NotFound"})
_S3_PRECONDITION_CODES = frozenset({"412", "PreconditionFailed"})
_S3_CONFLICT_CODES = frozenset({"409", "ConditionalRequestConflict"})
_S3_METADATA_TOO_LARGE_CODES = frozenset({"MetadataTooLarge"})
_S3_ACCESS_DENIED_CODES = frozenset({"403", "AccessDenied", "Forbidden"})


def _code_and_status(exc: ClientError) -> tuple[str, int | None]:
    """Return the botocore ``Error.Code`` (``""`` when absent) and the HTTP status of *exc*."""
    code = exc.response.get("Error", {}).get("Code") or ""
    return code, exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")


def is_s3_not_found(exc: ClientError) -> bool:
    """Return True iff *exc* is an S3 not-found / missing-key error.

    Accepts the usual botocore ``Error.Code`` values and a bare HTTP 404
    when ``Code`` is empty — both shapes appear across HeadObject / GetObject /
    DeleteObject responses. The status is only consulted when ``Code`` is
    empty: a 404 carrying ``NoSuchBucket`` must not read as a missing key.

    The two conditional-write classifiers below deliberately relax that rule
    and accept the status whatever ``Code`` says. Not-found is a terminal
    verdict that makes a missing object look benign, so it stays conservative;
    a refused conditional write only decides whether the lock acquire retries,
    and no other S3 condition answers 412 or 409 on a single-key write. Biasing
    those two towards "retry" costs a wait, whereas missing a vendor-specific
    412 would abort a writer that merely lost a race.
    """
    code, http = _code_and_status(exc)
    return code in _S3_NOT_FOUND_CODES or (not code and http == 404)


def is_s3_bucket_not_found(exc: ClientError) -> bool:
    """Return True iff *exc* says the bucket itself is missing.

    The bucket counterpart of `is_s3_not_found`: ``NoSuchBucket`` counts here, and a bare
    HTTP 404 with an empty ``Code`` too, since HeadBucket has no body to carry one.
    """
    code, http = _code_and_status(exc)
    return code in _S3_BUCKET_NOT_FOUND_CODES or (not code and http == 404)


def is_s3_precondition_failed(exc: ClientError) -> bool:
    """Return True iff *exc* is a refused conditional write (HTTP 412)."""
    code, http = _code_and_status(exc)
    return code in _S3_PRECONDITION_CODES or http == 412


def is_s3_conditional_conflict(exc: ClientError) -> bool:
    """Return True iff *exc* is a concurrent-conditional-write conflict (HTTP 409).

    S3 answers a conditional write that races another conditional write on the
    same key with ``ConditionalRequestConflict`` instead of a refused
    precondition. It means "try again", not "give up".
    """
    code, http = _code_and_status(exc)
    return code in _S3_CONFLICT_CODES or http == 409


def is_s3_access_denied(exc: BaseException | None) -> bool:
    """Return True iff *exc* is an S3 refusal on permissions (HTTP 403).

    Accepts the status whatever ``Code`` says, like the conditional-write classifiers: every
    403 (``AccessDenied``, ``InvalidAccessKeyId``, ``SignatureDoesNotMatch``, …) persists past a
    retry, so the lock acquire stops instead of waiting out its timeout. Anything that is not a
    ``ClientError`` is not a refusal.
    """
    if not isinstance(exc, ClientError):
        return False
    code, http = _code_and_status(exc)
    return code in _S3_ACCESS_DENIED_CODES or http == 403


def is_s3_metadata_too_large(exc: ClientError) -> bool:
    """Return True iff *exc* refuses a metadata write over the S3 size limit."""
    code, _ = _code_and_status(exc)
    return code in _S3_METADATA_TOO_LARGE_CODES


@contextmanager
def s3_errors(
    message: str,
    *,
    on_error: Callable[[], None] | None = None,
    not_found: str | None = None,
) -> Iterator[None]:
    """Raise a boto3 failure inside the block as `StorageException`, chained.

    Args:
        message: Prefix of the raised message; the backend error follows it.
        on_error: Called before raising, e.g. to count the failure in metrics.
        not_found: When set, a not-found `ClientError` raises `StorageNotFoundException`
            with this message instead.

    Raises:
        StorageException: For a `ClientError`, `BotoCoreError`, or `OSError` in the block.
    """
    try:
        yield
    except S3_BACKEND_ERRORS as exc:
        if on_error is not None:
            on_error()
        if not_found is not None and isinstance(exc, ClientError) and is_s3_not_found(exc):
            raise StorageNotFoundException(not_found) from exc
        raise StorageException(f"{message}: {exc}") from exc
