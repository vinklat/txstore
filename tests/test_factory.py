"""`txstore.factory`: `make_storage()` URL dispatch and setting resolution."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from txstore import NO_ACL, StorageException, StorageFS, StorageHTTP, make_storage

_S3_URL = "s3://endpoint/bucket"
_CREDS: dict[str, Any] = {"s3_access": "k", "s3_secret": "s"}


def _s3_init_kwargs(**kwargs: Any) -> dict[str, Any]:
    """Run `make_storage` on an S3 URL and return the keyword arguments `StorageS3` got."""
    with patch("txstore.factory.StorageS3.__init__", return_value=None) as init:
        make_storage(_S3_URL, **kwargs)
    return dict(init.call_args.kwargs)


def _s3_init_args(url: str = _S3_URL, **kwargs: Any) -> tuple[Any, ...]:
    """Run `make_storage` on *url* and return the positional arguments `StorageS3` got."""
    with patch("txstore.factory.StorageS3.__init__", return_value=None) as init:
        make_storage(url, **kwargs)
    return tuple(init.call_args.args)


def test_make_storage_fs() -> None:
    assert isinstance(make_storage("fs:///tmp/repo"), StorageFS)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://example.com/debian", id="plain"),
        pytest.param("https://example.com/debian", id="tls"),
        pytest.param("https://[::1]:8080/x", id="ipv6-port"),
        pytest.param("https://u:p@host.example:8080/x", id="userinfo-host-port"),
    ],
)
def test_make_storage_http(url: str) -> None:
    assert isinstance(make_storage(url), StorageHTTP)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http:///no-host", id="empty"),
        pytest.param("http://:80/x", id="port-only"),
        pytest.param("http://u@:80/x", id="userinfo-and-port"),
        pytest.param("http://u:hunter2@/x", id="userinfo-only"),
    ],
)
def test_make_storage_rejects_http_without_host(url: str) -> None:
    with pytest.raises(StorageException, match="Invalid HTTP URL"):
        make_storage(url)


@pytest.mark.parametrize("url", ["s3:///bucket", "s3://:9000/bucket"], ids=["empty", "port-only"])
def test_make_storage_rejects_s3_without_host(url: str) -> None:
    with pytest.raises(StorageException, match="Invalid S3 endpoint"):
        make_storage(url, **_CREDS)


@pytest.mark.parametrize(
    ("url", "kwargs", "match"),
    [
        pytest.param("s3://endpoint/", {}, "bucket name is not specified", id="no-bucket"),
        pytest.param("s3://endpoint/bucket//x", {}, "empty segment", id="empty-prefix-segment"),
        pytest.param(_S3_URL, {"s3_default_acl": "bogus"}, "object ACL", id="bad-object-acl"),
        pytest.param(_S3_URL, {"s3_bucket_acl": "bogus"}, "bucket ACL", id="bad-bucket-acl"),
    ],
)
def test_make_storage_s3_refuses_a_bad_setting_before_missing_keys(
    url: str, kwargs: dict[str, Any], match: str
) -> None:
    """A malformed path or ACL stays `ValueError` whether or not keys resolve → EXCEPTIONS.md."""
    with pytest.raises(ValueError, match=match):
        make_storage(url, **kwargs)


def test_make_storage_empty_url() -> None:
    with pytest.raises(StorageException, match="Empty storage URL"):
        make_storage("")


def test_make_storage_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="ftp"):
        make_storage("ftp://example.com/foo")


def test_make_storage_fs_without_create_path_does_not_create_dir(tmp_path: Path) -> None:
    """A read-only caller must not materialise a mistyped path."""
    target = tmp_path / "missing"
    assert isinstance(make_storage(f"fs://{target}"), StorageFS)
    assert not target.exists()


def test_make_storage_fs_with_create_path_creates_dir(tmp_path: Path) -> None:
    target = tmp_path / "missing"
    make_storage(f"fs://{target}", create_path=True)
    assert target.is_dir()


# S3 credentials


def test_make_storage_s3_credentials_from_arguments() -> None:
    assert _s3_init_args(**_CREDS)[2:4] == ("k", "s")


def test_make_storage_s3_credentials_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TXSTORE_S3_ACCESS", "env-k")
    monkeypatch.setenv("TXSTORE_S3_SECRET", "env-s")
    assert _s3_init_args()[2:4] == ("env-k", "env-s")


def test_make_storage_s3_argument_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TXSTORE_S3_ACCESS", "env-k")
    monkeypatch.setenv("TXSTORE_S3_SECRET", "env-s")
    assert _s3_init_args(**_CREDS)[2:4] == ("k", "s")


@pytest.mark.parametrize(
    ("kwargs", "missing"),
    [
        pytest.param({}, "s3_access, s3_secret", id="both"),
        pytest.param({"s3_access": "k"}, "s3_secret", id="secret"),
        pytest.param({"s3_secret": "s"}, "s3_access", id="access"),
    ],
)
def test_make_storage_s3_missing_credentials(kwargs: dict[str, Any], missing: str) -> None:
    with pytest.raises(StorageException, match=f"Missing S3 parameters: {missing}$"):
        make_storage(_S3_URL, **kwargs)


@pytest.mark.parametrize("name", ["s3_access", "s3_secret"])
def test_make_storage_s3_empty_credential_argument_is_missing_not_unset(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only ``None`` counts as not given (CONFIG.md#resolution); an empty key is never usable."""
    monkeypatch.setenv("TXSTORE_S3_ACCESS", "env-k")
    monkeypatch.setenv("TXSTORE_S3_SECRET", "env-s")
    with pytest.raises(StorageException, match=f"Missing S3 parameters: {name}$"):
        make_storage(_S3_URL, **{**_CREDS, name: ""})


