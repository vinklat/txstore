"""URL/path helpers in `txstore.url`."""

from __future__ import annotations

import logging
import traceback

import pytest

from txstore import (
    StorageType,
    get_supported_schemes,
    is_unsafe_path,
    join_url,
    parse_storage_url,
    redact_url,
)
from txstore.url import join_path, parse_s3_url_path, strip_userinfo

# --------------------------------------------------------------------------- #
# join_url
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("base", "path", "expected"),
    [
        ("http://h", "a/b", "http://h/a/b"),
        ("http://h/", "a", "http://h/a"),
        ("http://h/dists", "Release", "http://h/dists/Release"),
        # the trailing slash of a directory reference survives the `..` normalization
        ("http://h/a/", "sub/", "http://h/a/sub/"),
    ],
)
def test_join_url(base: str, path: str, expected: str) -> None:
    assert join_url(base, path) == expected


@pytest.mark.parametrize(
    ("base", "path"),
    [
        ("http://h", "//evil.invalid/x"),  # protocol-relative redirect
        ("http://h", "http://evil.invalid/x"),  # absolute URL override
        ("http://h", "https://evil.invalid/x"),  # cross-scheme + cross-origin
        ("https://h", "http://h/x"),  # scheme downgrade
        ("not-a-url", "path"),  # invalid base URL
    ],
)
def test_join_url_rejects_cross_origin(base: str, path: str) -> None:
    with pytest.raises(ValueError, match="URL"):
        join_url(base, path)


@pytest.mark.parametrize(
    "path",
    [
        "http://h/outside",  # same-origin absolute URL drops the prefix
        "http:/h/outside",  # what os.path.normpath collapses the above into
        "//h/outside",  # protocol-relative to the same host
        "/outside",  # rooted reference
        "../outside",  # resolved traversal above the prefix
        "%2e%2e/%2e%2e/outside",  # urljoin leaves the dots encoded
        "..%2f..%2foutside",  # encoded separators
        "%2E%2E/secret",  # uppercase encoding
        "%2e%2e%2fsecret",  # dots and separator both encoded
        "..%5c..%5coutside",  # backslash separator an origin resolves upward
    ],
)
def test_join_url_rejects_same_origin_prefix_escape(path: str) -> None:
    """Pinning the authority alone still lets a reference drop the repository prefix.

    `urljoin` does not decode `%2e`/`%2f`, so an origin that decodes before resolving
    `..` serves a sibling path while the raw form still starts with the prefix.
    """
    with pytest.raises(ValueError, match="out-of-prefix"):
        join_url("http://h/debian/", path)


@pytest.mark.parametrize(
    "path",
    [
        "a?b",
        "a#b",
        "dists/x/Packages?x=1",
        # Checked before resolution, so a key that also escapes names this cause.
        pytest.param("../a?b", id="before-prefix-check"),
        pytest.param("/outside#x", id="before-rooted-check"),
        pytest.param("http://evil/x?y", id="before-origin-check"),
    ],
)
def test_join_url_rejects_a_raw_query_or_fragment_in_the_key(path: str) -> None:
    """The key would fetch another object: `a?b` serves `a`, with a query."""
    with pytest.raises(ValueError, match="query or fragment"):
        join_url("http://h/debian/", path)


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("a?token={}", id="query"),
        pytest.param("a#{}", id="fragment"),
        pytest.param("http://u:{}@evil/x?y", id="userinfo-in-key"),
        pytest.param("//u:{}@evil/x#f", id="protocol-relative-userinfo"),
    ],
)
def test_join_url_query_or_fragment_refusal_echoes_no_part_of_the_key(key: str) -> None:
    """The raw key's query or userinfo may hold a credential, so only the redacted base shows."""
    secret = "hunter" + "2"  # Split, so the traceback's source lines do not carry it.
    with pytest.raises(ValueError, match="query or fragment") as excinfo:
        join_url(f"http://u:{secret}@h/debian/", key.format(secret))
    assert secret not in "".join(traceback.format_exception(excinfo.value))


def test_join_url_refusal_names_the_key_but_not_the_base_credentials() -> None:
    """A key is a name, not a secret (CONFIG.md#secrets): a later refusal names it."""
    secret = "hunter" + "2"
    with pytest.raises(ValueError, match=r"out-of-prefix URL join: 'http://\*\*\*@h/a;b'") as exc:
        join_url(f"http://u:{secret}@h/debian/", "../a;b")
    assert secret not in str(exc.value)


