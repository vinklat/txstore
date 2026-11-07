"""Unit tests for the `is_public` probe's retry and propagation behaviour.

Uses a stub S3 client (no real S3 / moto) to simulate transient
backend failures.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import EndpointConnectionError
from helpers.s3 import client_error

from txstore import StorageException
from txstore._s3_acl import is_public

# A minimal AllUsers-READ grant (`grants_all_users_read` is covered in test_s3_client.py).
_PUBLIC_GRANT = [
    {
        "Grantee": {
            "Type": "Group",
            "URI": "http://acs.amazonaws.com/groups/global/AllUsers",
        },
        "Permission": "READ",
    }
]


def _stub_client(get_acl_side_effect: list[object]) -> MagicMock:
    """A stub S3 client whose ``object_get_acl`` fires the given side-effect sequence."""
    client: MagicMock = MagicMock()
    client.object_get_acl.side_effect = get_acl_side_effect
    return client


# --------------------------------------------------------------------------- #
# is_public — retry mechanics
# --------------------------------------------------------------------------- #


def test_is_public_succeeds_on_first_attempt() -> None:
    client = _stub_client([_PUBLIC_GRANT])
    assert is_public(client, "bucket", "bucket/k") is True
    assert client.object_get_acl.call_count == 1


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(client_error("InternalError", 500), id="client-error"),
        pytest.param(EndpointConnectionError(endpoint_url="http://s3"), id="botocore-error"),
        pytest.param(ConnectionResetError("reset"), id="os-error"),
    ],
)
def test_is_public_retries_on_transient_error_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Each backend error class the probe catches is retried; True once a call succeeds."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    client = _stub_client([error, _PUBLIC_GRANT])
    assert is_public(client, "bucket", "bucket/k") is True
    assert client.object_get_acl.call_count == 2
    assert slept == [0.5]


def test_is_public_raises_storage_exception_after_all_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three tries, sleeping 0.5 s then 1 s, then a chained StorageException → STORAGE.md."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    cause = OSError("boom")
    client = _stub_client([OSError("x"), OSError("y"), cause])
    with pytest.raises(StorageException) as exc_info:
        is_public(client, "bucket", "bucket/k")
    assert exc_info.value.__cause__ is cause
    assert client.object_get_acl.call_count == 3
    assert slept == [0.5, 1.0]


def test_is_public_retries_exactly_attempts_times_on_persistent_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly three calls are made, no more."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    client = _stub_client([OSError("x")] * 8)
    with pytest.raises(StorageException):
        is_public(client, "bucket", "bucket/k")
    assert client.object_get_acl.call_count == 3


def test_is_public_does_not_retry_a_programming_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only backend errors are transient; a bug surfaces at once with its own type."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    client = _stub_client([TypeError("bug")])
    with pytest.raises(TypeError):
        is_public(client, "bucket", "bucket/k")
    assert client.object_get_acl.call_count == 1
    assert slept == []


def test_is_public_private_object_returns_false() -> None:
    client = _stub_client([[]])
    assert is_public(client, "bucket", "bucket/k") is False
