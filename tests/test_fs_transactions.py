"""`StorageFS`-specific transaction behavior: per-path reclaim and symlink confinement.

The lifecycle every backend shares lives in `test_transactions.py`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from helpers.backends import make_setgid
from helpers.storage import commit_transaction, deadline

from txstore import (
    ObjectOperation,
    StorageException,
    StorageFS,
    StorageNotFoundException,
    Timestamp,
    TransactionModel,
)


@pytest.fixture
def storage(tmp_path: Path) -> StorageFS:
    return StorageFS(str(tmp_path / "repo"))


def test_transaction_forget_continues_past_a_failing_delete(
    storage: StorageFS, caplog: pytest.LogCaptureFixture
) -> None:
    """One stubborn footmark must not abort the reclaim of the others."""
    model = commit_transaction(storage, "a.deb")
    storage.put("b.deb", b"x")
    model2 = storage.transaction_begin()
    storage.put("b.deb", b"y")
    storage.transaction_commit()

    doomed = [model, model2]
    real_delete = storage._reclaim_footmark
    calls: list[str] = []

    def flaky(rel: str) -> None:
        calls.append(rel)
        if len(calls) == 1:
            raise OSError("device busy")
        real_delete(rel)

    with (
        caplog.at_level(logging.WARNING),
        patch.object(storage, "_reclaim_footmark", side_effect=flaky),
    ):
        removed = storage.transaction_forget(doomed)

    assert len(removed) == len(calls) - 1
    assert "Failed to remove footmark" in caplog.text


@pytest.mark.parametrize("alias", ["dot", "symlink", "hardlink"])
def test_move_onto_an_alias_of_itself_keeps_the_object(storage: StorageFS, alias: str) -> None:
    """A `./` segment, a symlink, or a hard link names the same file, so the move is a no-op."""
    storage.put("d/f.deb", b"data")
    if alias == "dot":
        src, dst = "d/f.deb", "d/./f.deb"
    elif alias == "symlink":
        Path(storage.base_dir, "d", "link.deb").symlink_to("f.deb")
        src, dst = "d/link.deb", "d/f.deb"
    else:
        Path(storage.base_dir, "d", "link.deb").hardlink_to(Path(storage.base_dir, "d", "f.deb"))
        src, dst = "d/link.deb", "d/f.deb"
    storage.transaction_begin()
    storage.move(src, dst)
    storage.transaction_commit()

    assert storage.get("d/f.deb") == b"data"
    # Copy-then-delete would also leave `d/f.deb`; only a no-op keeps the source name.
    assert os.path.lexists(Path(storage.base_dir, src))


@pytest.mark.parametrize("staged_on", ["src", "dst"])
def test_move_between_hard_links_after_a_staged_write_moves(
    storage: StorageFS, staged_on: str
) -> None:
    """A write staged on one name splits the pair, so the move is real, as outside one."""
    storage.put("d/a.deb", b"old")
    Path(storage.base_dir, "d", "b.deb").hardlink_to(Path(storage.base_dir, "d", "a.deb"))
    storage.transaction_begin()
    storage.put(f"d/{'a' if staged_on == 'src' else 'b'}.deb", b"new")
    expected = storage.get("d/a.deb")
    storage.move("d/a.deb", "d/b.deb")
    storage.transaction_commit()

    assert storage.get("d/b.deb") == expected
    assert not storage.exists("d/a.deb")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.add_metadata("d/f.deb", {"k": "v"}),
        lambda s: s.delete_metadata("d/f.deb"),
        lambda s: s.delete_metadata_fields("d/f.deb", ["k"]),
    ],
    ids=["add_metadata", "delete_metadata", "delete_metadata_fields"],
)
def test_metadata_change_of_a_delete_marked_object_is_refused(
    storage: StorageFS, mutate: Callable[[StorageFS], object]
) -> None:
    """A metadata verb must not stage a PUT that makes `list` resurrect a staged delete."""
    storage.put("d/f.deb", b"data")
    storage.transaction_begin()
    storage.delete("d/f.deb")
    with pytest.raises(StorageException, match="marked for deletion"):
        mutate(storage)

    assert not storage.exists("d/f.deb")
    assert storage.list() == []
    storage.transaction_discard()
    assert storage.get("d/f.deb") == b"data"


def test_move_onto_a_delete_marked_hard_link_moves_the_object(storage: StorageFS) -> None:
    """The destination's staged delete must not make the move a no-op that commit then empties."""
    storage.put("d/f.deb", b"data")
    Path(storage.base_dir, "d", "link.deb").hardlink_to(Path(storage.base_dir, "d", "f.deb"))
    storage.transaction_begin()
    storage.delete("d/f.deb")
    storage.move("d/link.deb", "d/f.deb")
    storage.transaction_commit()

    assert storage.get("d/f.deb") == b"data"
    assert not storage.exists("d/link.deb")


