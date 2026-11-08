"""Contract test for `StorageHTTP` (read-only HTTP backend).

Uses the `responses` library (installed transitively as a moto dep) to
mock `requests` HTTP calls; no real network.
"""

from __future__ import annotations

import base64
import gzip
import logging
import traceback
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
import responses

from txstore import (
    REQUEST_TIMEOUT,
    StorageException,
    StorageHTTP,
    StorageNotFoundException,
    StorageType,
    make_storage,
)


@pytest.fixture
def storage() -> StorageHTTP:
    return StorageHTTP("http://repo.example.com", verify=False)


# --------------------------------------------------------------------------- #
# Type + repr
# --------------------------------------------------------------------------- #


def test_storage_http_get_type(storage: StorageHTTP) -> None:
    assert storage.get_type() is StorageType.http


def test_storage_http_repr_contains_base_url(storage: StorageHTTP) -> None:
    assert "http://repo.example.com" in repr(storage)


# --------------------------------------------------------------------------- #
# get / get_stream / get_metadata / exists
# --------------------------------------------------------------------------- #


@responses.activate
def test_storage_http_get_returns_body(storage: StorageHTTP) -> None:
    responses.add(
        responses.GET,
        "http://repo.example.com/Release",
        body=b"Origin: Debian\n",
        status=200,
    )
    assert storage.get("Release") == b"Origin: Debian\n"
    assert storage.metrics.read_objects_total == 1
    assert storage.metrics.read_bytes == len(b"Origin: Debian\n")


@pytest.mark.parametrize("verify", [True, False])
@responses.activate
def test_storage_http_passes_verify_to_requests(verify: bool) -> None:
    """The fixture runs with verification off, so only this pins the setting to the request."""
    responses.add(responses.GET, "https://repo.example.com/Release", body=b"x", status=200)
    storage = StorageHTTP("https://repo.example.com", verify=verify)
    with patch("txstore.storage_http.requests.request", wraps=requests.request) as request:
        storage.get("Release")
    assert request.call_args.kwargs["verify"] is verify


@pytest.mark.parametrize(
    ("method", "verb"),
    [
        ("get", responses.GET),
        ("get_stream", responses.GET),
        ("get_metadata", responses.HEAD),
        ("exists", responses.HEAD),
    ],
)
@responses.activate
def test_storage_http_request_carries_the_timeout(
    storage: StorageHTTP, method: str, verb: str
) -> None:
    """A stalled server cannot hang a read → STORAGE.md#storage-abi."""
    responses.add(verb, "http://repo.example.com/Release", status=200)
    with patch("txstore.storage_http.requests.request", wraps=requests.request) as request:
        getattr(storage, method)("Release")
    assert request.call_args.kwargs["timeout"] == REQUEST_TIMEOUT


@responses.activate
def test_storage_http_sends_netrc_credentials_for_its_host(
    storage: StorageHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client honours the standard environment → CONFIG.md#secrets."""
    netrc = tmp_path / "netrc"
    netrc.write_text("machine repo.example.com login user password pass\n")
    monkeypatch.setenv("NETRC", str(netrc))
    responses.add(responses.GET, "http://repo.example.com/Release", body=b"x", status=200)

    storage.get("Release")

    assert responses.calls[0].request.headers["Authorization"] == "Basic dXNlcjpwYXNz"


@responses.activate
@pytest.mark.parametrize(("scheme", "warned"), [("http", 1), ("HTTP", 1), ("https", 0)])
def test_storage_http_netrc_over_plain_http_warns_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    scheme: str,
    warned: int,
) -> None:
    """netrc credentials over `http://` travel in cleartext, like userinfo → CONFIG.md#secrets."""
    netrc = tmp_path / "netrc"
    netrc.write_text("machine repo.example.com login user password s3cr3t-netrc\n")
    monkeypatch.setenv("NETRC", str(netrc))
    url = f"{scheme.lower()}://repo.example.com/Release"
    responses.add(responses.GET, url, body=b"x", status=200)
    storage = StorageHTTP(f"{scheme}://repo.example.com")

    with caplog.at_level(logging.DEBUG, logger="txstore"):
        storage.get("Release")
        storage.get("Release")

    warnings = [r for r in caplog.records if "unencrypted" in r.getMessage()]
    assert len(warnings) == warned
    assert "s3cr3t-netrc" not in caplog.text
    assert base64.b64encode(b"user:s3cr3t-netrc").decode() not in caplog.text


