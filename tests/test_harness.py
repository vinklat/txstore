"""Test-harness behavior: backend skips under ``--strict-backends`` and the moto bucket helper."""

from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import patch

import pytest
from helpers import backends
from helpers.s3 import client_error, s3_bucket, s3_storage


def test_backend_unavailable_skips_without_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backends, "STRICT", False)
    with pytest.raises(pytest.skip.Exception, match="no backend"):
        backends.backend_unavailable("no backend")


def test_backend_unavailable_fails_under_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catch both outcomes: a skip escaping a fail-only `pytest.raises` would read as SKIPPED."""
    monkeypatch.setattr(backends, "STRICT", True)
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as raised:
        backends.backend_unavailable("no backend")
    assert raised.type is pytest.fail.Exception
    assert "--strict-backends" in str(raised.value)


def test_set_foreign_xattr_skips_only_where_acls_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed ACL is a broken helper, not a missing backend, so it must not read as a skip."""
    monkeypatch.setattr(backends, "STRICT", False)
    target = tmp_path / "f"
    unsupported = OSError(errno.ENOTSUP, "unsupported")
    with (
        patch("helpers.backends.xattr.set", side_effect=unsupported),
        pytest.raises(pytest.skip.Exception, match="POSIX ACL"),
    ):
        backends.set_foreign_xattr(target)
    with (
        patch("helpers.backends.xattr.set", side_effect=OSError(errno.EINVAL, "bad acl")),
        pytest.raises(OSError, match="bad acl"),
    ):
        backends.set_foreign_xattr(target)


def test_s3_bucket_deletes_its_bucket_while_the_yielded_client_is_patched(
    moto_server: str,
) -> None:
    """A patch still active at fixture teardown must not skip the delete and leak the bucket."""
    bucket = s3_bucket(moto_server)
    storage = bucket.__enter__()
    missing = client_error("NoSuchBucket", 404)
    with patch.object(storage._s3_client, "bucket_head", side_effect=missing):
        bucket.__exit__(None, None, None)

    assert not s3_storage(moto_server, storage.bucket).exists_bucket()


def test_strict_backends_option_reaches_the_helper(request: pytest.FixtureRequest) -> None:
    assert backends.STRICT is bool(request.config.getoption("--strict-backends"))