def test_move_onto_a_hard_linked_staged_object_keeps_the_object(storage: StorageFS) -> None:
    """Hard-linked PUT footmarks stand in for a case alias, which Linux CI cannot fold."""
    storage.transaction_begin()
    storage.put("a.deb", b"data")
    put = ObjectOperation.PUT
    a_put = storage._add_footmark(storage._obj_full("a.deb"), put)
    Path(storage._add_footmark(storage._obj_full("b.deb"), put)).hardlink_to(a_put)
    storage.move("a.deb", "b.deb")
    storage.transaction_commit()

    assert storage.get("a.deb") == b"data"


def _orphan_model() -> TransactionModel:
    """A transaction no history records: anyone who can write into the root can mint its tokens."""
    return TransactionModel(4242, Timestamp.from_json("2020-01-01T00:00:00Z"))


def _plant_victim(storage: StorageFS, tmp_path: Path, where: str) -> Path:
    """A file a planted link points at: outside the root, or a committed object inside it.

    Path confinement accepts the inside case, so the link check has to refuse it.
    """
    if where == "outside":
        victim = tmp_path / "outside" / "secret"
        victim.parent.mkdir()
        victim.write_bytes(b"keep me")
        return victim
    storage.put("pool/important.deb", b"keep me")
    return Path(storage.base_dir, "pool", "important.deb")


@contextmanager
def _swap_after_check(
    storage: StorageFS, monkeypatch: pytest.MonkeyPatch, footmark: Path, swap: Callable[[], None]
) -> Iterator[None]:
    """Replace `footmark` through `swap` right after the footmark check passes it, as a race would.

    Only `footmark` is swapped, so footmarks listed beside it still reach the step under test;
    the block fails unless the swap fired exactly once.
    """
    checked = storage._obj_checked_footmark
    swapped: list[str] = []

    def check_then_swap(rel: str) -> str:
        full = checked(rel)
        if os.path.basename(rel) == footmark.name:
            footmark.unlink()
            swap()
            swapped.append(rel)
        return full

    monkeypatch.setattr(storage, "_obj_checked_footmark", check_then_swap)
    yield
    assert len(swapped) == 1, swapped


@pytest.mark.parametrize("where", ["outside", "inside"])
def test_transaction_forget_orphans_never_follows_a_planted_symlink(
    storage: StorageFS, tmp_path: Path, caplog: pytest.LogCaptureFixture, where: str
) -> None:
    """A footmark name is mintable, so a sweep must unlink the link, not its target."""
    victim = _plant_victim(storage, tmp_path, where)
    # Anyone who can write into the repo can mint a token with the public API.
    token = _orphan_model().footmarks[ObjectOperation.PUT.value]
    Path(storage.base_dir).mkdir(parents=True, exist_ok=True)
    link = Path(storage.base_dir, f".attack.{token}")
    link.symlink_to(victim)

    # The sweep classifies it (the token decodes) but must refuse the delete.
    assert storage.find_orphaned_footmarks([]) == [link.name]
    with caplog.at_level(logging.WARNING):
        assert storage.transaction_forget_orphans([]) == []

    assert victim.read_bytes() == b"keep me"
    assert link.is_symlink()
    if where == "inside":
        assert "Refusing non-regular footmark" in caplog.text


