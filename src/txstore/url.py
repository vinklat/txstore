"""Methods for manipulating URLs and paths."""

import logging
import os
import posixpath
import re
from urllib.parse import ParseResult, unquote, urljoin, urlparse

from txstore.base import StorageType

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# C0 controls and DEL: no legitimate key holds one
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# a `;params` suffix an origin may drop from a segment before resolving `..`
_SEGMENT_PARAMS = re.compile(r";[^/\\]*")
# query and fragment delimiters: `urljoin` ends the path at the first one
_URL_DELIMITERS = ("?", "#")


def _is_doubly_encoded(value: str) -> bool:
    """Return True when value still holds a percent escape after one decode pass.

    One decode pass settles a single encoding; a value still holding an escape after
    it is doubly encoded, and no legitimate key or URL path is.

    Args:
        value (str): candidate key or URL path.

    Returns:
        bool: True when the value is doubly encoded.
    """
    decoded = unquote(value)
    return decoded != value and "%" in decoded


def _has_control_char(value: str) -> bool:
    """Return True when value holds a C0 control or DEL, raw or after one decode pass.

    `..%00` or `..%0a` is no `..` segment to a membership test, yet an origin that
    truncates or strips the padding before resolving `..` reads it as a parent step.

    Args:
        value (str): candidate key or URL path.

    Returns:
        bool: True when the value holds a control character.
    """
    return any(_CONTROL_CHARS.search(candidate) for candidate in (value, unquote(value)))


def _is_parent_segment(segment: str) -> bool:
    """Return True when segment is `..`, with or without a `;params` suffix.

    Servlet-style origins drop path parameters before resolving `..`, so `..;x` climbs.

    Args:
        segment (str): one path segment.

    Returns:
        bool: True when an origin may resolve the segment upward.
    """
    return segment.partition(";")[0] == ".."


# Message of every `parse_url` refusal: it cannot name the URL, since the part a password
# spilled into is not where any redaction looks → [CONFIG.md](CONFIG.md#secrets).
_UNPARSEABLE_AUTHORITY = (
    "URL authority does not parse: percent-encode non-ASCII characters and / ? # @ in credentials"
)
_CONTROL_CHARACTER = "URL holds a control character: percent-encode tabs and line breaks"
_NO_AUTHORITY = "URL holds '@' but no '//' authority: write scheme://user:password@host"


def _authority_parses(parsed: ParseResult) -> bool:
    """Return True when the authority has no port, or a non-empty numeric one in range.

    A password holding an unencoded `/`, `?`, or `#` ends the authority before its `@`, so the
    rest of the password reads as a port; such an authority never names a real host.
    """
    if parsed.netloc.endswith(":"):
        return False
    try:
        parsed.port  # noqa: B018 - the property validates on access
    except ValueError:
        return False
    return True


def parse_url(url: str) -> ParseResult:
    """Return `urlparse(url)` after checking that its authority parses.

    Raises:
        ValueError: If the URL holds a control character, holds `@` with no `//` authority,
            urllib refuses it, or its authority does not parse; the message never names the URL
            → [VALIDATION.md](VALIDATION.md#storage-urls).
    """
    # urllib drops tabs and line breaks before splitting, so its netloc would not match the
    # string that redaction edits → [CONFIG.md](CONFIG.md#secrets).
    if _CONTROL_CHARS.search(url):
        raise ValueError(_CONTROL_CHARACTER)
    # Without `//`, urllib reads `user:password@host` as a path, where no userinfo check or
    # redaction looks.
    scheme, colon, rest = url.partition(":")
    if colon and scheme and "@" in url and not rest.startswith("//"):
        raise ValueError(_NO_AUTHORITY)
    # urllib's own error quotes the netloc, credentials included, so the refusal is raised
    # after the handler and carries no context.
    try:
        parsed: ParseResult | None = urlparse(url)
    except ValueError:
        parsed = None
    if parsed is None or not _authority_parses(parsed):
        raise ValueError(_UNPARSEABLE_AUTHORITY)
    return parsed


def strip_userinfo(netloc: str) -> str:
    """Return the `host[:port]` of netloc, without any `user:password@` userinfo.

    Args:
        netloc (str): network location, as `urlparse` splits it.

    Returns:
        str: netloc after its last `@`, or netloc unchanged when it has none.
    """
    return netloc.rpartition("@")[2]


