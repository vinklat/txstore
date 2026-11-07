"""`txstore._footmark_wire`: byte compatibility with the protobuf wire format.

The vectors were produced once by a protobuf runtime from `docs/reference/footmark.proto`; footmark
objects already in storage hold these bytes, so they must never change.
"""

from __future__ import annotations

import pytest

from txstore._footmark_wire import FootmarkFields, decode, encode

# (header, seconds, nanos, transaction_id, operation, serialized hex)
WIRE = [
    (1, 0, 0, 0, 0, "08011200"),
    (1, 1767225600, 0, 1, 0, "080112060880f2d6ca061801"),
    (1, 1767225600, 0, 1, 1, "080112060880f2d6ca0618012001"),
    (1, 1767225600, 0, 1, 2, "080112060880f2d6ca0618012002"),
    (1, 1767225600, 0, 1, 3, "080112060880f2d6ca0618012003"),
    (1, 1760000000, 123456789, 7, 2, "0801120b0880f09dc70610959aef3a18072002"),
    (1, 1760000000, 123000000, 2147483647, 3, "0801120b0880f09dc70610c0a9d33a18ffffffff072003"),
    (-1, 1767225600, 0, 42, 1, "08ffffffffffffffffff0112060880f2d6ca06182a2001"),
    (1, -62135596800, 0, 5, 2, "0801120b088092b8c398feffffff0118052002"),
    (1, 253402300799, 999999999, 300, 0, "0801120d08ff82d1ffaf0710ff93ebdc0318ac02"),
    (0, 0, 0, 0, 0, "1200"),
]

# (serialized hex, decoded fields); None means the encoding is rejected.
DECODE = [
    pytest.param("", (0, 0, 0, 0, 0), id="empty"),
    pytest.param("0801", (1, 0, 0, 0, 0), id="no-timestamp"),
    pytest.param("08011a00", (1, 0, 0, 0, 0), id="known-field-wrong-wire-type"),
    pytest.param("08012a0161", (1, 0, 0, 0, 0), id="unknown-field"),
    pytest.param("0801290000000000000000", (1, 0, 0, 0, 0), id="unknown-fixed64"),
    pytest.param("08012d00000000", (1, 0, 0, 0, 0), id="unknown-fixed32"),
    pytest.param("120418010801", (0, 1, 0, 0, 0), id="unknown-field-in-timestamp"),
    pytest.param("0801220100", (1, 0, 0, 0, 0), id="enum-wrong-wire-type"),
    pytest.param("0801200a", (1, 0, 0, 0, 10), id="unknown-enum-value-kept"),
    pytest.param("080110ff", None, id="truncated-varint"),
    pytest.param("08011202", None, id="truncated-length"),
    pytest.param("0001", None, id="field-zero"),
    pytest.param("88808080800100", None, id="tag-over-5-bytes"),
    pytest.param("888080801000", None, id="tag-over-32-bits"),
    pytest.param("0b0c", None, id="group"),
    pytest.param("0e00", None, id="wire-type-6"),
    pytest.param("08" + "ff" * 10 + "01", None, id="varint-over-10-bytes"),
]


@pytest.mark.parametrize(
    ("header", "seconds", "nanos", "transaction_id", "operation", "hex_"), WIRE
)
def test_encode_matches_protobuf(
    header: int, seconds: int, nanos: int, transaction_id: int, operation: int, hex_: str
) -> None:
    fields = FootmarkFields(header, seconds, nanos, transaction_id, operation)
    assert encode(fields).hex() == hex_


@pytest.mark.parametrize(
    ("header", "seconds", "nanos", "transaction_id", "operation", "hex_"), WIRE
)
def test_decode_matches_protobuf(
    header: int, seconds: int, nanos: int, transaction_id: int, operation: int, hex_: str
) -> None:
    assert decode(bytes.fromhex(hex_)) == (header, seconds, nanos, transaction_id, operation)


@pytest.mark.parametrize(("hex_", "expected"), DECODE)
def test_decode_edge_cases(hex_: str, expected: tuple[int, ...] | None) -> None:
    if expected is None:
        with pytest.raises(ValueError, match="footmark"):
            decode(bytes.fromhex(hex_))
    else:
        assert decode(bytes.fromhex(hex_)) == expected


def test_decode_last_scalar_wins_and_timestamps_merge() -> None:
    data = bytes.fromhex("080108021202080112021005")
    assert decode(data) == FootmarkFields(header=2, seconds=1, nanos=5)


def test_decode_truncates_int32_fields() -> None:
    """A varint above 32 bits keeps its low 32 bits, read as signed."""
    data = bytes.fromhex("18ffffffff1f")  # 2**33 - 1
    assert decode(data).transaction_id == -1
