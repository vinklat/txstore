"""Tests for txstore/metrics.py: the per-instance counters STORAGE.md#storage-abi documents."""

from __future__ import annotations

import pytest

from txstore import StorageMetrics

_KEYS = (
    "read_objects_total",
    "read_bytes",
    "read_errors_total",
    "write_objects_total",
    "write_bytes",
    "write_errors_total",
    "delete_objects_total",
)


def test_a_fresh_instance_starts_at_zero() -> None:
    assert StorageMetrics().dump() == dict.fromkeys(_KEYS, 0)


def test_dump_exposes_every_documented_counter() -> None:
    assert tuple(StorageMetrics().dump()) == _KEYS


@pytest.mark.parametrize(
    ("method", "key"),
    [
        ("read_objects_inc", "read_objects_total"),
        ("read_errors_inc", "read_errors_total"),
        ("write_objects_inc", "write_objects_total"),
        ("write_errors_inc", "write_errors_total"),
        ("delete_objects_inc", "delete_objects_total"),
    ],
)
def test_counter_increments_by_one(method: str, key: str) -> None:
    metrics = StorageMetrics()

    getattr(metrics, method)()
    getattr(metrics, method)()

    assert metrics.dump()[key] == 2


@pytest.mark.parametrize(
    ("method", "key"), [("read_bytes_inc", "read_bytes"), ("write_bytes_inc", "write_bytes")]
)
def test_byte_counter_adds_the_given_size(method: str, key: str) -> None:
    metrics = StorageMetrics()

    getattr(metrics, method)(1024)
    getattr(metrics, method)(1)

    assert metrics.dump()[key] == 1025


def test_incrementing_one_counter_leaves_the_others_alone() -> None:
    metrics = StorageMetrics()

    metrics.read_objects_inc()

    assert metrics.dump() == {**dict.fromkeys(_KEYS, 0), "read_objects_total": 1}


def test_error_and_success_counters_stay_disjoint() -> None:
    """A failed read bumps only the error counter, never the object counter."""
    metrics = StorageMetrics()

    metrics.read_errors_inc()

    assert metrics.dump()["read_objects_total"] == 0
    assert metrics.dump()["read_errors_total"] == 1


def test_counters_are_per_instance() -> None:
    first, second = StorageMetrics(), StorageMetrics()

    first.write_objects_inc()

    assert second.dump()["write_objects_total"] == 0


def test_repr_reports_every_counter() -> None:
    metrics = StorageMetrics()
    metrics.write_bytes_inc(7)

    text = repr(metrics)

    assert text.startswith("StorageMetrics(")
    assert "write_bytes=7" in text
    assert all(key in text for key in _KEYS)