def redact_url(url: str) -> str:
    """Return url with its userinfo and query replaced by `***`, for logs, repr, and messages.

    A storage URL may carry credentials as `user:password@host`, and a query may carry a
    token or a presigned signature; neither reaches a log line or an exception message
    -> [CONFIG.md](CONFIG.md#secrets). The rest of the URL is kept as written.

    Args:
        url (str): URL that may hold userinfo or a query.

    Returns:
        str: the URL with `***@` in place of any userinfo and `?***` in place of any query;
            unchanged when it has neither; `<unparseable URL>` when it holds a control
            character, holds `@` with no `//` authority, or its authority does not parse, since
            an unencoded `/`, `?`, or `#` in a password ends the authority early.
    """
    try:
        parsed = parse_url(url)
    except ValueError:
        return "<unparseable URL>"
    redacted = url
    if parsed.query:
        # the first `?` starts the query; a fragment after it is kept
        head, _, tail = url.partition("?")
        _, hash_mark, fragment = tail.partition("#")
        redacted = f"{head}?***{hash_mark}{fragment}"
    if "@" in parsed.netloc:
        redacted = redacted.replace(parsed.netloc, f"***@{strip_userinfo(parsed.netloc)}", 1)
    return redacted


def join_url(base_url: str, path: str) -> str:
    """Join path with base_url and return the resulting URL string.

    Path may be relative.

    Paths that resolve to a different scheme or authority (browser-style URL
    resolution) raise ValueError to prevent SSRF when ``path`` is supplied by a
    caller or read back from storage.

    The resolved path must also stay under the base path, after one percent-decode
    pass. Pinning the authority alone leaves a same-origin escape: `urljoin` drops
    the base path for any reference it reads as rooted or scheme-carrying,
    so the client would read a sibling path on the configured host. `urljoin` also
    leaves `%2e%2e` encoded, so an origin that decodes before resolving `..` serves
    a sibling path while the raw form still starts with the prefix; the comparison
    therefore runs on the decoded path, and a key still holding an escape after one
    pass is doubly encoded and rejected outright. A path holding a control character
    is rejected too, and the pin drops `;params` before resolving `..`, the two
    paddings an origin may strip first. `is_unsafe_path` rejects those
    keys where a caller applies it, and this pin catches whatever reaches the
    network without it -> [VALIDATION.md](VALIDATION.md#http-url-join-safety).

    Args:
        base_url (str): base URL.
        path (str): path to join.

    Returns:
        str: joined URL.

    Raises:
        ValueError: base URL has no scheme or authority or its authority does not parse, or the
            joined URL does not parse, escapes the base authority or path, is doubly encoded,
            or holds a control character (possible SSRF URL), or `path` holds a raw `?` or `#`.
    """
    base_parsed = parse_url(base_url)
    if not base_parsed.scheme or not base_parsed.netloc:
        raise ValueError(f"Cannot join URLs: invalid base URL: {redact_url(base_url)!r}")
    # A raw `?` or `#` would make the key fetch a different object; `%3F` names it literally.
    # Checked on the key alone, before resolving, so the refusal names this cause; the raw key
    # is never echoed, since its query may carry a credential that only redaction would mask.
    if has_query_or_fragment(path):
        raise ValueError(f"Rejected query or fragment URL join (base {redact_url(base_url)!r})")

    normalized_base = base_url.rstrip("/") + "/"
    # urllib's own error may quote the key, so the refusal is raised after the handler.
    unparseable = False
    try:
        resolved = urljoin(normalized_base, path)
        joined_parsed = urlparse(resolved)
    except ValueError:
        unparseable = True
    if unparseable:
        raise ValueError(f"Rejected unparseable URL join (base {redact_url(base_url)!r})")
    shown = f"{redact_url(resolved)!r} (base {redact_url(base_url)!r})"

    if joined_parsed.scheme.lower() != base_parsed.scheme.lower():
        raise ValueError(f"Rejected cross-scheme URL join: {shown}")
    if joined_parsed.netloc.lower() != base_parsed.netloc.lower():
        raise ValueError(f"Rejected cross-origin URL join: {shown}")
    if _is_doubly_encoded(joined_parsed.path):
        raise ValueError(f"Rejected doubly encoded URL join: {shown}")
    if _has_control_char(joined_parsed.path):
        raise ValueError(f"Rejected control character URL join: {shown}")
    decoded_path = unquote(joined_parsed.path)
    base_path = unquote(urlparse(normalized_base).path)
    # an origin resolves `..` after decoding, so the pin resolves it too: comparing the
    # decoded path alone still reads `/debian/../outside` as starting with `/debian/`
    resolved_path = posixpath.normpath(_SEGMENT_PARAMS.sub("", decoded_path.replace("\\", "/")))
    if not resolved_path.endswith("/") and decoded_path.endswith("/"):
        resolved_path += "/"
    if resolved_path != base_path.rstrip("/") and not resolved_path.startswith(base_path):
        raise ValueError(f"Rejected out-of-prefix URL join: {shown}")
    return resolved


