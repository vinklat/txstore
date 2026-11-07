"""Object footmark tokens and their serialization for storage transactions."""

import base64
from dataclasses import dataclass, field
from enum import IntEnum
from typing import NamedTuple, TypeIs

from txstore._footmark_wire import FootmarkFields, decode, encode
from txstore._timestamp import Timestamp

FOOTMARK_HEADER = 0x0001
# Matches ``int32 transaction_id`` in footmark.proto; only positive ids parse back.
TRANSACTION_ID_MIN = 1
TRANSACTION_ID_MAX = 2**31 - 1


def is_transaction_id(value: object) -> TypeIs[int]:
    """Return True when `value` is an int in ``1..2^31-1``; a bool is never an id."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and TRANSACTION_ID_MIN <= value <= TRANSACTION_ID_MAX
    )


class ObjectOperation(IntEnum):
    """Footmark operation codes; values match the ``Operation`` enum of the wire schema."""

    NOOP = 0
    BACKUP = 1
    PUT = 2
    DELETE = 3


def now_timestamp() -> Timestamp:
    """Return the current time; tests patch `txstore.transaction.now_timestamp` to age one."""
    return Timestamp.now()


@dataclass(frozen=True, slots=True, kw_only=True)
class ObjectFootmark:
    """The fields one footmark token carries, and its serialization.

    Attributes:
        header: Footmark format header.
        timestamp: Begin timestamp of the transaction.
        transaction_id: Transaction identifier.
        operation: What the footmark records.
    """

    header: int = FOOTMARK_HEADER
    timestamp: Timestamp = field(default_factory=now_timestamp)
    transaction_id: int = 0
    operation: ObjectOperation = ObjectOperation.NOOP

    def serialize(self) -> str:
        """Serialize the footmark to its token → [STORAGE.md](STORAGE.md#footmarks).

        Returns:
            str: Base64 of the wire encoding, with ``/`` replaced by ``@``.
        """
        buf = encode(
            FootmarkFields(
                header=self.header,
                seconds=self.timestamp.seconds,
                nanos=self.timestamp.nanos,
                transaction_id=self.transaction_id,
                operation=self.operation.value,
            )
        )
        return base64.b64encode(buf).decode("utf-8").replace("/", "@")

    @classmethod
    def from_token(cls, token: str) -> "ObjectFootmark":
        """Decode a footmark token.

        Args:
            token (str): A token as `serialize` returns it.

        Returns:
            The decoded footmark.

        Raises:
            ValueError: If the token is not strict base64, not a valid encoding, or carries an
                out-of-range timestamp or unknown operation code.
        """
        fields = decode(base64.b64decode(token.replace("@", "/"), validate=True))
        return cls(
            header=fields.header,
            timestamp=Timestamp(seconds=fields.seconds, nanos=fields.nanos),
            transaction_id=fields.transaction_id,
            operation=ObjectOperation(fields.operation),
        )


def build_footmarks(transaction_id: int, timestamp: Timestamp) -> list[str]:
    """Serialise one footmark token per operation for a transaction.

    All tokens share the single ``timestamp`` so the returned list is a pure
    function of ``(transaction_id, timestamp)`` plus the fixed operation enum.
    This lets a transaction record store only ``{id, timestamp}`` and regenerate
    the tokens deterministically on revert.

    Args:
        transaction_id: The transaction identifier embedded in every token.
        timestamp: The shared begin timestamp embedded in every token.

    Returns:
        Serialised footmark tokens indexed by ``ObjectOperation.value``.
    """
    # `ObjectOperation` values are 0, 1, 2, …, so definition order is index order.
    return [
        ObjectFootmark(transaction_id=transaction_id, operation=op, timestamp=timestamp).serialize()
        for op in ObjectOperation
    ]


def footmark_path(path: str, token: str) -> str:
    """Return the footmark path `<dir>/.<file>.<token>` that parks a change to `path`.

    → [STORAGE.md](STORAGE.md#footmarks).

    Args:
        path: The original relative path or full key.
        token: A footmark token, which holds no ``.``.

    Returns:
        The hidden sibling of `path` carrying `token`.
    """
    dirname, sep, filename = path.rpartition("/")
    return f"{dirname}{sep}.{filename}.{token}"


class FootmarkPath(NamedTuple):
    """A footmark path split into the original it parks a change to and its token."""

    original: str
    token: str


def split_footmark_path(path: str) -> FootmarkPath | None:
    """Return the parts of `path` when it has the footmark form `<dir>/.<file>.<token>`.

    The inverse of `footmark_path`. A basename with an empty `<file>` (`.<token>`) or no
    trailing segment has no original, so it is not a footmark path. Whether the segment is a
    token is the caller's question → [STORAGE.md](STORAGE.md#footmarks).

    Args:
        path: A relative path or full key.

    Returns:
        The original path and the trailing segment, or `None` for any other name.
    """
    dirname, sep, filename = path.rpartition("/")
    if not filename.startswith("."):
        return None
    stem, dot, token = filename[1:].rpartition(".")
    if not (dot and stem and token):
        return None
    return FootmarkPath(f"{dirname}{sep}{stem}", token)


def parse_footmark_token(token: str) -> ObjectFootmark | None:
    """Decode `token` if it is a footmark this build could have written.

    Strict on purpose: an orphan sweep deletes what this function accepts, so
    every ambiguous segment must be rejected rather than guessed. A dotfile
    suffix that is not a footmark (a reserved name or any other dotted
    segment) decodes to `None` and is left untouched.

    Args:
        token: A single path segment, without the leading dot.

    Returns:
        The decoded footmark, or `None` when `token` is not a well-formed
        footmark: bad base64, an undecodable encoding, a foreign header, an
        unknown operation code, a non-positive transaction id, or any segment
        that is not exactly the token `serialize` writes for its fields.
    """
    if not token:
        return None
    try:
        footmark = ObjectFootmark.from_token(token)
    except ValueError:
        # The decode errors `from_token` documents: base64, wire, and out-of-range timestamp or
        # operation values all mean the same thing here, not our footmark. Anything else is a
        # bug and propagates.
        return None
    if footmark.header != FOOTMARK_HEADER or not is_transaction_id(footmark.transaction_id):
        return None
    # Canonical round trip: rejects stray characters, non-canonical padding,
    # and unknown or repeated wire fields that decode to the same values.
    if footmark.serialize() != token:
        return None
    return footmark