@pytest.mark.parametrize("where", ["outside", "inside"])
def test_discard_never_follows_a_planted_symlink(
    storage: StorageFS, tmp_path: Path, where: str
) -> None:
    """Discard deletes the same listed paths a reclaim does, so it is confined the same way."""
    victim = _plant_victim(storage, tmp_path, where)
    model = storage.transaction_begin()
    storage.put("pool/a.deb", b"data")
    # The live transaction's tokens sit on its own hidden objects, so a writer
    # watching the repository can copy one onto a link before the discard.
    link = Path(storage.base_dir, f".attack.{model.footmarks[ObjectOperation.PUT.value]}")
    link.symlink_to(victim)

    with pytest.raises(StorageException, match=r"\.attack\.") as info:
        storage.transaction_discard()

    assert victim.read_bytes() == b"keep me"
    assert link.is_symlink()
    if where == "inside":
        assert "Refusing non-regular footmark" in str(info.value)


@pytest.mark.parametrize("reclaim", ["forget-orphans", "discard"])
def test_a_footmark_swapped_for_a_symlink_after_the_check_is_unlinked_not_followed(
    storage: StorageFS, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reclaim: str
) -> None:
    """The check and the delete are two calls, so `_obj_delete` must unlink the link itself."""
    victim = _plant_victim(storage, tmp_path, "outside")
    if reclaim == "discard":
        model = storage.transaction_begin()
    else:
        model = _orphan_model()
    Path(storage.base_dir).mkdir(parents=True, exist_ok=True)
    footmark = Path(storage.base_dir, f".swapped.{model.footmarks[ObjectOperation.PUT.value]}")
    footmark.write_bytes(b"")
    with _swap_after_check(storage, monkeypatch, footmark, partial(footmark.symlink_to, victim)):
        if reclaim == "discard":
            storage.transaction_discard()
        else:
            assert storage.transaction_forget_orphans([]) == [footmark.name]

    assert victim.read_bytes() == b"keep me"
    assert not os.path.lexists(footmark)


def test_transaction_forget_orphans_refuses_a_footmark_it_cannot_inspect(
    storage: StorageFS, caplog: pytest.LogCaptureFixture
) -> None:
    """An `lstat` failure other than a missing file is a refusal, never a footmark gone."""
    token = _orphan_model().footmarks[ObjectOperation.PUT.value]
    Path(storage.base_dir).mkdir(parents=True, exist_ok=True)
    Path(storage.base_dir, "d").write_bytes(b"keep")  # a file where a directory is named: ENOTDIR
    orphan = f"d/.x.{token}"

    with caplog.at_level(logging.WARNING):
        assert storage.transaction_forget_orphans([], orphans=[orphan]) == []

    assert f"Refusing non-regular footmark: {orphan}" in caplog.text


def test_transaction_forget_counts_a_footmark_gone_since_the_listing_as_removed(
    storage: StorageFS, caplog: pytest.LogCaptureFixture
) -> None:
    """A vanished footmark is reclaimed, not reported as resisting removal → STORAGE.md."""
    model = commit_transaction(storage, "a.deb")
    paths = storage.find_transaction_footmarks([model])
    Path(storage.base_dir, paths[0]).unlink()

    with caplog.at_level(logging.WARNING):
        assert storage.transaction_forget([model], paths=paths) == sorted(paths)

    assert "Failed to remove footmark" not in caplog.text


@pytest.mark.parametrize("where", ["outside", "inside"])
def test_commit_never_follows_a_planted_delete_footmark_link(
    storage: StorageFS, tmp_path: Path, where: str
) -> None:
    """The original is named by the listed footmark, never by the link's target."""
    victim = _plant_victim(storage, tmp_path, where)
    model = storage.transaction_begin()
    link = Path(storage.base_dir, f".attack.{model.footmarks[ObjectOperation.DELETE.value]}")
    link.symlink_to(victim)

    storage.transaction_commit()

    assert victim.read_bytes() == b"keep me"


# Each case returns the token to plant at, the call that must refuse it, and the live `a.txt` the
# refusal leaves: copy sources are checked before the first write, so that is the pre-call object
# or nothing, never the PUT staged in a footmark.
_CopyCase = Callable[[StorageFS], tuple[str, Callable[[], object], bytes | None]]


def _commit_put(storage: StorageFS) -> tuple[str, Callable[[], object], bytes | None]:
    """Commit copies a PUT footmark onto its original."""
    model = storage.transaction_begin()
    storage.put("a.txt", b"new")
    return model.footmarks[ObjectOperation.PUT.value], storage.transaction_commit, None