def join_path(base_path: str, path: str, no_root: bool = True) -> str:
    """Join path with base_path and return the resulting filesystem path.

    Path may be relative.

    Args:
        base_path (str): base path.
        path (str): path to join.
        no_root (bool): remove leading slash. Defaults to True.

    Returns:
        str: joined path.
    """
    ret = os.path.realpath(os.path.join("/", base_path, path))

    if no_root:
        ret = re.sub(r"^/{1,}", "", ret)

    return ret


def is_unsafe_path(path: str) -> bool:
    r"""Report whether a storage key would escape the storage root.

    `os.path.normpath` does not collapse leading `..` segments, so a normalized
    path is not yet a safe one
    -> [VALIDATION.md](VALIDATION.md#filename-safety).

    A key reaches an HTTP source through `join_url`, and `urljoin` leaves `%2e%2e`
    encoded, so an origin that decodes it before resolving `..` serves a same-origin
    path outside the base path.
    Both forms are checked, because the decoded one is what an origin resolves while
    the raw one is the key the storage holds. A backslash counts as a separator
    too: `..\etc` splits into no `..` segment on `/`, yet an origin that treats `\`
    as `/` resolves it upward, and no legitimate key holds a backslash. A key
    holding a control character is unsafe, and a `..;params` segment counts as `..`:
    an origin may strip either padding before it resolves `..`.

    A key must also be relative in the URL sense, not only in the leading-slash sense.
    `urljoin` resolves a reference carrying a scheme against the origin rather than the
    base path, so `http://host/outside` and the `http:/host/outside` that
    `os.path.normpath` collapses it to both leave the prefix while `join_url` still sees the
    configured authority. The first segment therefore holds no `:`, and no segment is
    empty: that covers every `scheme:` form without touching an epoch `:` in a package
    filename, which never falls in the first segment. A `?` or `#` ends the URL path,
    so the key would fetch another resource than the one it is stored under.

    Args:
        path (str): candidate storage key.

    Returns:
        bool: True when the key is unsafe to read, write, or delete.
    """
    if not path:
        return True

    if _is_doubly_encoded(path) or _has_control_char(path):
        return True

    decoded = unquote(path)

    for candidate in (path, decoded):
        segments = re.split(r"[/\\]", candidate)
        if candidate.startswith(("/", "\\")) or any(_is_parent_segment(s) for s in segments):
            return True
        # a scheme turns the key into a URL reference `urljoin` resolves against the
        # origin, and an empty segment makes it protocol-relative
        if ":" in segments[0] or "" in segments[1:]:
            return True
        if any(delimiter in candidate for delimiter in _URL_DELIMITERS):
            return True

    return False


# URL scheme → (backend, transport scheme it is built with; ``""`` for ``fs``) → VALIDATION.md.
_SCHEMES: dict[str, tuple[StorageType, str]] = {
    "fs": (StorageType.fs, ""),
    "s3": (StorageType.s3, "https"),
    "http": (StorageType.http, "http"),
    "https": (StorageType.http, "https"),
}


def get_supported_schemes() -> list[str]:
    """Return the URL schemes `parse_storage_url` recognises.

    Returns:
        Supported scheme tokens.
    """
    return list(_SCHEMES)


def has_query_or_fragment(url: str) -> bool:
    """Return whether `url` holds a `?` or `#` anywhere, even an empty one → VALIDATION.md.

    Args:
        url: storage URL or backend endpoint.

    Returns:
        True when the URL would carry a query or fragment.
    """
    return any(delimiter in url for delimiter in _URL_DELIMITERS)


