"""`txstore._env`: ``TX_STORE_*`` variable reading and value grammar."""

from __future__ import annotations

import logging

import pytest

from txstore import make_storage
from txstore._env import env_bool, env_int, env_str


def test_env_str_unset_and_empty_are_none(monkeypatch: pytest.MonkeyPatch) -> None:
    assert env_str("TX_STORE_S3_ACCESS") is None
    monkeypatch.setenv("TX_STORE_S3_ACCESS", "")
    assert env_str("TX_STORE_S3_ACCESS") is None
    monkeypatch.setenv("TX_STORE_S3_ACCESS", "key")
    assert env_str("TX_STORE_S3_ACCESS") == "key"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        *[
            pytest.param(v, True, id=f"true-{v}")
            for v in ("1", "true", "TRUE", "Yes", "on", " on ")
        ],
        *[pytest.param(v, False, id=f"false-{v}") for v in ("0", "false", "False", "NO", "off")],
    ],
)
def test_env_bool_vocabulary(raw: str, expected: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TX_STORE_VERIFY_SSL", raw)
    assert env_bool("TX_STORE_VERIFY_SSL") is expected


@pytest.mark.parametrize("raw", ["2", "y", "enable", "truee"])
def test_env_bool_rejects_other_values(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TX_STORE_VERIFY_SSL", raw)
    with pytest.raises(ValueError, match="^TX_STORE_VERIFY_SSL: expected a boolean"):
        env_bool("TX_STORE_VERIFY_SSL")


def test_env_bool_unset_is_none() -> None:
    assert env_bool("TX_STORE_VERIFY_SSL") is None


@pytest.mark.parametrize(
    ("raw", "expected"), [("7", 7), (" 12 ", 12), ("-1", -1), ("+5", 5), ("0", 0)]
)
def test_env_int_parses_decimal(raw: str, expected: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TX_STORE_HISTORY_KEEP", raw)
    assert env_int("TX_STORE_HISTORY_KEEP") == expected


@pytest.mark.parametrize("raw", ["seven", "1.5", "0x10", "1_0", "١٢", "１２", "+-5"])
def test_env_int_rejects_non_integers(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TX_STORE_HISTORY_KEEP", raw)
    with pytest.raises(ValueError, match="^TX_STORE_HISTORY_KEEP: expected an integer"):
        env_int("TX_STORE_HISTORY_KEEP")


def test_env_helpers_refuse_unprefixed_names() -> None:
    with pytest.raises(ValueError, match="TX_STORE_ prefix"):
        env_str("S3_ACCESS")


def test_s3_secrets_from_env_are_never_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("TX_STORE_S3_ACCESS", "access-sentinel-123")
    monkeypatch.setenv("TX_STORE_S3_SECRET", "secret-sentinel-456")
    caplog.set_level(logging.DEBUG)
    make_storage("s3://127.0.0.1:9/bucket")
    assert "access-sentinel-123" not in caplog.text
    assert "secret-sentinel-456" not in caplog.text