def test_make_storage_s3_empty_bucket_acl_argument_is_not_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``""`` wins over the variable and is rejected like any invalid value."""
    monkeypatch.setenv("TXSTORE_S3_BUCKET_ACL", "private")
    with pytest.raises(ValueError, match="invalid S3 bucket ACL"):
        make_storage(_S3_URL, **_CREDS, s3_bucket_acl="")  # type: ignore[arg-type]


def test_make_storage_s3_ignores_unprefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only ``TXSTORE_*`` names are read; a generic ``S3_ACCESS`` is not a fallback."""
    monkeypatch.setenv("S3_ACCESS", "k")
    monkeypatch.setenv("S3_SECRET", "s")
    with pytest.raises(StorageException, match="Missing S3 parameters"):
        make_storage(_S3_URL)


# S3 ACLs


def test_make_storage_s3_default_acl_is_no_acl() -> None:
    """Nothing becomes public unless the caller asks for it."""
    kwargs = _s3_init_kwargs(**_CREDS)
    assert kwargs["default_acl"] is None
    assert kwargs["bucket_acl"] is None


def test_make_storage_s3_default_acl_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TXSTORE_S3_DEFAULT_ACL", "private")
    kwargs = _s3_init_kwargs(**_CREDS)
    assert kwargs["default_acl"] == "private"
    assert kwargs["bucket_acl"] is None


def test_make_storage_s3_default_acl_argument_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TXSTORE_S3_DEFAULT_ACL", "private")
    assert _s3_init_kwargs(**_CREDS, s3_default_acl="authenticated-read")["default_acl"] == (
        "authenticated-read"
    )