def _commit_backup(storage: StorageFS) -> tuple[str, Callable[[], object], bytes | None]:
    """Commit only checks a BACKUP footmark, so its revert can restore it."""
    storage.put("a.txt", b"old")
    model = storage.transaction_begin()
    storage.put("a.txt", b"new")
    return model.footmarks[ObjectOperation.BACKUP.value], storage.transaction_commit, b"old"


def _revert_backup(storage: StorageFS) -> tuple[str, Callable[[], object], bytes | None]:
    """Revert copies a BACKUP footmark onto its original."""
    storage.put("a.txt", b"old")
    model = commit_transaction(storage, "a.txt", b"new")
    token = model.footmarks[ObjectOperation.BACKUP.value]
    return token, partial(storage.transaction_revert, model), b"new"


_COPY_CASES = [
    pytest.param(_commit_put, id="commit-put"),
    pytest.param(_commit_backup, id="commit-backup"),
    pytest.param(_revert_backup, id="revert-backup"),
]
# An outside target fails path confinement; an inside one the regular-file check.
_REFUSED_FOOTMARK = r"non-regular footmark: \.stolen\.txt\."
_LINK_REFUSALS = {"outside": "resolves outside storage root", "inside": _REFUSED_FOOTMARK}


def _read(storage: StorageFS, rel: str) -> bytes | None:
    path = Path(storage.base_dir, rel)
    return path.read_bytes() if path.exists() else None


@pytest.mark.parametrize("case", _COPY_CASES)
def test_a_footmark_gone_since_the_listing_is_refused(
    storage: StorageFS, monkeypatch: pytest.MonkeyPatch, case: _CopyCase
) -> None:
    """A missing copy source is a refusal, never the caller's own object reported missing."""
    token, apply, live = case(storage)
    categorize = storage._categorize

    def vanish_after_listing(footmarks: Sequence[str] | None = None) -> Any:
        cat = categorize(footmarks)
        Path(storage.base_dir, f".a.txt.{token}").unlink()
        return cat

    monkeypatch.setattr(storage, "_categorize", vanish_after_listing)
    with pytest.raises(StorageException, match="Footmark gone since the listing") as info:
        apply()

    assert not isinstance(info.value, StorageNotFoundException)
    assert _read(storage, "a.txt") == live


@pytest.mark.parametrize("where", ["outside", "inside"])
@pytest.mark.parametrize("case", _COPY_CASES)
def test_a_planted_footmark_link_is_never_copied_from(
    storage: StorageFS, tmp_path: Path, where: str, case: _CopyCase
) -> None:
    """A link at a footmark commit or revert copies from would copy its target."""
    victim = _plant_victim(storage, tmp_path, where)
    token, apply, live = case(storage)
    Path(storage.base_dir, f".stolen.txt.{token}").symlink_to(victim)

    with pytest.raises(StorageException, match=_LINK_REFUSALS[where]):
        apply()

    assert _read(storage, "stolen.txt") is None
    assert _read(storage, "a.txt") == live
    assert victim.read_bytes() == b"keep me"


@pytest.mark.parametrize("case", _COPY_CASES)
def test_a_planted_fifo_footmark_is_never_opened(storage: StorageFS, case: _CopyCase) -> None:
    """Opening a FIFO to copy from it would block the commit or revert forever."""
    token, apply, live = case(storage)
    os.mkfifo(Path(storage.base_dir, f".stolen.txt.{token}"))

    with deadline(5), pytest.raises(StorageException, match=_REFUSED_FOOTMARK):
        apply()

    assert _read(storage, "a.txt") == live


@pytest.mark.parametrize(
    "case",
    [pytest.param(_commit_put, id="commit-put"), pytest.param(_revert_backup, id="revert-backup")],
)
def test_a_footmark_swapped_for_a_fifo_after_the_check_is_never_opened(
    storage: StorageFS, monkeypatch: pytest.MonkeyPatch, case: _CopyCase
) -> None:
    """The path check and the copy's open are two calls; a FIFO between them must not hang.

    The swap is caught at the open, inside the copy loop, so copies listed earlier may already
    have landed: `a.txt` is not asserted.
    """
    token, apply, _ = case(storage)
    footmark = Path(storage.base_dir, f".stolen.txt.{token}")
    footmark.write_bytes(b"")
    swap = partial(os.mkfifo, footmark)
    with _swap_after_check(storage, monkeypatch, footmark, swap), deadline(5):
        with pytest.raises(StorageException, match="Not a regular file"):
            apply()

    assert _read(storage, "stolen.txt") is None


