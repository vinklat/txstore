"""UTC instant with nanosecond precision and a fixed RFC 3339 text form.

`Timestamp` is the begin time a transaction shares across its footmarks and history record. Its
text form is the history record's ``timestamp``, and revert rebuilds footmark tokens from that
text, so formatting and parsing are frozen → [STORAGE.md](STORAGE.md#timestamps).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

NANOS_PER_SECOND = 1_000_000_000
# 0001-01-01T00:00:00Z and 9999-12-31T23:59:59Z, the range RFC 3339 years can express.
MIN_SECONDS = -62_135_596_800
MAX_SECONDS = 253_402_300_799

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NAIVE_EPOCH = datetime(1970, 1, 1)  # noqa: DTZ001 - strptime yields naive UTC wall time
_SECONDS_FORMAT = "%Y-%m-%dT%H:%M:%S"
_OFFSET_RE = re.compile(r"([+-])([01]\d|2[0-3]):([0-5]\d)", re.ASCII)


@dataclass(frozen=True, slots=True)
class Timestamp:
    """An instant as whole seconds since the Unix epoch plus nanoseconds.

    Attributes:
        seconds: Seconds since 1970-01-01T00:00:00Z, within `MIN_SECONDS`..`MAX_SECONDS`.
        nanos: Nanoseconds within the second, ``0 <= nanos < 1_000_000_000``.
    """

    seconds: int = 0
    nanos: int = 0

    def __post_init__(self) -> None:
        """Reject an instant outside the representable range.

        Raises:
            ValueError: If ``seconds`` or ``nanos`` is out of range.
        """
        if not MIN_SECONDS <= self.seconds <= MAX_SECONDS:
            raise ValueError(
                f"timestamp seconds {self.seconds} outside [{MIN_SECONDS}, {MAX_SECONDS}]"
            )
        if not 0 <= self.nanos < NANOS_PER_SECOND:
            raise ValueError(f"timestamp nanos {self.nanos} outside [0, {NANOS_PER_SECOND})")

    @classmethod
    def now(cls) -> Timestamp:
        """Return the current time at microsecond precision."""
        return cls.from_datetime(datetime.now(tz=UTC))

    @classmethod
    def from_datetime(cls, value: datetime) -> Timestamp:
        """Return the instant of an aware `value`.

        Raises:
            ValueError: If `value` is naive.
        """
        if value.tzinfo is None:
            raise ValueError("timestamp datetime must be timezone-aware")
        delta = value - _EPOCH
        return cls(
            seconds=delta.days * 86_400 + delta.seconds,
            nanos=delta.microseconds * 1_000,
        )

    @classmethod
    def from_json(cls, text: str) -> Timestamp:
        """Parse RFC 3339 `text` with a ``Z`` or ``±HH:MM`` offset and up to 9 fraction digits.

        Every character is ASCII; the offset hours are 00-23 and minutes 00-59.

        Raises:
            ValueError: If `text` is not such a timestamp or is out of range.
        """
        if not isinstance(text, str):
            raise ValueError(f"timestamp is not a string: {text!r}")
        if not text.isascii():
            raise ValueError(f"timestamp {text!r} has a non-ASCII character")
        offset_at = text.find("Z")
        if offset_at == -1:
            offset_at = text.find("+")
        if offset_at == -1:
            offset_at = text.rfind("-")
        if offset_at == -1:
            raise ValueError(f"timestamp {text!r} has no UTC offset")

        wall, _, fraction = text[:offset_at].partition(".")
        if "t" in wall:
            raise ValueError(f"timestamp {text!r} uses a lowercase 't'")
        delta = datetime.strptime(wall, _SECONDS_FORMAT) - _NAIVE_EPOCH  # noqa: DTZ007
        seconds = delta.days * 86_400 + delta.seconds
        if len(fraction) > 9:
            raise ValueError(f"timestamp {text!r} has more than 9 fraction digits")
        if fraction and not fraction.isdigit():
            raise ValueError(f"timestamp {text!r} has a non-digit fraction")
        nanos = int(fraction.ljust(9, "0")) if fraction else 0

        offset = text[offset_at:]
        if offset[0] == "Z":
            if offset != "Z":
                raise ValueError(f"timestamp {text!r} has trailing data")
        else:
            match = _OFFSET_RE.fullmatch(offset)
            if match is None:
                raise ValueError(f"timestamp {text!r} has an offset that is not ±HH:MM")
            sign, hours, minutes = match.groups()
            shift = (int(hours) * 60 + int(minutes)) * 60
            seconds += -shift if sign == "+" else shift
        return cls(seconds=seconds, nanos=nanos)

    def to_json(self) -> str:
        """Return the UTC RFC 3339 form with 0, 3, 6, or 9 fraction digits, ending in ``Z``."""
        wall = (_NAIVE_EPOCH + timedelta(seconds=self.seconds)).isoformat()
        if self.nanos == 0:
            return f"{wall}Z"
        if self.nanos % 1_000_000 == 0:
            return f"{wall}.{self.nanos // 1_000_000:03d}Z"
        if self.nanos % 1_000 == 0:
            return f"{wall}.{self.nanos // 1_000:06d}Z"
        return f"{wall}.{self.nanos:09d}Z"

    def to_datetime(self) -> datetime:
        """Return the aware UTC datetime, truncated to microseconds."""
        return _EPOCH + timedelta(seconds=self.seconds, microseconds=self.nanos // 1_000)