@pytest.mark.parametrize("source", ["argument", "env"])
def test_make_storage_s3_no_acl_sends_no_acl(source: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`NO_ACL` sends no ACL on writes or on bucket creation."""
    if source == "env":
        monkeypatch.setenv("TXSTORE_S3_DEFAULT_ACL", NO_ACL)
        kwargs = _s3_init_kwargs(**_CREDS)
    else:
        kwargs = _s3_init_kwargs(**_CREDS, s3_default_acl=NO_ACL)
    assert kwargs["default_acl"] is None
    assert kwargs["bucket_acl"] is None


@pytest.mark.parametrize("setting", [NO_ACL, "private", "public-read"])
def test_make_storage_s3_leaves_the_public_probe_to_the_constructor(setting: str) -> None:
    """Both construction paths probe every write whose effective ACL is public."""
    assert "verify_public_acl" not in _s3_init_kwargs(**_CREDS, s3_default_acl=setting)


@pytest.mark.parametrize(
    "default_acl", ["private", "public-read", "authenticated-read", "public-read-write"]
)
def test_make_storage_s3_default_acl_never_becomes_the_bucket_acl(default_acl: str) -> None:
    """On a bucket even public-read lists every key; only an explicit bucket ACL sets one."""
    kwargs = _s3_init_kwargs(**_CREDS, s3_default_acl=default_acl)
    assert kwargs["default_acl"] == default_acl
    assert kwargs["bucket_acl"] is None
    explicit = _s3_init_kwargs(**_CREDS, s3_default_acl=default_acl, s3_bucket_acl="public-read")
    assert explicit["bucket_acl"] == "public-read"


@pytest.mark.parametrize("source", ["argument", "env"])
def test_make_storage_s3_bucket_acl_from_argument_or_env(
    source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if source == "env":
        monkeypatch.setenv("TXSTORE_S3_BUCKET_ACL", "private")
        kwargs = _s3_init_kwargs(**_CREDS)
    else:
        kwargs = _s3_init_kwargs(**_CREDS, s3_bucket_acl="private")
    assert kwargs["default_acl"] is None
    assert kwargs["bucket_acl"] == "private"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        pytest.param("TXSTORE_S3_DEFAULT_ACL", "world", id="default-acl"),
        pytest.param("TXSTORE_S3_BUCKET_ACL", "aws-exec-read", id="bucket-acl-object-only"),
    ],
)
def test_make_storage_s3_invalid_acl_env(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=repr(value)):
        make_storage(_S3_URL, **_CREDS)


# TLS verification


@pytest.mark.parametrize(
    ("argument", "env", "expected"),
    [
        pytest.param(None, None, True, id="default"),
        pytest.param(None, "false", False, id="env-false"),
        pytest.param(None, "1", True, id="env-true"),
        pytest.param(True, "off", True, id="argument-wins"),
        pytest.param(True, "maybe", True, id="argument-skips-invalid-env"),
        pytest.param(False, None, False, id="argument"),
    ],
)
def test_make_storage_verify_ssl_resolution(
    argument: bool | None, env: str | None, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if env is not None:
        monkeypatch.setenv("TXSTORE_VERIFY_SSL", env)
    assert _s3_init_kwargs(**_CREDS, verify_ssl=argument)["verify"] is expected
    with patch("txstore.factory.StorageHTTP", MagicMock()) as http:
        make_storage("https://example.com/debian", verify_ssl=argument)
    assert http.call_args.kwargs["verify"] is expected


@pytest.mark.parametrize("url", ["https://u:secret@example.com/debian", "s3://endpoint/bucket"])
def test_make_storage_warns_when_tls_verification_is_off(
    url: str, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch("txstore.factory.StorageS3.__init__", return_value=None),
        caplog.at_level(logging.DEBUG, logger="txstore"),
    ):
        make_storage(url, verify_ssl=False, **_CREDS)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "verification is disabled" in warnings[0].getMessage()
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    "url", ["https:u:hunter2@example.com/x", "https://u:hunter2\t@example.com/x"]
)
def test_make_storage_refuses_userinfo_urllib_does_not_see_without_logging_it(
    url: str, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level(logging.DEBUG, logger="txstore"),
        pytest.raises(ValueError, match="URL holds"),
    ):
        make_storage(url, verify_ssl=False)
    assert "hunter2" not in caplog.text


@pytest.mark.parametrize(
    ("url", "warned"),
    [
        pytest.param("http://u:secret@h.example/base/", True, id="http-userinfo"),
        pytest.param("https://u:secret@h.example/base/", False, id="https-userinfo"),
        pytest.param("http://h.example/base/", False, id="http-anonymous"),
    ],
)
def test_make_storage_warns_on_credentials_over_plain_http(
    url: str, warned: bool, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="txstore"):
        make_storage(url)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("unencrypted over plain http" in m for m in warnings) is warned
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("s3://u:hunter2@endpoint:9000/bucket", id="userinfo"),
        pytest.param("s3://AKIDLEAK:/hunter2@host/bucket", id="empty-port-split"),
        pytest.param("s3://AKIDLEAK:12/hunter2@host/bucket", id="numeric-port-split"),
        pytest.param("s3://AKID:/se/hunter2@host/bucket", id="multi-slash-split"),
        pytest.param("s3://tok/hunter2@host/bucket", id="token-only"),
        # The password's tail lands in the prefix, past any authority or bucket check: only
        # the whole-URL refusal catches these.
        pytest.param("s3://AKIDLEAK:12/se/hunter2@host/bucket", id="numeric-port-prefix-split"),
        pytest.param("s3://tok/se/hunter2@host/bucket", id="token-only-prefix-split"),
    ],
)
def test_make_storage_s3_refuses_url_credentials(
    url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """S3 keys come from settings, so any `@` is a credential that would reach logs and repr."""
    with (
        caplog.at_level(logging.DEBUG, logger="txstore"),
        pytest.raises(ValueError, match="S3 keys come from|authority does not parse") as raised,
    ):
        make_storage(url, **_CREDS)
    for secret in ("hunter2", "AKIDLEAK"):
        assert secret not in str(raised.value)
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("fs://u:hunter2@h/b", id="userinfo"),
        pytest.param("fs://u:12/hunter2@h/b", id="split-password"),
        pytest.param("fs://host/b", id="host"),
    ],
)
def test_make_storage_fs_refuses_an_authority(url: str, caplog: pytest.LogCaptureFixture) -> None:
    """A local root has no host; an authority would land in the path, `repr`, and logs."""
    with (
        caplog.at_level(logging.DEBUG, logger="txstore"),
        pytest.raises(ValueError, match="names an authority") as raised,
    ):
        make_storage(url)
    assert "hunter2" not in str(raised.value)
    assert "hunter2" not in caplog.text


def test_make_storage_does_not_warn_when_tls_verification_is_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="txstore.factory"):
        make_storage("https://example.com/debian")
    assert not caplog.records


def test_make_storage_invalid_verify_ssl_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TXSTORE_VERIFY_SSL", "maybe")
    with pytest.raises(ValueError, match="TXSTORE_VERIFY_SSL"):
        make_storage("fs:///tmp/repo")