@pytest.mark.parametrize(
    ("url", "warned"),
    [
        pytest.param("http://u:s3cr3t-url@h.example/base/", True, id="http-userinfo"),
        pytest.param("HTTP://u:s3cr3t-url@h.example/base/", True, id="upper-http-userinfo"),
        pytest.param("http://@h.example/base/", True, id="http-empty-userinfo"),
        pytest.param("https://u:s3cr3t-url@h.example/base/", False, id="https-userinfo"),
        pytest.param("http://h.example/base/", False, id="http-anonymous"),
    ],
)
def test_storage_http_warns_on_url_credentials_over_plain_http(
    url: str, warned: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """Built directly, not only through `make_storage` → CONFIG.md#secrets."""
    with caplog.at_level(logging.DEBUG, logger="txstore"):
        StorageHTTP(url)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("unencrypted over plain http" in m for m in warnings) is warned
    assert "s3cr3t-url" not in caplog.text


@responses.activate
def test_storage_http_get_5xx_raises(storage: StorageHTTP) -> None:
    responses.add(responses.GET, "http://repo.example.com/oops", status=503)
    with pytest.raises(StorageException):
        storage.get("oops")
    assert storage.metrics.read_errors_total == 1


@responses.activate
def test_storage_http_get_404_raises_not_found(storage: StorageHTTP) -> None:
    responses.add(responses.GET, "http://repo.example.com/missing", status=404)
    with pytest.raises(StorageNotFoundException, match="not found"):
        storage.get("missing")


@responses.activate
def test_storage_http_get_stream_404_raises_not_found(storage: StorageHTTP) -> None:
    responses.add(responses.GET, "http://repo.example.com/missing", status=404)
    with pytest.raises(StorageNotFoundException, match="not found"):
        storage.get_stream("missing")


@responses.activate
def test_storage_http_get_stream_403_raises_plain_storage_exception(storage: StorageHTTP) -> None:
    """`get_stream` has no empty-stream sentinel, so every client error raises."""
    responses.add(responses.GET, "http://repo.example.com/denied", status=403)
    with pytest.raises(StorageException, match="status 403") as excinfo:
        storage.get_stream("denied")
    assert not isinstance(excinfo.value, StorageNotFoundException)
    assert storage.metrics.read_errors_total == 1


@responses.activate
def test_storage_http_get_403_raises_plain_storage_exception(storage: StorageHTTP) -> None:
    """A non-404 client error must stay a generic StorageException, not not-found."""
    responses.add(responses.GET, "http://repo.example.com/denied", status=403)
    with pytest.raises(StorageException) as excinfo:
        storage.get("denied")
    assert not isinstance(excinfo.value, StorageNotFoundException)


@responses.activate
def test_storage_http_exists_true(storage: StorageHTTP) -> None:
    responses.add(responses.HEAD, "http://repo.example.com/file", status=200)
    assert storage.exists("file") is True


@responses.activate
def test_storage_http_exists_false_on_404(storage: StorageHTTP) -> None:
    responses.add(responses.HEAD, "http://repo.example.com/missing", status=404)
    assert storage.exists("missing") is False


@responses.activate
@pytest.mark.parametrize("status", [403, 405, 429, 503])
def test_storage_http_exists_raises_on_a_non_404_error(storage: StorageHTTP, status: int) -> None:
    """Only "not found" answers False; anything else is a backend error.

    A HEAD the server refuses says nothing about publication, and reading it
    as absence drops an object the source still serves over GET.
    """
    responses.add(responses.HEAD, "http://repo.example.com/denied", status=status)
    with pytest.raises(StorageException):
        storage.exists("denied")


@responses.activate
def test_storage_http_get_metadata_404_raises_not_found(storage: StorageHTTP) -> None:
    responses.add(responses.HEAD, "http://repo.example.com/missing", status=404)
    with pytest.raises(StorageNotFoundException, match="not found"):
        storage.get_metadata("missing")


@responses.activate
def test_storage_http_exists_true_for_any_2xx(storage: StorageHTTP) -> None:
    responses.add(responses.HEAD, "http://repo.example.com/file", status=204)
    assert storage.exists("file") is True


@responses.activate
def test_storage_http_get_stream_5xx_raises(storage: StorageHTTP) -> None:
    responses.add(responses.GET, "http://repo.example.com/oops", status=503)
    with pytest.raises(StorageException, match="status 503"):
        storage.get_stream("oops")


@responses.activate
def test_storage_http_get_metadata_returns_headers(storage: StorageHTTP) -> None:
    responses.add(
        responses.HEAD,
        "http://repo.example.com/file",
        status=200,
        headers={"Content-Length": "42", "ETag": "abc"},
    )
    md = storage.get_metadata("file")
    assert md["http_status_code"] == 200
    assert md["Content-Length"] == "42"
    assert md["ETag"] == "abc"


@responses.activate
def test_storage_http_get_metadata_drops_headers_outside_the_allowlist(
    storage: StorageHTTP,
) -> None:
    responses.add(
        responses.HEAD,
        "http://repo.example.com/file",
        status=200,
        headers={"Set-Cookie": "session=abc", "Server": "nginx", "Content-Type": "text/plain"},
    )
    assert storage.get_metadata("file") == {
        "http_status_code": 200,
        "Content-Type": "text/plain",
    }


@responses.activate
def test_storage_http_get_stream_returns_raw(storage: StorageHTTP) -> None:
    responses.add(
        responses.GET,
        "http://repo.example.com/big",
        body=b"chunk1chunk2",
        status=200,
    )
    stream = storage.get_stream("big")
    assert stream.read() == b"chunk1chunk2"


@responses.activate
def test_storage_http_get_stream_decodes_a_gzip_transfer(storage: StorageHTTP) -> None:
    """A gzip `Content-Encoding` is transport, so the stream yields the object's own bytes."""
    body = b"x" * 5000
    responses.add(
        responses.GET,
        "http://repo.example.com/gz",
        body=gzip.compress(body),
        headers={"Content-Encoding": "gzip"},
    )
    with storage.get_stream("gz") as stream:
        assert stream.read() == body


@responses.activate
def test_storage_http_refuses_a_key_with_a_raw_query_before_any_request(
    storage: StorageHTTP,
) -> None:
    with pytest.raises(StorageException, match="query or fragment"):
        storage.get("a?x=1")
    assert len(responses.calls) == 0


@responses.activate
def test_storage_http_get_connection_error_raises_storage_exception(
    storage: StorageHTTP,
) -> None:
    responses.add(
        responses.GET,
        "http://repo.example.com/down",
        body=requests.exceptions.ConnectionError("refused"),
    )
    with pytest.raises(StorageException, match="failed: ConnectionError"):
        storage.get("down")
    assert storage.metrics.read_errors_total == 1


# --------------------------------------------------------------------------- #
# Redirects are refused, never followed
# --------------------------------------------------------------------------- #


_READ_METHODS = pytest.mark.parametrize(
    ("method", "verb"),
    [
        ("get", responses.GET),
        ("get_stream", responses.GET),
        ("get_metadata", responses.HEAD),
        ("exists", responses.HEAD),
    ],
)


@responses.activate
@_READ_METHODS
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_storage_http_redirect_raises_without_following(
    storage: StorageHTTP, method: str, verb: str, status: int
) -> None:
    responses.add(
        verb,
        "http://repo.example.com/Release",
        status=status,
        headers={"Location": "http://169.254.169.254/latest/meta-data/"},
    )
    target = responses.add(verb, "http://169.254.169.254/latest/meta-data/", body=b"secret")
    with pytest.raises(StorageException, match=f"redirect {status} not followed"):
        getattr(storage, method)("Release")
    assert target.call_count == 0


@responses.activate
@_READ_METHODS
@pytest.mark.parametrize("status", [300, 304, 302])
def test_storage_http_3xx_without_location_raises(
    storage: StorageHTTP, method: str, verb: str, status: int
) -> None:
    """Every 3xx is refused, not only what `requests` calls a redirect → VALIDATION.md."""
    responses.add(verb, "http://repo.example.com/Release", status=status)
    with pytest.raises(StorageException, match=f"redirect {status} not followed"):
        getattr(storage, method)("Release")


# --------------------------------------------------------------------------- #
# Credentials in the base URL never reach repr, logs, or messages
# --------------------------------------------------------------------------- #

_SECRET_URL = "https://mirror:hunter2@repo.example.com/debian"


def test_storage_http_repr_redacts_credentials() -> None:
    text = repr(StorageHTTP(_SECRET_URL))
    assert "hunter2" not in text
    assert "***@repo.example.com/debian" in text


@pytest.mark.parametrize(
    "base_url", ["https://u:hunter2\n@repo.example.com/debian", "https:u:hunter2@repo.example.com/"]
)
def test_storage_http_refuses_userinfo_urllib_does_not_see(
    base_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG), pytest.raises(ValueError, match="URL holds") as excinfo:
        StorageHTTP(base_url)
    assert "hunter2" not in "".join(traceback.format_exception(excinfo.value))
    assert "hunter2" not in caplog.text


@responses.activate
@pytest.mark.parametrize("status", [403, 503])
def test_storage_http_error_message_redacts_credentials(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    responses.add(responses.GET, "https://mirror:hunter2@repo.example.com/debian/f", status=status)
    with caplog.at_level(logging.DEBUG, logger="txstore.storage_http"):
        with pytest.raises(StorageException) as excinfo:
            StorageHTTP(_SECRET_URL).get("f")
    assert "hunter2" not in str(excinfo.value)
    assert "hunter2" not in caplog.text


@responses.activate
def test_storage_http_transport_error_message_redacts_credentials() -> None:
    responses.add(
        responses.GET,
        "https://mirror:hunter2@repo.example.com/debian/f",
        body=requests.exceptions.ConnectionError(
            "Max retries exceeded with url https://mirror:hunter2@repo.example.com/debian/f"
        ),
    )
    with pytest.raises(StorageException) as excinfo:
        StorageHTTP(_SECRET_URL).get_stream("f")
    assert "hunter2" not in str(excinfo.value)


@pytest.fixture
def no_netrc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's `~/.netrc` entry for the host would otherwise win over URL credentials."""
    monkeypatch.setenv("NETRC", str(tmp_path / "absent-netrc"))


@responses.activate
def test_storage_http_sends_url_credentials_as_basic_auth_not_in_the_url(
    no_netrc: None,
) -> None:
    """No URL `requests` handles holds the password, so no library error can quote it."""
    responses.add(responses.GET, "https://repo.example.com/debian/f", body=b"x")
    StorageHTTP(_SECRET_URL).get("f")
    sent = responses.calls[0].request
    assert sent.url == "https://repo.example.com/debian/f"
    assert sent.headers["Authorization"] == "Basic " + base64.b64encode(b"mirror:hunter2").decode()


@responses.activate
@pytest.mark.parametrize(
    ("machine", "sent"),
    [
        pytest.param("repo.example.com", b"user:pass", id="netrc-for-the-host"),
        pytest.param("other.invalid", b"mirror:hunter2", id="netrc-for-another-host"),
    ],
)
def test_storage_http_netrc_for_the_host_overrides_url_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, machine: str, sent: bytes
) -> None:
    netrc = tmp_path / "netrc"
    netrc.write_text(f"machine {machine} login user password pass\n")
    monkeypatch.setenv("NETRC", str(netrc))
    responses.add(responses.GET, "https://repo.example.com/debian/f", body=b"x")
    StorageHTTP(_SECRET_URL).get("f")
    expected = "Basic " + base64.b64encode(sent).decode()
    assert responses.calls[0].request.headers["Authorization"] == expected


@responses.activate
def test_storage_http_unquotes_percent_encoded_url_credentials(no_netrc: None) -> None:
    responses.add(responses.GET, "https://repo.example.com/f", body=b"x")
    StorageHTTP("https://mi%40rror:pa%2Fss@repo.example.com").get("f")
    expected = "Basic " + base64.b64encode(b"mi@rror:pa/ss").decode()
    assert responses.calls[0].request.headers["Authorization"] == expected


@pytest.mark.parametrize(
    ("call", "expected", "match"),
    [
        # `requests` refuses the scheme before connecting; its error would quote the URL.
        pytest.param(
            lambda: StorageHTTP("ftp://user:hunter2@127.0.0.1:1/b").get("f"),
            StorageException,
            "failed: InvalidSchema",
            id="scheme",
        ),
        pytest.param(
            lambda: StorageHTTP("http://user:hunter2@127.0.0.1:99999/b"),
            ValueError,
            "authority does not parse",
            id="port",
        ),
        pytest.param(
            lambda: make_storage("http://user:hunter2@/b"),
            StorageException,
            "Invalid HTTP URL",
            id="no-host",
        ),
        pytest.param(
            lambda: StorageHTTP("https://h.example/b?token=hunter2"),
            ValueError,
            "query or fragment",
            id="query",
        ),
        pytest.param(
            lambda: StorageHTTP("https://h.example/b#hunter2"),
            ValueError,
            "query or fragment",
            id="fragment",
        ),
        pytest.param(
            lambda: StorageHTTP("http://user:hunter2@:80/b"),
            StorageException,
            "Invalid HTTP URL",
            id="no-host-direct",
        ),
    ],
)
def test_storage_http_malformed_base_url_never_names_credentials(
    call: Callable[[], object], expected: type[Exception], match: str
) -> None:
    """The chained cause and the traceback count too: `logger.exception` prints both."""
    with pytest.raises(expected, match=match) as excinfo:
        call()
    assert type(excinfo.value) is expected
    chain: list[BaseException] = []
    exc: BaseException | None = excinfo.value
    while exc is not None:
        chain.append(exc)
        exc = exc.__cause__ or exc.__context__
    assert not [e for e in chain if "hunter2" in "".join(traceback.format_exception_only(e))]


def test_storage_http_rejected_join_message_redacts_credentials() -> None:
    with pytest.raises(StorageException, match="cross-origin") as excinfo:
        StorageHTTP(_SECRET_URL).get("//evil.invalid/steal")
    assert "hunter2" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Unsupported write / metadata / transaction operations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("put", ("p", b"x")),
        ("put_stream", ("p", None)),
        ("add_metadata", ("p", {})),
        ("delete", ("p",)),
        ("delete_metadata", ("p",)),
        ("delete_metadata_fields", ("p", [])),
        ("copy", ("a", "b")),
        ("move", ("a", "b")),
        ("format", ()),
        ("mkdir", ("d",)),
        ("rmdir", ("d",)),
        ("list", ()),
    ],
)
def test_storage_http_write_methods_raise_not_implemented(
    storage: StorageHTTP, method: str, args: tuple[object, ...]
) -> None:
    with pytest.raises(NotImplementedError):
        getattr(storage, method)(*args)


def test_storage_http_is_never_in_a_transaction(storage: StorageHTTP) -> None:
    assert storage.in_transaction() is False


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("transaction_begin", ()),
        ("transaction_commit", ()),
        ("transaction_revert", (None,)),
        ("transaction_discard", ()),
    ],
)
def test_storage_http_has_no_transaction_support(
    storage: StorageHTTP, method: str, args: tuple[object, ...]
) -> None:
    """StorageHTTP exposes transaction stubs that raise NotImplementedError."""
    with pytest.raises(NotImplementedError):
        getattr(storage, method)(*args)


# --------------------------------------------------------------------------- #
# SSRF guard — cross-origin paths rejected before network
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method", ["get", "get_metadata", "get_stream"])
def test_storage_http_raises_on_cross_origin_path(storage: StorageHTTP, method: str) -> None:
    """SSRF guard raises StorageException (not raw ValueError) at the storage layer."""
    with pytest.raises(StorageException, match="cross-origin"):
        getattr(storage, method)("//evil.invalid/steal")
