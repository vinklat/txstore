"""Hash helpers in `txstore.hexdigest`."""

from __future__ import annotations

import io

import pytest

from txstore import (
    SUPPORT_HASH_LIST,
    ChecksumOpen,
    calc_digests,
    compare_digests,
    normalize_digests,
)


def test_calc_digests_returns_all_algos() -> None:
    d = calc_digests(b"hello")
    assert set(d) == {"MD5Sum", "SHA1", "SHA256", "SHA512", "Size"}
    assert d["Size"] == "5"


def test_calc_digests_matches_the_known_answers() -> None:
    """Each key carries its own algorithm, not merely a consistent one."""
    assert calc_digests(b"abc") == {
        "MD5Sum": "900150983cd24fb0d6963f7d28e17f72",
        "SHA1": "a9993e364706816aba3e25717850c26c9cd0d89d",
        "SHA256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        "SHA512": (
            "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
            "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f"
        ),
        "Size": "3",
    }


def test_checksum_open_read_matches_calc_digests() -> None:
    data = b"the quick brown fox" * 10
    buf = io.BytesIO(data)
    co = ChecksumOpen(buf)
    while co.read():
        pass
    assert co.get_digests() == calc_digests(data)


def test_checksum_open_close_closes_the_wrapped_file_once() -> None:
    buf = io.BytesIO(b"data")
    co = ChecksumOpen(buf)
    co.close()
    assert buf.closed
    assert co.closed
    co.close()  # a second close is a no-op


def test_checksum_open_read_without_size_reads_to_end() -> None:
    data = b"x" * (3 * 1024 * 1024)
    co = ChecksumOpen(io.BytesIO(data))
    assert co.read() == data
    assert co.read() == b""


def test_checksum_open_write_matches_calc_digests() -> None:
    data = b"streamed payload" * 8
    sink = io.BytesIO()
    co = ChecksumOpen(sink)
    co.write(data)
    sink.seek(0)
    assert sink.read() == data
    assert co.get_digests() == calc_digests(data)


def test_compare_digests_match() -> None:
    d = calc_digests(b"x")
    assert compare_digests(d, d) is True


def test_compare_digests_mismatch() -> None:
    a = calc_digests(b"x")
    b = calc_digests(b"y")
    assert compare_digests(a, b) is False


def test_compare_digests_empty_returns_false() -> None:
    assert compare_digests({}, calc_digests(b"x")) is False
    assert compare_digests(calc_digests(b"x"), {}) is False


def test_compare_digests_partial_overlap_match() -> None:
    a = calc_digests(b"x")
    b = {"SHA256": a["SHA256"]}
    assert compare_digests(a, b) is True


def test_compare_digests_partial_overlap_mismatch() -> None:
    a = calc_digests(b"x")
    b = {"SHA256": "deadbeef"}
    assert compare_digests(a, b) is False


@pytest.mark.parametrize("alg", SUPPORT_HASH_LIST)
def test_compare_digests_one_mismatched_algorithm_refuses(alg: str) -> None:
    """Every shared digest must agree, wherever the mismatch falls in the comparison order."""
    a = calc_digests(b"x")
    assert compare_digests(a, {**a, alg: "deadbeef"}) is False


def test_compare_digests_md5_alone_never_matches() -> None:
    """A shared MD5 is not enough: some other algorithm must agree."""
    a = calc_digests(b"x")
    assert compare_digests({"MD5Sum": a["MD5Sum"]}, a) is False


def test_compare_digests_mismatched_md5_refuses_an_agreeing_sha() -> None:
    a = calc_digests(b"x")
    assert compare_digests({"SHA256": a["SHA256"], "MD5Sum": "deadbeef"}, a) is False


def test_normalize_digests_case_folds_and_filters() -> None:
    raw = {"sha256": "abc", "sha1": "def", "unknown": "xyz"}
    out = normalize_digests(raw)
    assert out == {"SHA256": "abc", "SHA1": "def"}


def test_normalize_digests_drops_md5() -> None:
    """MD5 never decides a skip or a verify on its own, whatever case it arrives in."""
    assert normalize_digests({"MD5Sum": "a", "md5sum": "a", "SHA256": "b"}) == {"SHA256": "b"}
    assert normalize_digests({"MD5Sum": "a"}) == {}
