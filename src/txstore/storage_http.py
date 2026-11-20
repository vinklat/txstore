"""StorageHTTP: read-only HTTP/HTTPS storage backend.

Read-only access to HTTP resources. Write/delete/metadata/copy/move
operations are inherited from `ReadOnlyStorageMixin` and raise
`StorageNotSupportedException`. Transaction methods raise `StorageNotSupportedException`
via `TransactionNotSupportedMixin`.

Redirects are never followed: `join_url` pins only the first request, so a
3xx would lead a fetch off the configured origin or prefix
→ [VALIDATION.md](VALIDATION.md#http-url-join-safety). URLs are redacted with
`redact_url` wherever they reach a log line or a message
→ [CONFIG.md](CONFIG.md#secrets).
"""

from __future__ import annotations

import logging
from typing import IO, Any, cast
from urllib.parse import unquote, urlunparse

import requests

from txstore._capabilities import ReadOnlyStorageMixin, TransactionNotSupportedMixin
from txstore.base import (
    CHUNK_SIZE,
    REQUEST_TIMEOUT,
    GuardedStream,
    Storage,
    StorageException,
    StorageNotFoundException,
    StorageNotSupportedException,
    StorageType,
)
from txstore.url import has_query_or_fragment, join_url, parse_url, redact_url, strip_userinfo

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Response headers `get_metadata` returns; anything else (cookies, server
# internals) stays out of the caller's metadata dict.
METADATA_HEADERS: tuple[str, ...] = (
    "Content-Type",
    "Content-Length",
    "Content-Encoding",
    "Last-Modified",
    "ETag",
    "Cache-Control",
)


class _ContentReader:
    """Readable view of a streamed response body, decoded by `iter_content`.

    `iter_content` raises urllib3's read errors as `requests.RequestException`, so the body never
    leaks a urllib3 class → [STORAGE.md](STORAGE.md#storage-abi).
    """

    def __init__(self, response: requests.Response) -> None:
        self._response = response
        self._chunks = response.iter_content(CHUNK_SIZE)
        self._buffer = bytearray()

    def read(self, size: int = -1) -> bytes:
        """Read up to `size` decoded bytes, or to the end when `size` is negative."""
        while size < 0 or len(self._buffer) < size:
            chunk = next(self._chunks, None)
            if chunk is None:
                break
            self._buffer += chunk
        end = len(self._buffer) if size < 0 else size
        data = bytes(self._buffer[:end])
        del self._buffer[:end]
        return data

    def close(self) -> None:
        """Release the connection."""
        self._response.close()


class StorageHTTP(TransactionNotSupportedMixin, ReadOnlyStorageMixin, Storage):
    """Read-only storage backend for HTTP/HTTPS resources.

    Implements `get_stream` (and so the shared `get`), `get_metadata`, `exists` against an
    HTTP server; `list` raises `StorageNotSupportedException` (HTTP has no listing
    API). Every write, delete, metadata, copy, move, format, mkdir, and
    rmdir method is inherited from `ReadOnlyStorageMixin` and raises
    `StorageNotSupportedException`. Transaction methods raise `StorageNotSupportedException`
    via `TransactionNotSupportedMixin`.

    Attributes:
        base_url (str): The base URL for the requested documents.
        verify (bool): Whether to verify HTTPS certificates.
    """

    def __init__(self, base_url: str, verify: bool = True) -> None:
        """Initialize the StorageHTTP instance.

        The URL's userinfo becomes Basic auth and leaves the URL, so no URL `requests` sees, and
        so no library error or chained cause, holds the credentials
        → [CONFIG.md](CONFIG.md#secrets).

        Args:
            base_url (str): The base URL for the requested documents.
            verify (bool): Whether to verify the HTTPS certificate. Default is True.

        Raises:
            ValueError: If the authority of `base_url` does not parse, or it carries a query or
                fragment; the message never names it → [VALIDATION.md](VALIDATION.md#storage-urls).
            StorageException: If `base_url` names no host → [STORAGE.md](STORAGE.md#construction).
        """
        super().__init__()
        parsed = parse_url(base_url)
        if has_query_or_fragment(base_url):
            raise ValueError("HTTP base URL carries a query or fragment: it takes neither")
        self._shown = redact_url(base_url)
        # The host, not the authority: `:80` and `u@:80` hold a port but name no host.
        if not parsed.hostname:
            raise StorageException(f"Invalid HTTP URL: {self._shown}")
        self.base_url = urlunparse(parsed._replace(netloc=strip_userinfo(parsed.netloc)))
        self._auth = (
            None
            if parsed.username is None
            else (unquote(parsed.username), unquote(parsed.password or ""))
        )
        self.verify = verify
        # Basic auth over plain http is cleartext → [CONFIG.md](CONFIG.md#secrets).
        self._plain_http = parsed.scheme == "http"
        if self._plain_http and parsed.username is not None:
            logger.warning("Credentials in %s are sent unencrypted over plain http", self._shown)
        self._warned_cleartext_netrc = False
        logger.debug("Init HTTP storage class: %s (verify=%s)", self._shown, self.verify)

    def __repr__(self) -> str:
        """Return a string representation of the storage."""
        return f"{self.get_type()} {self._shown}"

    def get_type(self) -> StorageType:
        """Return the storage backend type identifier."""
        return StorageType.http

    def _request(self, method: str, path: str, *, stream: bool = False) -> requests.Response:
        """Send one request for *path* without following redirects.

        Args:
            method: HTTP method.
            path: Path relative to `base_url`.
            stream: Defer the body download.

        Returns:
            The response, whatever its non-redirect status.

        Raises:
            StorageException: If the join is rejected, the request fails, or the
                server answers a redirect.
        """
        try:
            url = join_url(self.base_url, path)
        except ValueError as exc:
            raise StorageException(str(exc)) from exc
        shown = redact_url(url)
        # A netrc entry for the host wins over URL userinfo → [CONFIG.md](CONFIG.md#secrets).
        netrc_auth = requests.utils.get_netrc_auth(url)
        if netrc_auth and self._plain_http and not self._warned_cleartext_netrc:
            self._warned_cleartext_netrc = True
            logger.warning("netrc credentials for %s are sent unencrypted over plain http", shown)
        auth = netrc_auth or self._auth

        logger.debug("%s %s...", method, shown)
        try:
            response = requests.request(
                method,
                url,
                auth=auth,
                stream=stream,
                verify=self.verify,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as exc:
            # The library's text may quote the URL; `url` holds no credentials, but its query or
            # a malformed form still never reaches the message.
            raise StorageException(f"HTTP {method} {shown} failed: {type(exc).__name__}") from exc

        # Every 3xx, not just `is_redirect`: a 300 or 304, or one without `Location`, too.
        if 300 <= response.status_code < 400:
            response.close()
            raise StorageException(
                f"HTTP {method} {shown}: redirect {response.status_code} not followed"
            )
        return response

    @staticmethod
    def _raise_for_status(method: str, response: requests.Response) -> None:
        """Raise for a non-2xx *response*: 404 as not found, anything else as a failure.

        Raises:
            StorageNotFoundException: On HTTP 404.
            StorageException: On any other error status.
        """
        if response.ok:
            return
        response.close()
        shown = redact_url(response.url)
        if response.status_code == 404:
            raise StorageNotFoundException(f"HTTP object not found: {shown}")
        raise StorageException(f"HTTP {method} {shown}: status {response.status_code}")

    def list(self) -> list[str]:
        """List documents from HTTP server (not supported)."""
        raise StorageNotSupportedException("HTTP servers do not provide a directory listing API.")

    def get_metadata(self, path: str) -> dict[str, Any]:
        """Get document metadata from HEAD headers at the specified path.

        Returns:
            ``http_status_code`` plus the `METADATA_HEADERS` the server sent.

        Raises:
            StorageNotFoundException: If the object is missing (HTTP 404).
            StorageException: If the request fails, is redirected, or answers
                another error status, so a 403, 405, or 429 never reads as
                absent -> [STORAGE.md](STORAGE.md#storage-abi).
        """
        response = self._request("HEAD", path)
        self._raise_for_status("HEAD", response)

        ret: dict[str, Any] = {"http_status_code": response.status_code}
        for name in METADATA_HEADERS:
            value = response.headers.get(name)
            if value is not None:
                ret[name] = value
        return ret

    def exists(self, path: str) -> bool:
        """Check if a document exists at the specified path."""
        try:
            self.get_metadata(path)
        except StorageNotFoundException:
            return False
        return True

    def get_stream(self, path: str) -> IO[bytes]:
        """Get file stream of HTTP content.

        Args:
            path: The path to stream the content from.

        A content encoding such as gzip is decoded, and a read failure → `StorageException`
        → [STORAGE.md](STORAGE.md#storage-abi).

        Raises:
            StorageNotFoundException: If the object is missing (HTTP 404).
            StorageException: If the request fails, is redirected, or answers
                any other error status.

        Returns:
            IO[bytes]: A readable stream of the content at *path*; it does not seek.
        """
        try:
            response = self._request("GET", path, stream=True)
            self._raise_for_status("GET", response)
        except StorageException:
            self.metrics.read_errors_inc()
            raise

        self.metrics.read_objects_inc()
        guarded = GuardedStream(
            cast(IO[bytes], _ContentReader(response)),
            errors=(requests.RequestException,),
            path=redact_url(response.url),
            metrics=self.metrics,
        )
        return cast(IO[bytes], guarded)
