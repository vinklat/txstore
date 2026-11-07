"""Footmark token round-trip, token parsing, and enum invariant tests."""

from __future__ import annotations

import base64

import pytest

from txstore import ObjectOperation, Timestamp
from txstore._footmark_wire import FootmarkFields, encode
from txstore.footmark import (
    FOOTMARK_HEADER,
    FootmarkPath,
    ObjectFootmark,
    build_footmarks,
    footmark_path,
    parse_footmark_token,
    split_footmark_path,
)


def _token(fields: FootmarkFields) -> str:
    return base64.b64encode(encode(fields)).decode("utf-8").replace("/", "@")


def test_object_operation_enum_values_are_contiguous() -> None:
    """`build_footmarks` emits tokens in definition order and callers index them by
    `op.value`, so the values must be 0, 1, 2, … in definition order.
    """
    values = [op.value for op in ObjectOperation]
    assert values == list(range(len(values)))


def test_object_footmark_serialize_round_trip() -> None:
    src = ObjectFootmark(transaction_id=42, operation=ObjectOperation.PUT)
    encoded = src.serialize()
    assert isinstance(encoded, str)
    assert encoded
    # No '/' allowed in the encoded form (replaced by '@').
    assert "/" not in encoded

    decoded = ObjectFootmark.from_token(encoded)
    assert decoded.transaction_id == 42
    assert decoded.operation == ObjectOperation.PUT


def test_object_footmark_round_trip_handles_at_substitution() -> None:
    # Force several round-trips for different ops to ensure '@'<->'/' swap is symmetric.
    for op in (ObjectOperation.PUT, ObjectOperation.DELETE, ObjectOperation.BACKUP):
        src = ObjectFootmark(transaction_id=7, operation=op)
        for _ in range(3):
            decoded = ObjectFootmark.from_token(src.serialize())
            assert decoded.operation == op
            assert decoded.transaction_id == 7
            src = decoded


# --------------------------------------------------------------------------- #
# parse_footmark_token
# --------------------------------------------------------------------------- #


def test_parse_footmark_token_accepts_built_tokens() -> None:
    """Every token `build_footmarks` emits must decode back to its transaction."""
    ts = Timestamp.from_json("2026-01-01T00:00:00Z")
    for op in sorted(ObjectOperation, key=lambda o: o.value):
        token = build_footmarks(7, ts)[op.value]
        parsed = parse_footmark_token(token)
        assert parsed is not None
        assert parsed.transaction_id == 7
        assert parsed.operation == op


def test_parse_footmark_token_rejects_non_footmark_segments() -> None:
    """Reserved-file suffixes and plain words must never look like footmarks."""
    for token in ("", "json", "lock", "CA", "deb", "not base64!"):
        assert parse_footmark_token(token) is None


def test_parse_footmark_token_rejects_a_valid_token_with_extra_characters() -> None:
    """Characters outside the base64 alphabet must not be skipped into a match."""
    token = build_footmarks(7, Timestamp.from_json("2026-01-01T00:00:00Z"))[2]
    for segment in (
        "!" + token,
        token[:4] + "!!" + token[4:],
        token + "~",
        token[:4] + "-" + token[4:],
    ):
        assert parse_footmark_token(segment) is None


def test_parse_footmark_token_rejects_non_canonical_encodings() -> None:
    """Only the exact bytes `serialize` writes are ours: no padding bits, no extra fields."""
    ts = Timestamp.from_json("2026-01-01T00:00:00Z")
    token = build_footmarks(7, ts)[ObjectOperation.PUT.value]
    raw = base64.b64decode(token.replace("@", "/"))
    extra_field = base64.b64encode(raw + bytes([0x28, 0x01])).decode().replace("/", "@")
    repeated = base64.b64encode(raw + raw[-2:]).decode().replace("/", "@")
    assert parse_footmark_token(extra_field) is None
    assert parse_footmark_token(repeated) is None
    if token.endswith("="):
        stem = token.rstrip("=")
        bumped = stem[:-1] + chr(ord(stem[-1]) + 1) + token[len(stem) :]
        assert parse_footmark_token(bumped) is None


def test_parse_footmark_token_rejects_truncated_encoding() -> None:
    ts = Timestamp.from_json("2026-01-01T00:00:00Z")
    token = build_footmarks(7, ts)[ObjectOperation.PUT.value]
    raw = base64.b64decode(token.replace("@", "/"))
    truncated = base64.b64encode(raw[: len(raw) // 2]).decode("utf-8").replace("/", "@")
    assert parse_footmark_token(truncated) is None


def test_parse_footmark_token_rejects_foreign_header() -> None:
    """An encoding that decodes but carries another header is not ours."""
    token = _token(
        FootmarkFields(header=0x0002, transaction_id=7, operation=ObjectOperation.PUT.value)
    )
    assert parse_footmark_token(token) is None


def test_parse_footmark_token_rejects_non_positive_transaction_id() -> None:
    """`transaction_id` 0 is the never-written NOOP default, not a real transaction."""
    token = _token(
        FootmarkFields(
            header=FOOTMARK_HEADER, transaction_id=0, operation=ObjectOperation.PUT.value
        )
    )
    assert parse_footmark_token(token) is None


def test_parse_footmark_token_rejects_unknown_operation() -> None:
    token = _token(FootmarkFields(header=FOOTMARK_HEADER, transaction_id=7, operation=9))
    assert parse_footmark_token(token) is None


def test_parse_footmark_token_rejects_out_of_range_timestamp() -> None:
    token = _token(
        FootmarkFields(header=FOOTMARK_HEADER, seconds=-1 << 40, transaction_id=7, operation=2)
    )
    assert parse_footmark_token(token) is None


def test_object_footmark_preserves_timestamp() -> None:
    ts = Timestamp(seconds=1_760_000_000, nanos=123_456_789)
    decoded = ObjectFootmark.from_token(ObjectFootmark(transaction_id=3, timestamp=ts).serialize())
    assert decoded.timestamp == ts
    assert decoded.timestamp.to_json() == "2025-10-09T08:53:20.123456789Z"


# --------------------------------------------------------------------------- #
# footmark paths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("operation", list(ObjectOperation), ids=lambda op: op.name)
@pytest.mark.parametrize(
    "path", ["a.deb", "pool/main/a.deb", "pool/a.b.c.deb", "pool/.env", "pool/x.CAESDAi", "a."]
)
def test_footmark_path_round_trips(path: str, operation: ObjectOperation) -> None:
    token = build_footmarks(7, Timestamp.from_json("2026-01-01T00:00:00Z"))[operation]
    marked = footmark_path(path, token)

    assert split_footmark_path(marked) == FootmarkPath(original=path, token=token)


def test_footmark_path_hides_the_basename_only() -> None:
    assert footmark_path("pool/main/a.deb", "T") == "pool/main/.a.deb.T"
    assert footmark_path("a.deb", "T") == ".a.deb.T"


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("pool/a.deb", id="not-hidden"),
        pytest.param("pool/.env", id="no-token-segment"),
        pytest.param("pool/.T", id="no-original"),
        pytest.param(".T", id="no-original-at-root"),
        pytest.param("pool/.a.deb.", id="empty-token"),
        pytest.param("pool/plain.T", id="no-leading-dot"),
    ],
)
def test_split_footmark_path_rejects_other_names(path: str) -> None:
    assert split_footmark_path(path) is None