@pytest.mark.parametrize("path", ["a%3Fb", "a%23b"])
def test_join_url_names_an_encoded_query_delimiter_literally(path: str) -> None:
    assert join_url("http://h/debian/", path) == f"http://h/debian/{path}"


def test_join_url_rejects_a_doubly_encoded_key() -> None:
    """A key still holding an escape after one decode pass is doubly encoded."""
    with pytest.raises(ValueError, match="doubly encoded"):
        join_url("http://h/debian/", "%252e%252e/outside")


@pytest.mark.parametrize(
    "path",
    [
        "%252e%252e/outside",
        "pool/a%2520b.deb",
        "a%25b",
    ],
)
def test_the_double_encoding_rule_has_one_owner(path: str) -> None:
    """`is_unsafe_path` and `join_url` decide a doubly encoded key the same way.

    `is_unsafe_path` rejects the key where a caller applies it and `join_url` catches
    whatever reaches the network without it, so tightening one site alone would leave
    the other serving the escape.
    """
    assert is_unsafe_path(path) is True
    with pytest.raises(ValueError, match="doubly encoded"):
        join_url("http://h/debian/", path)


@pytest.mark.parametrize(
    "path",
    [
        "main/..%00/outside",
        "main/%2e%2e%00/outside",
        "main/..%0d/outside",
        "main/..%0a/outside",
        "main/..%09/outside",
        "main/..\x00/outside",
        "main/Packages%7f",
    ],
)
def test_the_control_character_rule_has_one_owner(path: str) -> None:
    """`is_unsafe_path` and `join_url` both refuse a key padded with a control character.

    `..%00` is no `..` segment, yet an origin that truncates or strips the padding
    before resolving `..` reads it as a parent step and serves a sibling path.
    """
    assert is_unsafe_path(path) is True
    with pytest.raises(ValueError, match="control character"):
        join_url("http://h/debian/", path)


@pytest.mark.parametrize("path", ["..;/outside", "main/..;x/..;/outside", "%2e%2e;/outside"])
def test_join_url_rejects_a_path_parameter_parent_segment(path: str) -> None:
    """A servlet-style origin drops `;params` before resolving `..`, so the pin drops them too."""
    assert is_unsafe_path(path) is True
    with pytest.raises(ValueError, match="out-of-prefix"):
        join_url("http://h/debian/", path)


@pytest.mark.parametrize(
    "path",
    [
        "pool/main/f/foo_1.0.deb",
        "pool/main/f/foo_1.0%2Bdeb10u1.deb",  # an encoded epoch plus stays legitimate
        "dists/bookworm/main/binary-amd64/Packages.xz",
    ],
)
def test_join_url_keeps_a_legitimate_key(path: str) -> None:
    """The decode pass must not reject keys a real archive publishes."""
    assert join_url("http://h/debian/", path) == f"http://h/debian/{path}"


@pytest.mark.parametrize(
    ("base", "path", "expected"),
    [
        ("http://h/debian/", "pool/h/hello.deb", "http://h/debian/pool/h/hello.deb"),
        ("http://h/debian", "dists/stable/Release", "http://h/debian/dists/stable/Release"),
        ("http://h/debian/", "./hello.deb", "http://h/debian/hello.deb"),
        ("http://h", "pool/hello.deb", "http://h/pool/hello.deb"),
    ],
)
def test_join_url_keeps_a_relative_key_under_the_prefix(
    base: str, path: str, expected: str
) -> None:
    """The prefix pin leaves every ordinary repository key alone, rooted base or not."""
    assert join_url(base, path) == expected


# --------------------------------------------------------------------------- #
# join_path
# --------------------------------------------------------------------------- #


def test_join_path_strips_leading_slash_by_default() -> None:
    assert join_path("/base", "x/y") == "base/x/y"


def test_join_path_keeps_root_when_no_root_false() -> None:
    assert join_path("/base", "x", no_root=False) == "/base/x"