def test_transaction_forget_orphans_refuses_a_planted_fifo_footmark(
    storage: StorageFS, caplog: pytest.LogCaptureFixture
) -> None:
    """A sweep deletes only regular files, as commit and revert copy only from them."""
    token = _orphan_model().footmarks[ObjectOperation.PUT.value]
    Path(storage.base_dir).mkdir(parents=True, exist_ok=True)
    fifo = Path(storage.base_dir, f".attack.{token}")
    os.mkfifo(fifo)

    with caplog.at_level(logging.WARNING):
        assert storage.transaction_forget_orphans([]) == []

    assert "Refusing non-regular footmark" in caplog.text
    assert fifo.exists()


def test_discard_refuses_a_planted_fifo_footmark(storage: StorageFS) -> None:
    """Discard deletes the same listed paths a sweep does, so it refuses the same files."""
    token = storage.transaction_begin().footmarks[ObjectOperation.PUT.value]
    Path(storage.base_dir).mkdir(parents=True, exist_ok=True)
    fifo = Path(storage.base_dir, f".attack.{token}")
    os.mkfifo(fifo)

    with pytest.raises(StorageException, match=r"non-regular footmark: \.attack\."):
        storage.transaction_discard()

    assert fifo.exists()


def test_delete_refuses_a_fifo_planted_at_its_delete_marker(storage: StorageFS) -> None:
    """The DELETE marker name derives from the live tokens; a FIFO there must not hang `delete`."""
    storage.put("a.txt", b"old")
    model = storage.transaction_begin()
    os.mkfifo(Path(storage.base_dir, f".a.txt.{model.footmarks[ObjectOperation.DELETE.value]}"))

    with deadline(5), pytest.raises(StorageException, match="DELETE marker"):
        storage.delete("a.txt")


def test_commit_lands_a_planted_setuid_footmark_without_special_bits(storage: StorageFS) -> None:
    """A footmark name is mintable, so promoting one must not hand out the txstore uid."""
    model = storage.transaction_begin()
    Path(storage.base_dir).mkdir(parents=True, exist_ok=True)
    footmark = Path(storage.base_dir, f".target.{model.footmarks[ObjectOperation.PUT.value]}")
    footmark.write_bytes(b"")  # Empty, so no write after the copy's chmod clears the bit.
    footmark.chmod(0o4755)

    storage.transaction_commit()

    assert Path(storage.base_dir, "target").stat().st_mode & 0o7777 == 0o755


def test_commit_refuses_a_delete_footmark_whose_original_leaves_the_root(
    storage: StorageFS, tmp_path: Path
) -> None:
    victim = _plant_victim(storage, tmp_path, "outside")
    Path(storage.base_dir, "escape").symlink_to(victim)
    model = storage.transaction_begin()
    Path(storage.base_dir, f".escape.{model.footmarks[ObjectOperation.DELETE.value]}").touch()

    with pytest.raises(StorageException, match="resolves outside storage root"):
        storage.transaction_commit()

    assert victim.read_bytes() == b"keep me"


def test_commit_refuses_a_put_footmark_for_a_reserved_object(storage: StorageFS) -> None:
    storage.put("a.txt", b"a")
    model = storage.transaction_begin()
    token = model.footmarks[ObjectOperation.PUT.value]
    Path(storage.base_dir, f"..transactions.json.{token}").write_bytes(b"forged")

    with pytest.raises(StorageException, match="reserved"):
        storage.transaction_commit()

    assert not Path(storage.base_dir, ".transactions.json").exists()


def test_revert_refuses_a_put_footmark_whose_original_leaves_the_root(
    storage: StorageFS, tmp_path: Path
) -> None:
    victim = _plant_victim(storage, tmp_path, "outside")
    model = commit_transaction(storage, "a.txt")
    Path(storage.base_dir, "escape").symlink_to(victim)
    Path(storage.base_dir, f".escape.{model.footmarks[ObjectOperation.PUT.value]}").touch()

    with pytest.raises(StorageException, match="resolves outside storage root"):
        storage.transaction_revert(model)

    assert victim.read_bytes() == b"keep me"


