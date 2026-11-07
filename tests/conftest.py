"""Shared pytest fixtures. Harness rules → TESTING.md."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from helpers import backends
from helpers.backends import backend_unavailable
from helpers.s3 import s3_bucket

from txstore import StorageS3

# Variables that change `make_storage` / `HistoryRetention.from_env` resolution.
_TX_STORE_VARS = (
    "TX_STORE_VERIFY_SSL",
    "TX_STORE_S3_ACCESS",
    "TX_STORE_S3_SECRET",
    "TX_STORE_S3_DEFAULT_ACL",
    "TX_STORE_S3_BUCKET_ACL",
    "TX_STORE_HISTORY_KEEP",
    "TX_STORE_HISTORY_MAX_AGE_DAYS",
)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register ``--strict-backends``: a missing backend leg fails instead of skipping."""
    parser.addoption(
        "--strict-backends",
        action="store_true",
        help="fail, rather than skip, a test whose backend (moto server, xattrs) is missing",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Hand the ``--strict-backends`` flag to the skip helper."""
    backends.STRICT = bool(config.getoption("--strict-backends"))


@pytest.fixture(autouse=True)
def _isolate_tx_store_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no ``TX_STORE_*`` variable set, whatever the caller's shell holds."""
    for name in _TX_STORE_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def moto_server() -> Iterator[str]:
    """Spin up a single in-process ``moto`` server for the whole session.

    Yields:
        Endpoint URL (``http://127.0.0.1:<port>``) suitable for passing to
        ``StorageS3`` as its ``endpoint`` argument.
    """
    try:
        import moto.server as moto_server_module
    except ImportError:
        backend_unavailable("moto.server is not installed")
    # Proxy env vars would otherwise force boto3 through an outbound proxy
    # and never reach the in-process server.
    proxy_keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    saved = {k: os.environ.pop(k, None) for k in proxy_keys}
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    server = moto_server_module.ThreadedMotoServer(port=0, verbose=False)
    server.start()
    assert server._server is not None
    port = server._server.socket.getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


@pytest.fixture
def storage_public_verify(moto_server: str) -> Iterator[StorageS3]:
    """A fresh bucket with a public default ACL and the post-PUT public probe on."""
    with s3_bucket(
        moto_server, bucket_acl="public-read", default_acl="public-read", verify_public_acl=True
    ) as s:
        yield s