# --------------------------------------------------------------------------- #
# redact_url
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param("https://u:secret@h/p", "https://***@h/p", id="user-and-password"),
        pytest.param("http://token@h:8080/a?q=1", "http://***@h:8080/a?***", id="user-port-query"),
        pytest.param(
            "https://h/p?X-Amz-Signature=abc&token=t", "https://h/p?***", id="presigned-query"
        ),
        pytest.param("s3://ak:sk@s3.example.com/bucket", "s3://***@s3.example.com/bucket", id="s3"),
        pytest.param("https://u:p@ss@h/p", "https://***@h/p", id="at-in-password"),
        pytest.param("fs:///var/data?x=1", "fs:///var/data?***", id="fs-query"),
        pytest.param("https://h/p?t=1#frag", "https://h/p?***#frag", id="query-then-fragment"),
    ],
)
def test_redact_url_hides_userinfo_and_query(url: str, expected: str) -> None:
    assert redact_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://h/p", id="no-userinfo"),
        pytest.param("fs:///var/data", id="fs"),
        pytest.param("https://h/a@b", id="at-in-path"),
    ],
)
def test_redact_url_leaves_url_without_userinfo_unchanged(url: str) -> None:
    assert redact_url(url) == url


@pytest.mark.parametrize("url", ["http://ak:hunter2@h.example/bucket", "https://u:hunter2@h/p"])
def test_parse_storage_url_logs_a_redacted_url(url: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="txstore.url"):
        parse_storage_url(url)
    assert "***@" in caplog.text
    assert "hunter2" not in caplog.text


@pytest.mark.parametrize(
    ("netloc", "expected"),
    [
        pytest.param("u:p@h", "h", id="user-and-password"),
        pytest.param("u:p@ss@h:9000", "h:9000", id="at-in-password"),
        pytest.param("h:9000", "h:9000", id="no-userinfo"),
    ],
)
def test_strip_userinfo_keeps_host_and_port(netloc: str, expected: str) -> None:
    assert strip_userinfo(netloc) == expected


def test_redact_url_unparseable_hides_input() -> None:
    assert redact_url("http://u:secret@[::1/") == "<unparseable URL>"


# An unencoded `/`, `?`, or `#` in a password ends the authority before its `@`, so the rest of
# the password lands in the path, query, or fragment where userinfo redaction cannot see it.
_SPLIT_PASSWORD_URLS = [
    pytest.param("https://user:pa#hunter2@host/x", id="hash"),
    pytest.param("s3://AKIDLEAK:se?hunter2@host/b", id="question-mark"),
    pytest.param("https://user:pa/hunter2@host/x", id="slash"),
    pytest.param("s3://AKIDLEAK:se/hunter2@host/b", id="s3-slash"),
    pytest.param("https://user:/hunter2@host/x", id="empty-port"),
    # urllib's own error quotes a netloc holding a character NFKC folds into a delimiter, or a
    # malformed bracketed host.
    pytest.param("https://user:hunter2\uff20x@host/x", id="nfkc"),
    pytest.param("https://u:p@[hunter2]/x@host/x", id="bracketed-host"),
]


@pytest.mark.parametrize("url", _SPLIT_PASSWORD_URLS)
def test_redact_url_hides_a_password_that_splits_the_url(url: str) -> None:
    assert redact_url(url) == "<unparseable URL>"


@pytest.mark.parametrize("url", _SPLIT_PASSWORD_URLS)
def test_parse_storage_url_refuses_a_password_that_splits_the_url(
    url: str, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(ValueError, match="percent-encode") as excinfo,
    ):
        parse_storage_url(url)
    shown = "".join(traceback.format_exception(excinfo.value))
    for secret in ("hunter2", "AKIDLEAK", "pa#", "se?"):
        assert secret not in shown
        assert secret not in caplog.text
    assert excinfo.value.__context__ is None


@pytest.mark.parametrize("url", _SPLIT_PASSWORD_URLS)
def test_join_url_never_names_a_base_whose_authority_does_not_parse(url: str) -> None:
    with pytest.raises(ValueError, match="authority does not parse") as excinfo:
        join_url(url, "x")
    assert "hunter2" not in "".join(traceback.format_exception(excinfo.value))


@pytest.mark.parametrize(
    "key", ["//[hunter2/", "//u:hunter2\uff20x@h/"], ids=["bracketed-host", "nfkc"]
)
def test_join_url_refuses_a_key_urllib_cannot_parse_without_naming_it(key: str) -> None:
    """The key may be caller input; urllib's own error would quote it."""
    with pytest.raises(ValueError, match="unparseable URL join") as excinfo:
        join_url("https://h/base/", key)
    assert "hunter2" not in "".join(traceback.format_exception_only(excinfo.value))
    assert excinfo.value.__context__ is None


