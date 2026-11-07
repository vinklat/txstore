"""Tests for txstore/_s3_errors.py: the ClientError classifiers and the `s3_errors` translation.

The not-found classifier is deliberately stricter than the two conditional-write ones:
a missing object is a terminal verdict, a refused write only means "retry"
(STORAGE.md#locking).
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from helpers.s3 import client_error

from txstore import StorageException, StorageNotFoundException
from txstore._s3_errors import (
    is_s3_access_denied,
    is_s3_bucket_not_found,
    is_s3_conditional_conflict,
    is_s3_metadata_too_large,
    is_s3_not_found,
    is_s3_precondition_failed,
    s3_errors,
)

# --- not found ----------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_not_found_accepts_every_missing_key_code(code: str) -> None:
    assert is_s3_not_found(client_error(code)) is True


def test_not_found_accepts_a_bare_404_without_a_code() -> None:
    assert is_s3_not_found(client_error(status=404)) is True


def test_not_found_rejects_a_404_carrying_another_code() -> None:
    """A missing *bucket* must not read as a missing key."""
    assert is_s3_not_found(client_error("NoSuchBucket", status=404)) is False


@pytest.mark.parametrize(
    "exc",
    [
        client_error("NoSuchBucket", status=404),
        client_error("404"),
        client_error("NotFound"),
        client_error(status=404),
    ],
    ids=["no-such-bucket", "code-404", "not-found", "bare-404"],
)
def test_bucket_not_found_accepts_every_missing_bucket_shape(exc: ClientError) -> None:
    assert is_s3_bucket_not_found(exc) is True


def test_bucket_not_found_rejects_a_missing_key_and_a_denial() -> None:
    assert is_s3_bucket_not_found(client_error("NoSuchKey", status=404)) is False
    assert is_s3_bucket_not_found(client_error("AccessDenied", status=403)) is False


def test_not_found_rejects_an_unrelated_code() -> None:
    assert is_s3_not_found(client_error("AccessDenied", status=403)) is False


def test_not_found_rejects_an_empty_response() -> None:
    assert is_s3_not_found(client_error()) is False


# --- precondition failed ------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["412", "PreconditionFailed"])
def test_precondition_accepts_every_refused_write_code(code: str) -> None:
    assert is_s3_precondition_failed(client_error(code)) is True


def test_precondition_accepts_a_412_status_whatever_the_code_says() -> None:
    """Relaxed on purpose: a vendor-specific 412 must still retry, not abort."""
    assert is_s3_precondition_failed(client_error("SomeVendorCode", status=412)) is True


def test_precondition_rejects_an_unrelated_failure() -> None:
    assert is_s3_precondition_failed(client_error("AccessDenied", status=403)) is False


# --- conditional conflict -----------------------------------------------------------------------


@pytest.mark.parametrize("code", ["409", "ConditionalRequestConflict"])
def test_conflict_accepts_every_racing_write_code(code: str) -> None:
    assert is_s3_conditional_conflict(client_error(code)) is True


def test_conflict_accepts_a_409_status_whatever_the_code_says() -> None:
    assert is_s3_conditional_conflict(client_error("SomeVendorCode", status=409)) is True


def test_conflict_rejects_an_unrelated_failure() -> None:
    assert is_s3_conditional_conflict(client_error("AccessDenied", status=403)) is False


# --- access denied ------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["403", "AccessDenied", "Forbidden"])
def test_access_denied_accepts_every_refusal_code(code: str) -> None:
    assert is_s3_access_denied(client_error(code)) is True


def test_access_denied_accepts_a_403_status_whatever_the_code_says() -> None:
    assert is_s3_access_denied(client_error("SignatureDoesNotMatch", status=403)) is True


def test_access_denied_rejects_an_unrelated_failure() -> None:
    assert is_s3_access_denied(client_error("SlowDown", status=503)) is False


def test_access_denied_rejects_what_is_not_a_client_error() -> None:
    assert is_s3_access_denied(None) is False
    assert is_s3_access_denied(OSError("connection reset")) is False


@pytest.mark.parametrize(
    ("code", "expected"),
    [("MetadataTooLarge", True), ("AccessDenied", False), ("", False)],
    ids=["too-large", "other-code", "no-code"],
)
def test_metadata_too_large_matches_only_its_code(code: str, expected: bool) -> None:
    assert is_s3_metadata_too_large(client_error(code, status=400)) is expected


def test_the_classifiers_are_mutually_exclusive_for_a_missing_key() -> None:
    missing = client_error("NoSuchKey", status=404)

    assert (
        is_s3_not_found(missing),
        is_s3_precondition_failed(missing),
        is_s3_conditional_conflict(missing),
        is_s3_access_denied(missing),
    ) == (True, False, False, False)


# --- s3_errors ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(client_error("AccessDenied", 403), id="client-error"),
        pytest.param(EndpointConnectionError(endpoint_url="http://s3"), id="botocore-error"),
        pytest.param(OSError("reset"), id="os-error"),
    ],
)
def test_s3_errors_raises_a_chained_storage_exception(error: Exception) -> None:
    failures: list[int] = []
    with (
        pytest.raises(StorageException, match="^Error reading k: ") as exc_info,
        s3_errors("Error reading k", on_error=lambda: failures.append(1)),
    ):
        raise error

    assert exc_info.value.__cause__ is error
    assert type(exc_info.value) is StorageException
    assert failures == [1]


def test_s3_errors_maps_not_found_when_asked() -> None:
    with (
        pytest.raises(StorageNotFoundException, match="^missing k$"),
        s3_errors("Error reading k", not_found="missing k"),
    ):
        raise client_error("NoSuchKey", 404)


def test_s3_errors_keeps_not_found_generic_unless_asked() -> None:
    with pytest.raises(StorageException) as exc_info, s3_errors("Error reading k"):
        raise client_error("NoSuchKey", 404)
    assert type(exc_info.value) is StorageException


def test_s3_errors_lets_other_exceptions_through() -> None:
    with pytest.raises(KeyError), s3_errors("Error reading k"):
        raise KeyError("not a backend failure")