def test_transaction_forget_orphans_reclaims_real_footmarks(storage: StorageFS) -> None:
    """The symlink refusal must not block an ordinary nested footmark."""
    token = _orphan_model().footmarks[ObjectOperation.PUT.value]
    nested = Path(storage.base_dir, "pool", "sub", f".real.{token}")
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_bytes(b"")

    assert storage.transaction_forget_orphans([]) == ["pool/sub/" + nested.name]
    assert not nested.exists()


def test_find_orphaned_footmarks_spares_reserved_and_foreign_dotfiles(storage: StorageFS) -> None:
    """Only decodable footmark tokens are orphans; other dotfiles are untouchable."""
    recorded = commit_transaction(storage, "kept.deb")
    Path(storage.base_dir, ".transactions.json").write_bytes(b"[]")
    Path(storage.base_dir, ".transactions.lock").write_bytes(b"")
    Path(storage.base_dir, ".hidden.txt").write_bytes(b"mine")

    assert storage.find_orphaned_footmarks([recorded.footmarks]) == []


def test_lock_file_planted_as_a_symlink_is_refused(storage: StorageFS, tmp_path: Path) -> None:
    """The flock must never land on a file outside the root (VALIDATION.md#filename-safety)."""
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    (Path(storage.base_dir) / ".transactions.lock").symlink_to(victim)

    with pytest.raises(StorageException, match="Failed to open history lock"):
        with storage._obj_lock():
            pass
    assert victim.read_bytes() == b"keep"


def test_list_hides_a_temp_file_left_by_a_crashed_write(storage: StorageFS) -> None:
    storage.put("pool/a.deb", b"a")
    leftover = Path(storage.base_dir) / "pool" / f".a.deb.tmp.{'0' * 16}"
    leftover.write_bytes(b"partial")
    (Path(storage.base_dir) / "pool" / "notes.tmp.txt").write_bytes(b"kept")

    assert sorted(storage.list()) == ["pool/a.deb", "pool/notes.tmp.txt"]


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "in-transaction"])
def test_a_key_shaped_like_a_temp_file_is_listed(storage: StorageFS, in_transaction: bool) -> None:
    """Temp files are dotfiles, so no object key collides with one."""
    key = f"a.txt.tmp.{'0' * 16}"
    if in_transaction:
        storage.transaction_begin()
    storage.put(key, b"v")

    assert storage.list() == [key]
    if in_transaction:
        storage.transaction_commit()
        assert storage.list() == [key]


@pytest.mark.parametrize("in_transaction", [False, True], ids=["direct", "in-transaction"])
@pytest.mark.parametrize("operation", ["put", "copy", "move"])
def test_parent_that_cannot_be_created_maps_to_storage_exception(
    storage: StorageFS, operation: str, in_transaction: bool
) -> None:
    """A regular file in place of a parent directory never escapes as a raw `OSError`."""
    storage.put("a.deb", b"a")
    (Path(storage.base_dir) / "pool").write_bytes(b"not a directory")
    if in_transaction:
        storage.transaction_begin()
    calls = {
        "put": lambda: storage.put("pool/sub/x.deb", b"x"),
        "copy": lambda: storage.copy("a.deb", "pool/sub/x.deb"),
        "move": lambda: storage.move("a.deb", "pool/sub/x.deb"),
    }

    with pytest.raises(
        StorageException, match="Failed to create parent of pool/sub/x.deb"
    ) as raised:
        calls[operation]()

    assert isinstance(raised.value.__cause__, OSError)


@pytest.mark.parametrize(
    ("in_transaction", "mode"),
    [pytest.param(False, 0o2755, id="native-rename"), pytest.param(True, 0o755, id="staged")],
)
def test_move_keeps_attributes_only_as_a_native_rename(
    storage: StorageFS, in_transaction: bool, mode: int
) -> None:
    """A rename moves the inode with its bits; a staged move is a copy, which sanitizes them."""
    storage.put("a.txt", b"")
    make_setgid(Path(storage.base_dir, "a.txt"))
    if in_transaction:
        storage.transaction_begin()
    storage.move("a.txt", "b.txt")
    if in_transaction:
        storage.transaction_commit()

    assert Path(storage.base_dir, "b.txt").stat().st_mode & 0o7777 == mode
