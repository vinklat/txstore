"""`txstore.Timestamp`: frozen RFC 3339 form and parsing grammar.

History records store `Timestamp.to_json()` and revert rebuilds footmarks from it, so the vectors
below are fixed: they match the protobuf ``Timestamp`` JSON mapping footmark data was written with.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from txstore import Timestamp
from txstore._timestamp import MAX_SECONDS, MIN_SECONDS

TO_JSON = [
    (0, 0, "1970-01-01T00:00:00Z"),
    (1767225600, 0, "2026-01-01T00:00:00Z"),
    (1760000000, 123000000, "2025-10-09T08:53:20.123Z"),
    (1760000000, 123456000, "2025-10-09T08:53:20.123456Z"),
    (1760000000, 123456789, "2025-10-09T08:53:20.123456789Z"),
    (1760000000, 1, "2025-10-09T08:53:20.000000001Z"),
    (-62135596800, 0, "0001-01-01T00:00:00Z"),
    (253402300799, 999999999, "9999-12-31T23:59:59.999999999Z"),
    (-1, 500000000, "1969-12-31T23:59:59.500Z"),
]

FROM_JSON_OK = [
    ("2026-01-01T00:00:00Z", 1767225600, 0),
    ("2026-06-08T10:15:30.123456789Z", 1780913730, 123456789),
    ("2026-06-08T10:15:30.1Z", 1780913730, 100000000),
    ("2026-06-08T10:15:30.12345Z", 1780913730, 123450000),
    ("1972-01-01T10:00:20.021-05:00", 63126020, 21000000),
    ("2026-06-08T10:15:30+02:00", 1780906530, 0),
    ("2026-06-08T10:15:30.5+01:30", 1780908330, 500000000),
    ("0001-01-01T00:00:00Z", -62135596800, 0),
    ("9999-12-31T23:59:59.999999999Z", 253402300799, 999999999),
    ("2026-1-8T1:2:3Z", 1767834123, 0),
    ("2026-06-08T10:15:30.Z", 1780913730, 0),
]

FROM_JSON_BAD = [
    "2026-06-08T10:15:30",
    "2026-06-08t10:15:30Z",
    "2026-13-01T00:00:00Z",
    "2026-06-08T10:15:30ZZ",
    "2026-06-08T10:15:30.1234567890Z",
    "2026-06-08T10:15:30.0000000001Z",
    "2026-06-08T10:15:30.00000000000Z",
    "2026-06-08T10:15:30+0200",
    "2026-06-08 10:15:30Z",
    "",
    "garbage",
    "2026-06-08T10:15:30.abcZ",
    "9999-12-31T23:59:59-01:00",
    "2026-06-08T10:15:30.1_5Z",
    "2026-06-08T10:15:30.e5Z",
    "2026-06-08T10:15:30+1:2",
    "2026-06-08T10:15:30+ 1:00",
    "2026-06-08T10:15:30+-1:00",
    "2026-06-08T10:15:30+24:00",
    "2026-06-08T10:15:30+99:99",
    "\u0662026-06-08T10:15:30Z",
    "2026-06-08T10:15:30.\u0661Z",
]


@pytest.mark.parametrize(("seconds", "nanos", "text"), TO_JSON)
def test_to_json(seconds: int, nanos: int, text: str) -> None:
    assert Timestamp(seconds, nanos).to_json() == text


@pytest.mark.parametrize(("text", "seconds", "nanos"), FROM_JSON_OK)
def test_from_json(text: str, seconds: int, nanos: int) -> None:
    assert Timestamp.from_json(text) == Timestamp(seconds, nanos)


def test_from_json_rejects_a_non_string() -> None:
    with pytest.raises(ValueError, match="not a string"):
        Timestamp.from_json(123)  # type: ignore[arg-type]


@pytest.mark.parametrize("text", FROM_JSON_BAD)
def test_from_json_rejects(text: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - strptime, int() and range errors differ
        Timestamp.from_json(text)


@pytest.mark.parametrize(("seconds", "nanos", "text"), TO_JSON)
def test_json_round_trip(seconds: int, nanos: int, text: str) -> None:
    assert Timestamp.from_json(text).to_json() == text


@pytest.mark.parametrize(
    ("seconds", "nanos"),
    [(MIN_SECONDS - 1, 0), (MAX_SECONDS + 1, 0), (0, -1), (0, 1_000_000_000)],
)
def test_rejects_out_of_range(seconds: int, nanos: int) -> None:
    with pytest.raises(ValueError, match="outside"):
        Timestamp(seconds, nanos)


def test_datetime_round_trip_truncates_to_microseconds() -> None:
    ts = Timestamp(1760000000, 123456789)
    assert ts.to_datetime() == datetime(2025, 10, 9, 8, 53, 20, 123456, tzinfo=UTC)
    assert Timestamp.from_datetime(ts.to_datetime()) == Timestamp(1760000000, 123456000)


def test_from_datetime_converts_offsets() -> None:
    local = datetime(2026, 1, 1, 2, 0, tzinfo=timezone(timedelta(hours=2)))
    assert Timestamp.from_datetime(local) == Timestamp.from_json("2026-01-01T00:00:00Z")


def test_from_datetime_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Timestamp.from_datetime(datetime(2026, 1, 1))  # noqa: DTZ001


def test_now_is_current_utc() -> None:
    before = datetime.now(tz=UTC)
    now = Timestamp.now().to_datetime()
    assert before <= now <= datetime.now(tz=UTC)