def parse_storage_url(url: str) -> tuple[StorageType, str, str, str]:
    """Parse a storage URL into its routing tuple.

    Args:
        url (str): storage URL.

    Raises:
        ValueError: if the URL does not parse, uses an unknown scheme, has an authority that does
            not parse, carries a query or fragment, is an ``s3://`` URL holding ``@``, or is an
            ``fs://`` URL with an authority or a path that is not absolute; no message names the
            URL.

    Returns:
        Tuple[StorageType, str, str, str]: storage_type, transport scheme (empty for ``fs``),
        netloc, path.
    """
    parsed = parse_url(url)
    if parsed.scheme not in _SCHEMES:
        raise ValueError(
            f"Unknown storage scheme '{parsed.scheme}'. "
            f"Supported schemes are: {', '.join(_SCHEMES)}."
        )
    # No message below names the URL: an unencoded `/`, `?`, or `#` in a password splits the
    # URL where no redaction can find the password → [CONFIG.md](CONFIG.md#secrets).
    # `s3://` takes its keys from settings, so any `@` is a misplaced credential, wherever the
    # split left it.
    if parsed.scheme == "s3" and "@" in url:
        raise ValueError(
            "s3:// URL holds '@': S3 keys come from s3_access and s3_secret, never the URL"
        )
    # A local root has no host, and an authority would be glued onto the path, credentials
    # included, where `StorageFS` repr and logs show it.
    if parsed.scheme == "fs" and parsed.netloc:
        raise ValueError("fs:// URL names an authority: write fs:///absolute/path")
    # `StorageFS` resolves a relative root against the working directory, so a truncated URL
    # such as `fs://` would silently make the working directory a store.
    if parsed.scheme == "fs" and not parsed.path.startswith("/"):
        raise ValueError("fs:// URL path is not absolute: write fs:///absolute/path")
    # routing keeps only netloc and path, so a query or fragment would be dropped silently;
    # any delimiter counts, since `fs:///a#` would otherwise name `/a`
    if has_query_or_fragment(url):
        raise ValueError(
            f"Storage URL carries a query or fragment ({parsed.scheme}://...): storage URLs take "
            "neither; percent-encode ? and # in credentials"
        )
    storage_type, ret_scheme = _SCHEMES[parsed.scheme]
    logger.debug("storage_type=%s, url=%s", str(storage_type), redact_url(url))

    return storage_type, ret_scheme, parsed.netloc, parsed.path


def parse_s3_url_path(url_path: str) -> tuple[str, str | None]:
    """Split the path of an ``s3://`` URL into ``(bucket, prefix)`` → VALIDATION.md.

    The prefix goes through `normalize_s3_prefix`, so ``s3://host/bucket/`` gives prefix ``None``
    and ``s3://host/bucket/dir/`` gives ``dir``, the same keys as the spelling without a slash.

    Args:
        url_path: Path portion of the storage URL (with leading ``/``).

    Raises:
        ValueError: If the bucket segment is empty (``""``, ``/``, ``//x``), or the prefix holds
            an empty segment (``/b//x``, ``/b/a//b``).

    Returns:
        ``(bucket, prefix)``; ``prefix`` is ``None`` when absent or when only slashes follow
        the bucket name.
    """
    url_path_split = url_path[1:].split("/", 1)
    bucket = url_path_split[0]
    if not bucket:
        raise ValueError("S3 bucket name is not specified in url.")
    return bucket, normalize_s3_prefix(url_path_split[1] if len(url_path_split) == 2 else None)


def normalize_s3_prefix(prefix: str | None) -> str | None:
    """Return the canonical form of an S3 key prefix → VALIDATION.md.

    Keys are ``<prefix>/<path>``, so ``dir/`` would give ``dir//a``, a different S3 object from
    the ``dir/a`` that ``dir`` gives. Trailing slashes are dropped; an empty segment elsewhere
    is refused rather than collapsed, since it names keys another spelling cannot reach.

    Args:
        prefix: Key prefix from the URL path or the ``StorageS3`` argument.

    Raises:
        ValueError: If the prefix starts with ``/`` or holds ``//`` once trailing slashes are
            dropped.

    Returns:
        The prefix without trailing slashes; ``None`` when absent or empty.
    """
    if prefix is None:
        return None
    prefix = prefix.rstrip("/")
    if prefix.startswith("/") or "//" in prefix:
        raise ValueError(f"S3 prefix has an empty segment: {prefix!r}")
    return prefix or None
