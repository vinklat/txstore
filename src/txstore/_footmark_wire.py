"""Binary encoding of a footmark.

A footmark is the protocol-buffers message in
[docs/reference/footmark.proto](docs/reference/footmark.proto); this module encodes and
decodes it in the protobuf wire format without a protobuf runtime, so footmark objects stay
byte-compatible with any protobuf implementation → [STORAGE.md](STORAGE.md#footmarks).

Decoding follows proto3: absent fields are zero, the last scalar occurrence wins, repeated
timestamp fields merge, unknown fields and fields of an unexpected wire type are skipped. Group
wire types are rejected.
"""

from __future__ import annotations

from typing import NamedTuple, cast

_VARINT = 0
_FIXED64 = 1
_LENGTH = 2
_FIXED32 = 5

_HEADER = 1
_TIMESTAMP = 2
_TRANSACTION_ID = 3
_OPERATION = 4

_TS_SECONDS = 1
_TS_NANOS = 2

_MASK64 = (1 << 64) - 1
_MAX_VARINT_BYTES = 10
# A tag is a uint32 in at most 5 varint bytes.
_MAX_TAG_BYTES = 5
_MAX_TAG = 0xFFFF_FFFF


class FootmarkFields(NamedTuple):
    """Decoded footmark fields; absent fields are zero.

    Attributes:
        header: Format marker.
        seconds: Begin timestamp seconds since the Unix epoch.
        nanos: Begin timestamp nanoseconds.
        transaction_id: Transaction identifier.
        operation: Operation code.
    """

    header: int = 0
    seconds: int = 0
    nanos: int = 0
    transaction_id: int = 0
    operation: int = 0


def encode(fields: FootmarkFields) -> bytes:
    """Encode `fields`, always emitting the timestamp and omitting other zero fields.

    Args:
        fields: Footmark values; integers must fit their protobuf types.

    Returns:
        The serialized message.
    """
    timestamp = _scalar(_TS_SECONDS, fields.seconds) + _scalar(_TS_NANOS, fields.nanos)
    return b"".join(
        (
            _scalar(_HEADER, fields.header),
            _tag(_TIMESTAMP, _LENGTH) + _varint(len(timestamp)) + timestamp,
            _scalar(_TRANSACTION_ID, fields.transaction_id),
            _scalar(_OPERATION, fields.operation),
        )
    )


def decode(data: bytes) -> FootmarkFields:
    """Decode a serialized footmark.

    Args:
        data: The serialized message.

    Returns:
        The decoded fields.

    Raises:
        ValueError: If `data` is truncated, has a tag above 32 bits or field number 0, uses a
            group or an invalid wire type, or has a value varint longer than 10 bytes.
    """
    header = transaction_id = operation = seconds = nanos = 0
    for field, wire_type, value in _fields(data):
        if wire_type == _VARINT and field == _HEADER:
            header = _int32(cast(int, value))
        elif wire_type == _VARINT and field == _TRANSACTION_ID:
            transaction_id = _int32(cast(int, value))
        elif wire_type == _VARINT and field == _OPERATION:
            operation = _int32(cast(int, value))
        elif wire_type == _LENGTH and field == _TIMESTAMP:
            # `_fields` yields bytes for a length-delimited value and an int for a varint.
            for ts_field, ts_wire_type, ts_value in _fields(cast(bytes, value)):
                if ts_wire_type == _VARINT and ts_field == _TS_SECONDS:
                    seconds = _int64(cast(int, ts_value))
                elif ts_wire_type == _VARINT and ts_field == _TS_NANOS:
                    nanos = _int32(cast(int, ts_value))
    return FootmarkFields(header, seconds, nanos, transaction_id, operation)


def _fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    """Split `data` into ``(field, wire_type, value)``; varints stay unsigned 64-bit."""
    out: list[tuple[int, int, int | bytes]] = []
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos, _MAX_TAG_BYTES)
        if key > _MAX_TAG:
            raise ValueError("footmark tag above 32 bits")
        field, wire_type = key >> 3, key & 7
        if field == 0:
            raise ValueError("footmark field number 0")
        value: int | bytes
        if wire_type == _VARINT:
            value, pos = _read_varint(data, pos)
        elif wire_type == _LENGTH:
            size, pos = _read_varint(data, pos)
            value, pos = _take(data, pos, size)
        elif wire_type == _FIXED64:
            value, pos = _take(data, pos, 8)
        elif wire_type == _FIXED32:
            value, pos = _take(data, pos, 4)
        else:
            raise ValueError(f"footmark wire type {wire_type} not supported")
        out.append((field, wire_type, value))
    return out


def _take(data: bytes, pos: int, size: int) -> tuple[bytes, int]:
    end = pos + size
    if end > len(data):
        raise ValueError("footmark truncated")
    return data[pos:end], end


def _read_varint(data: bytes, pos: int, max_bytes: int = _MAX_VARINT_BYTES) -> tuple[int, int]:
    result = 0
    for index in range(max_bytes):
        if pos >= len(data):
            raise ValueError("footmark truncated inside a varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return result & _MASK64, pos
    raise ValueError(f"footmark varint longer than {max_bytes} bytes")


def _int32(value: int) -> int:
    value &= 0xFFFF_FFFF
    return value - (1 << 32) if value >= 1 << 31 else value


def _int64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value


def _tag(field: int, wire_type: int) -> bytes:
    return _varint(field << 3 | wire_type)


def _scalar(field: int, value: int) -> bytes:
    """Encode a proto3 integer field; zero is omitted, negatives take 10 bytes."""
    if value == 0:
        return b""
    return _tag(field, _VARINT) + _varint(value & _MASK64)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)
