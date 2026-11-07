"""Tests for txstore/transaction.py: the model, the capability discriminator, and the
`maybe_transaction` wrapper (STORAGE.md#transactions, STORAGE.md#failure-semantics).

The shared lifecycle, parametrized over every transactional backend, lives in
`test_transactions.py`; backend-specific cases in `test_fs_transactions.py` /
`test_s3_transactions.py`; history recording in `test_history.py`.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest
from helpers.storage import TxnStorageStub

from txstore import (
    ObjectOperation,
    StorageNull,
    Timestamp,
    TransactionModel,
    is_transactional,
    maybe_transaction,
)


def _ts(seconds: int = 1_760_000_000, nanos: int = 123456789) -> Timestamp:
    return Timestamp(seconds=seconds, nanos=nanos)


# --- TransactionModel ---------------------------------------------------------------------------


def test_model_is_a_pure_function_of_id_and_timestamp() -> None:
    """Revert rebuilds the model from `{id, timestamp}` alone, so it must be bit-identical."""
    rebuilt = TransactionModel(7, _ts())
    assert rebuilt == TransactionModel(7, _ts())
    assert rebuilt.footmarks == TransactionModel(7, _ts()).footmarks


def test_model_yields_one_token_per_object_operation() -> None:
    model = TransactionModel(7, _ts())

    assert len(model.footmarks) == len(ObjectOperation)
    assert len(set(model.footmarks)) == len(ObjectOperation)


def test_model_is_frozen() -> None:
    """The handle `transaction_begin` returns is the live transaction; nobody may change it."""
    model = TransactionModel(7, _ts())
    with pytest.raises(dataclasses.FrozenInstanceError):
        model.id = 8  # type: ignore[misc]
    assert isinstance(model.footmarks, tuple)


def test_model_distinguishes_transaction_ids() -> None:
    assert TransactionModel(7, _ts()).footmarks != TransactionModel(8, _ts()).footmarks


@pytest.mark.parametrize(
    "txn_id",
    [0, -1, 2**31, 2**64 + 1, True],
    ids=["0", "negative", "int32+1", "wraps-to-1", "bool"],
)
def test_model_refuses_an_id_its_tokens_cannot_carry(txn_id: int) -> None:
    """Out-of-int32 ids write tokens no sweep parses back; `2**64 + 1` and `True` alias id 1."""
    with pytest.raises(ValueError, match="Transaction id out of range"):
        TransactionModel(txn_id, _ts())


def test_model_distinguishes_timestamps() -> None:
    early = TransactionModel(7, _ts(nanos=1))
    late = TransactionModel(7, _ts(nanos=2))

    assert early.footmarks != late.footmarks


# --- the capability discriminator ---------------------------------------------------------------


def test_is_transactional_accepts_a_footmark_backend() -> None:
    assert is_transactional(TxnStorageStub()) is True


def test_is_transactional_rejects_a_capability_stub_backend() -> None:
    """`TransactionNotSupportedMixin` only raises `NotImplementedError`, so it is not capable."""
    assert is_transactional(StorageNull()) is False


def test_is_transactional_rejects_an_unrelated_object() -> None:
    assert is_transactional(object()) is False


# --- maybe_transaction --------------------------------------------------------------------------


def test_maybe_transaction_is_a_no_op_for_a_non_transactional_backend() -> None:
    storage = StorageNull()

    with maybe_transaction(storage):
        ran = True

    assert ran is True


def test_maybe_transaction_commits_a_successful_body() -> None:
    storage = TxnStorageStub()

    with maybe_transaction(storage):
        pass

    assert (storage.began, storage.committed, storage.discarded) == (True, True, False)


def test_maybe_transaction_discards_on_an_exception() -> None:
    storage = TxnStorageStub()

    with pytest.raises(RuntimeError, match="boom"), maybe_transaction(storage):
        raise RuntimeError("boom")

    assert (storage.committed, storage.discarded) == (False, True)


def test_maybe_transaction_discards_on_a_base_exception() -> None:
    """A `KeyboardInterrupt` mid-write must not leave footmarks staged."""
    storage = TxnStorageStub()

    with pytest.raises(KeyboardInterrupt), maybe_transaction(storage):
        raise KeyboardInterrupt

    assert storage.discarded is True


def test_a_failing_discard_does_not_mask_the_original_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _BrokenDiscard(TxnStorageStub):
        def transaction_discard(self) -> None:
            raise OSError("backend gone")

    with caplog.at_level(logging.ERROR), pytest.raises(ValueError, match="original"):
        with maybe_transaction(_BrokenDiscard()):
            raise ValueError("original")

    assert "Failed to discard transaction during rollback" in caplog.text