@pytest.mark.parametrize(
    ("url", "netloc"),
    [
        pytest.param("https://h:9000/x", "h:9000", id="port"),
        pytest.param("https://u:p@h:8080/x", "u:p@h:8080", id="userinfo-and-port"),
        pytest.param("https://[::1]:8080/x", "[::1]:8080", id="ipv6"),
        pytest.param("https://u:1234@h/x", "u:1234@h", id="numeric-password"),
    ],
)
def test_parse_storage_url_accepts_a_well_formed_authority(url: str, netloc: str) -> None:
    assert parse_storage_url(url)[2] == netloc


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://h:99999/x", id="port-out-of-range"),
        pytest.param("https://h:/x", id="empty-port"),
        pytest.param("https://[::1/x", id="unclosed-ipv6"),
    ],
)
def test_parse_storage_url_refuses_an_authority_that_does_not_parse(url: str) -> None:
    with pytest.raises(ValueError, match="authority does not parse"):
        parse_storage_url(url)


# --------------------------------------------------------------------------- #
# parse_storage_url
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "expected_type", "expected_scheme"),
    [
        ("fs:///var/repo", StorageType.fs, ""),
        ("s3://endpoint/bucket/dists", StorageType.s3, "https"),
        ("http://repo.example.com/path", StorageType.http, "http"),
        ("https://repo.example.com/path", StorageType.http, "https"),
    ],
)
def test_parse_storage_url_known_schemes(
    url: str, expected_type: StorageType, expected_scheme: str
) -> None:
    storage_type, scheme, _netloc, _path = parse_storage_url(url)
    assert storage_type is expected_type
    assert scheme == expected_scheme


def test_parse_storage_url_unknown_scheme_raises() -> None:
    with pytest.raises(ValueError, match="Unknown storage scheme"):
        parse_storage_url("ftp://example.com")


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("fs:", id="bare-scheme"),
        pytest.param("fs://", id="empty-authority"),
        pytest.param("fs:a", id="relative"),
        pytest.param("fs:./a", id="dot-relative"),
        pytest.param("fs:~/a", id="tilde"),
        pytest.param("fs:hunter2/a", id="relative-marker"),
        # A dropped `//` turns userinfo into a relative path, past the authority check.
        pytest.param("fs:u:hunter2@h/b", id="userinfo-without-slashes"),
    ],
)
def test_parse_storage_url_refuses_an_fs_path_that_is_not_absolute(url: str) -> None:
    """`StorageFS` resolves a relative root against cwd, so the URL would root a store there."""
    with pytest.raises(ValueError, match="not absolute") as excinfo:
        parse_storage_url(url)
    assert "hunter2" not in "".join(traceback.format_exception(excinfo.value))
    assert excinfo.value.__context__ is None


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://u:hunter2@h/p?token=hunter2", id="query"),
        pytest.param("https://u:hunter2@h/p#frag", id="fragment"),
        pytest.param("fs:///var/data?", id="empty-query"),
        pytest.param("fs:///var/data#", id="empty-fragment"),
        pytest.param("s3://h/bucket?versionId=hunter2", id="s3-query"),
        # A numeric password reads as a valid port, so the authority parses, no redaction finds
        # the password, and only the message keeping the URL out hides it.
        pytest.param("https://u:12#hunter2@h/x", id="numeric-password-hash"),
    ],
)
def test_parse_storage_url_rejects_a_query_or_fragment(url: str) -> None:
    """Routing keeps only netloc and path, so the dropped part would change what the URL names."""
    with pytest.raises(ValueError, match="query or fragment") as excinfo:
        parse_storage_url(url)
    assert "hunter2" not in "".join(traceback.format_exception(excinfo.value))
    assert excinfo.value.__context__ is None


