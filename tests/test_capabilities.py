"""Coverage for `txstore._capabilities` mixins."""

from __future__ import annotations

import inspect
from io import BytesIO

import pytest

from txstore import FootmarkMixin, Storage
from txstore._capabilities import (
    ReadOnlyStorageMixin,
    TransactionNotSupportedMixin,
    WriteOnlyStorageMixin,
)


class _RO(ReadOnlyStorageMixin):
    pass


class _WO(WriteOnlyStorageMixin):
    pass


class _NoTxn(TransactionNotSupportedMixin):
    pass


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("put", ("p", b"x")),
        ("put_stream", ("p", BytesIO(b"x"))),
        ("add_metadata", ("p", {"k": "v"})),
        ("delete", ("p",)),
        ("delete_metadata", ("p",)),
        ("delete_metadata_fields", ("p", ["k"])),
        ("copy", ("a", "b")),
        ("move", ("a", "b")),
        ("format", ()),
        ("mkdir", ("p",)),
        ("rmdir", ("p",)),
    ],
)
def test_read_only_rejects_writes(method: str, args: tuple[object, ...]) -> None:
    with pytest.raises(NotImplementedError):
        getattr(_RO(), method)(*args)


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("list", ()),
        ("get", ("p",)),
        ("get_metadata", ("p",)),
        ("exists", ("p",)),
        ("get_stream", ("p",)),
        ("copy", ("a", "b")),
        ("move", ("a", "b")),
        ("mkdir", ("p",)),
    ],
)
def test_write_only_rejects_reads(method: str, args: tuple[object, ...]) -> None:
    with pytest.raises(NotImplementedError):
        getattr(_WO(), method)(*args)


def test_transaction_not_supported_mixin_is_never_in_a_transaction() -> None:
    assert _NoTxn().in_transaction() is False


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("transaction_begin", ()),
        ("transaction_commit", ()),
        ("transaction_revert", (None,)),
        ("transaction_discard", ()),
    ],
)
def test_transaction_not_supported_mixin(method: str, args: tuple[object, ...]) -> None:
    with pytest.raises(NotImplementedError):
        getattr(_NoTxn(), method)(*args)


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("hidden_objects", ()),
        ("find_transaction_footmarks", ([],)),
        ("transaction_forget", ([],)),
        ("find_orphaned_footmarks", ([],)),
        ("transaction_forget_orphans", ([],)),
    ],
)
def test_transaction_not_supported_rejects_reclaim(method: str, args: tuple[object, ...]) -> None:
    with pytest.raises(NotImplementedError):
        getattr(_NoTxn(), method)(*args)


def _public_methods(cls: type) -> dict[str, list[str]]:
    """Public callables defined on *cls* or a base, mapped to their parameter names."""
    return {
        name: list(inspect.signature(member).parameters)
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def test_transaction_stub_names_every_transactional_method() -> None:
    """A method missing from the stub raises `AttributeError` on a backend without
    transactions, where the contract asks for `NotImplementedError` → STORAGE.md#backends."""
    mixin = {
        name: params
        for name, params in _public_methods(FootmarkMixin).items()
        if name not in _public_methods(Storage)
    }
    assert _public_methods(TransactionNotSupportedMixin) == mixin