# --------------------------------------------------------------------------- #
# is_unsafe_path
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "../../etc/passwd",
        "main/../../../etc/passwd",
        "dists/../..",
    ],
)
def test_is_unsafe_path_rejects_traversal_absolute_and_empty(path: str) -> None:
    """A key that escapes the storage root, or names nothing, is unsafe."""
    assert is_unsafe_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "%2e%2e/etc/passwd",
        "%2E%2E/etc/passwd",
        "main/%2e%2e/%2e%2e/etc/passwd",
        "main/..%2fetc/passwd",
        "%2fetc/passwd",
        "%252e%252e/etc/passwd",
    ],
)
def test_is_unsafe_path_rejects_percent_encoded_traversal(path: str) -> None:
    """`urljoin` leaves an escape encoded, so an origin that decodes it escapes the prefix."""
    assert is_unsafe_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "\\etc\\passwd",
        "..\\..\\etc\\passwd",
        "main\\..\\..\\etc/passwd",
        "main/..\\etc/passwd",
        "main%5c..%5c..%5cetc/passwd",
    ],
)
def test_is_unsafe_path_rejects_backslash_traversal(path: str) -> None:
    """An origin that treats `\\` as `/` resolves a backslash `..` upward, raw or encoded."""
    assert is_unsafe_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "http://host/outside",
        "https://host/outside",
        "HTTP://HOST/outside",
        "http:/host/outside",
        "evil:pool/hello.deb",
        "http:%2f%2fhost/outside",
        "//host/outside",
        "main//../etc/passwd",
        "a//b",
        "a/%2f/b",
        "a\\\\b",
    ],
)
def test_is_unsafe_path_rejects_scheme_and_protocol_relative_keys(path: str) -> None:
    """`urljoin` resolves a reference carrying a scheme against the origin, not the prefix."""
    assert is_unsafe_path(path) is True


def test_is_unsafe_path_rejects_a_trailing_slash() -> None:
    """A trailing `/` leaves an empty last segment: the key names a prefix, not an object."""
    assert is_unsafe_path("a/b/") is True


@pytest.mark.parametrize(
    "path", ["..?x", "%2e%2e?x", "dists/..#x", "a/b?c", "Packages%3fx", "Packages%23x"]
)
def test_is_unsafe_path_rejects_query_and_fragment_delimiters(path: str) -> None:
    """`urljoin` ends the path at `?` or `#`, so the key would fetch another resource."""
    assert is_unsafe_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "dists/bookworm/Release",
        "pool/main/h/hello/hello_1.0_amd64.deb",
        "Packages.xz",
        "a..b/c",
        "main/b%20c/Packages.xz",
        "pool/h/hello_1%3a2.0-1_amd64.deb",
        "pool/h/hello_1:2.0-1_amd64.deb",
        "main/dep11/icons-64x64@2.xz",
        "main/..a/b;c",
    ],
)
def test_is_unsafe_path_accepts_relative_key(path: str) -> None:
    """A relative key without a `..` segment stays inside the root, encoded or not.

    An epoch `:` never falls in the first segment, so the scheme rule leaves it alone.
    """
    assert is_unsafe_path(path) is False


# --------------------------------------------------------------------------- #
# Supported schemes
# --------------------------------------------------------------------------- #


def test_get_supported_schemes_lists_every_url_routable_scheme() -> None:
    assert get_supported_schemes() == ["fs", "s3", "http", "https"]


def test_get_supported_schemes_omits_the_non_routable_null_backend() -> None:
    assert "null" not in get_supported_schemes()


# --------------------------------------------------------------------------- #
# parse_s3_url_path
# --------------------------------------------------------------------------- #


def test_parse_s3_url_path_bucket_only() -> None:
    assert parse_s3_url_path("/my-bucket") == ("my-bucket", None)


def test_parse_s3_url_path_bucket_and_prefix() -> None:
    assert parse_s3_url_path("/my-bucket/dists/bookworm") == ("my-bucket", "dists/bookworm")


def test_parse_s3_url_path_trailing_slash_normalised_to_none() -> None:
    assert parse_s3_url_path("/my-bucket/") == ("my-bucket", None)


@pytest.mark.parametrize("url_path", ["/my-bucket/dists/", "/my-bucket/dists//"])
def test_parse_s3_url_path_drops_prefix_trailing_slash(url_path: str) -> None:
    assert parse_s3_url_path(url_path) == ("my-bucket", "dists")


@pytest.mark.parametrize("url_path", ["/my-bucket//x", "/my-bucket/a//b"])
def test_parse_s3_url_path_empty_prefix_segment_raises(url_path: str) -> None:
    with pytest.raises(ValueError, match="empty segment"):
        parse_s3_url_path(url_path)


@pytest.mark.parametrize("url_path", ["", "/", "//", "//x"])
def test_parse_s3_url_path_empty_bucket_raises(url_path: str) -> None:
    with pytest.raises(ValueError, match="bucket name"):
        parse_s3_url_path(url_path)
