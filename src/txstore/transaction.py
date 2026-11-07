"""Transaction support for storage backends.

`FootmarkMixin` owns the transaction algorithm: the lifecycle (`transaction_begin` / `commit` /
`discard` / `revert`) and the overlay that reads and writes go through while a transaction is
active. Footmark reclamation lives in `txstore._reclaim.ReclaimMixin`, which it inherits. A
backend gains transactions by inheriting `FootmarkMixin` and implementing the backend
primitives → [STORAGE.md](STORAGE.md#backend-primitives).

`is_transactional` is the capability test; `txstore.history.maybe_transaction` wraps a block
of writes in one recorded transaction → [STORAGE.md](STORAGE.md#failure-semantics).
"""

from __future__ import annotations

import logging
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple, TypeIs, final

from txstore._reclaim import ReclaimMixin
from txstore._reserved import check_reserved_path
from txstore.base import StorageException, StorageNotFoundException
from txstore.footmark import (
    ObjectOperation,
    build_footmarks,
    footmark_path,
    is_transaction_id,
    now_timestamp,
    split_footmark_path,
)

if TYPE_CHECKING:
    from txstore._timestamp import Timestamp
    from txstore.metrics import StorageMetrics

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


@dataclass(frozen=True, slots=True)
class TransactionModel:
    """One transaction's identity, as `transaction_begin` returns it and history records it.

    The footmark tokens are a pure function of ``(id, timestamp)``, so a model rebuilt from a
    history record equals the one returned at begin → [STORAGE.md](STORAGE.md#footmarks).

    Attributes:
        id: Transaction identifier embedded in every footmark.
        timestamp: Begin timestamp shared by every footmark token.
        footmarks: Serialised footmark tokens, one per `ObjectOperation`, indexed by its value.
    """

    id: int
    timestamp: Timestamp
    footmarks: tuple[str, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Derive the footmark tokens.

        Raises:
            ValueError: If `id` is not an int in ``1..2^31-1`` (bools rejected): its tokens would
                not parse back, so no sweep could reclaim them.
        """
        if not is_transaction_id(self.id):
            raise ValueError(f"Transaction id out of range: {self.id!r}")
        object.__setattr__(self, "footmarks", tuple(build_footmarks(self.id, self.timestamp)))


class _CommitResult(NamedTuple):
    """What `FootmarkMixin._commit_listed` did.

    Attributes:
        changed: True iff the transaction staged any work (PUT, DELETE or BACKUP footmarks).
        hidden: The hidden objects commit listed, sorted.
    """

    changed: bool
    hidden: list[str]


@dataclass
class _Categories:
    """One listing of the storage, classified by footmark suffix."""

    # Regular (non-hidden) relative paths.
    objects: set[str] = field(default_factory=set)
    # PUT / DELETE / BACKUP footmarks of the transaction being classified, each mapped to the
    # original it stands for.
    put: dict[str, str] = field(default_factory=dict)
    delete: dict[str, str] = field(default_factory=dict)
    backup: dict[str, str] = field(default_factory=dict)
    # Relative path of every footmark of that transaction, NOOP included.
    footmark_paths: set[str] = field(default_factory=set)
    # Every hidden relative path, footmark or not.
    all_hidden: set[str] = field(default_factory=set)


def _folded(full: str) -> str:
    """`full` as a case- and Unicode-insensitive filesystem compares it."""
    return unicodedata.normalize("NFC", full).casefold()


def _is_hidden(rel: str) -> bool:
    """Return True iff `rel` names a hidden path, which `list()` never shows.

    → [STORAGE.md](STORAGE.md#storage-abi)
    """
    return rel.rpartition("/")[2].startswith(".")


class _StagedFootmarks:
    """Full footmark paths a transaction staged, grouped by their folded spelling.

    A case or Unicode alias names one file under several spellings
    → [STORAGE.md](STORAGE.md#storage-abi). The groups find a footmark's other spellings without
    a scan; whether one names the same file is the backend's `_obj_same`, never the fold.
    """

    def __init__(self) -> None:
        """Start empty."""
        self._by_fold: dict[str, set[str]] = {}

    def __iter__(self) -> Iterator[str]:
        """Yield every staged spelling."""
        for spellings in self._by_fold.values():
            yield from spellings

    def add(self, full: str) -> None:
        """Stage `full`."""
        self._by_fold.setdefault(_folded(full), set()).add(full)

    def spellings(self, full: str) -> set[str]:
        """Staged spellings that fold like `full`, `full` itself included when staged."""
        return set(self._by_fold.get(_folded(full), ()))

    def discard(self, spellings: Iterable[str]) -> None:
        """Unstage each of `spellings` that is staged."""
        for full in spellings:
            group = self._by_fold.get(_folded(full))
            if group is not None:
                group.discard(full)
                if not group:
                    del self._by_fold[_folded(full)]


class FootmarkMixin(ReclaimMixin, ABC):
    """Shared transaction state, overlay, and lifecycle for storage backends.

    Subclasses provide the backend primitives (`_obj_*`)
    → [STORAGE.md](STORAGE.md#backend-primitives) and route their public read, write, delete,
    and metadata methods through the overlay helpers (`_read_target`, `_source_target`,
    `_overlay_exists`, `_overlay_list`, `_stage_write`, `_stage_delete`, `_metadata_target`,
    `_copy_object`, `_move_object`), so the path overlay is defined once; `StorageS3` keeps a
    per-transaction ACL overlay beside it → [STORAGE.md](STORAGE.md#s3-acl-rules). The
    reserved-path guard is the `_obj_check_reserved` primitive; its callers
    → [STORAGE.md](STORAGE.md#backend-primitives). Footmark classification and reclamation are
    inherited from `ReclaimMixin`.

    Thread safety:
        NOT thread-safe. Only one transaction may be active per storage
        instance at a time. Concurrent access from multiple threads
        requires external synchronisation.
    """

    # Provided by `Storage`, which every concrete subclass also inherits.
    metrics: StorageMetrics
    # Id of the last transaction begun on this instance; `transaction_begin(0)` uses the next.
    transaction_id: int

    def __init__(self) -> None:
        """Initialise empty transaction state."""
        self.transaction_id = 0
        self._active: TransactionModel | None = None
        # Full footmark paths this transaction staged and still expects; commit refuses one gone
        # from storage, so no attempt skips a vanished PUT, DELETE, or BACKUP.
        self._staged_footmarks = _StagedFootmarks()
        super().__init__()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def in_transaction(self) -> bool:
        """Return True iff a transaction is currently active."""
        return self._active is not None

    def _require_active(self) -> TransactionModel:
        """Return the active transaction.

        Raises:
            StorageException: If no transaction is active.
        """
        if self._active is None:  # pragma: no cover - every caller checks in_transaction() first
            raise StorageException("No active transaction")
        return self._active

    def _require_idle(self, action: str) -> None:
        """Refuse *action* while a transaction is active on this instance.

        Raises:
            StorageException: If a transaction is active.
        """
        if self.in_transaction():
            raise StorageException(f"Cannot {action}: another transaction is active")

    def _transaction_abandon(self) -> None:
        """Forget the active transaction without commit or discard, keeping its footmarks.

        `maybe_transaction` calls it after a failed commit or discard so the instance can begin
        again; the footmarks, a partial commit's BACKUPs included, are left for an orphan sweep
        → [STORAGE.md](STORAGE.md#failure-semantics).
        """
        self._active = None

    def transaction_begin(self, transaction_id: int = 0) -> TransactionModel:
        """Begin a new transaction and populate per-operation footmarks.

        All four tokens share one begin timestamp, so the model is a pure function of
        `(transaction_id, timestamp)` and a history record stores only those two.

        Args:
            transaction_id: Optional explicit id. 0 (default) auto-increments.

        Returns:
            TransactionModel: Snapshot of the new transaction.

        Raises:
            StorageException: If a transaction is already active.
            ValueError: If the explicit or auto-incremented id is outside ``1..2^31-1``; the
                instance id is left unchanged.
        """
        self._require_idle("begin")
        if isinstance(transaction_id, bool):
            # `False or n` would auto-increment; `TransactionModel` refuses `True` → STORAGE.md.
            raise ValueError(f"Transaction id out of range: {transaction_id!r}")
        transaction_id = transaction_id or self.transaction_id + 1
        self._active = TransactionModel(transaction_id, now_timestamp())
        self._staged_footmarks = _StagedFootmarks()
        self.transaction_id = transaction_id
        logger.debug(
            "Begin transaction %d with footmarks: %s", self._active.id, self._active.footmarks
        )
        return self._active

    # ------------------------------------------------------------------ #
    # Path / key helpers
    # ------------------------------------------------------------------ #

    def _add_footmark(self, path: str, operation: ObjectOperation) -> str:
        """Translate `path` to its footmarked form for `operation` in the active transaction.

        Produces `<dir>/.<file>.<token>` (or `.<file>.<token>` when no
        directory part is present) → `footmark_path`.
        """
        return footmark_path(path, self._require_active().footmarks[operation.value])

    def _refuse_gone_footmark(self, rel: str, full: str) -> None:
        """Refuse to re-create the missing footmark `full` of `rel` when this transaction staged it.

        Re-creating it from storage would let commit apply the original instead of the staged
        change → [STORAGE.md](STORAGE.md#lifecycle). The caller found the footmark missing, so a
        staged spelling of it that is gone too is the same footmark under an alias.

        Raises:
            StorageException: If this transaction staged the footmark, under any spelling.
        """
        for staged in self._staged_footmarks.spellings(full):
            if staged == full or not self._obj_exists(staged):
                raise StorageException(f"Cannot stage {rel}: staged footmark gone: {staged}")

    def _staged_spellings_of(self, full: str) -> set[str]:
        """Staged spellings naming the existing footmark `full`, to unstage once it is deleted.

        Called before the delete, while `_obj_same` can still match an alias spelling.
        """
        same = {s for s in self._staged_footmarks.spellings(full) if self._obj_same(s, full)}
        return same | {full}

    # ------------------------------------------------------------------ #
    # Backend primitives (overridden by concrete backends)
    # ------------------------------------------------------------------ #

    @abstractmethod
    def _obj_full(self, rel: str) -> str:
        """Return the backend-full path/key for a relative path."""
        raise NotImplementedError

    @abstractmethod
    def _obj_iter_rel_all(self) -> Iterable[str]:
        """Yield every relative path/key (regular and hidden)."""
        raise NotImplementedError

    @abstractmethod
    def _obj_exists(self, full: str) -> bool:
        """Return True iff the backend object at `full` exists."""
        raise NotImplementedError

    def _obj_same(self, full_a: str, full_b: str) -> bool:
        """Return True iff distinct keys `full_a` and `full_b` name one stored object.

        An alias or a hard link counts; a missing one → False. Defaults to False, since S3 keys
        have no aliases; `_is_self_move` handles equal keys itself.
        """
        del full_a, full_b
        return False

    @abstractmethod
    def _obj_copy(self, src_full: str, dst_full: str) -> None:
        """Copy data + metadata/ACL from `src_full` to `dst_full` (replace)."""
        raise NotImplementedError

    def _obj_rename(self, src_full: str, dst_full: str) -> bool:
        """Rename `src_full` to `dst_full` outside a transaction; True iff it did.

        False makes `_move_object` copy then delete: the default has no native rename.
        """
        del src_full, dst_full
        return False

    @abstractmethod
    def _obj_delete(self, full: str) -> None:
        """Hard-delete the backend object at `full`; an absent object is no error."""
        raise NotImplementedError

    def _obj_checked_footmark(self, rel: str) -> str:
        """Full path of footmark `rel`, which came from a listing of the storage, once checked.

        Commit and revert copy from or check it, and reclamation and discard delete it. The
        name is not a validated caller argument, so a backend whose namespace can point outside
        itself (a symlink on FS) refuses a footmark that is not a regular object; key/value
        backends keep the plain path.

        Raises:
            StorageNotFoundException: If the backend checks the footmark and it is gone.
            StorageException: If the backend refuses the listed footmark.
        """
        return self._obj_full(rel)

    @final
    def _reclaim_footmark(self, rel: str) -> None:
        """Delete the listed footmark object at relative path `rel`, through the checked path.

        A footmark already gone counts as deleted, as `_obj_delete` treats an absent object.

        Raises:
            StorageException: If the backend refuses the listed footmark.
        """
        try:
            full = self._obj_checked_footmark(rel)
        except StorageNotFoundException:
            return
        self._obj_delete(full)

    def _obj_reclaim_delete_many(
        self, rels: Sequence[str]
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Best-effort delete of listed footmark objects.

        The default deletes one path at a time through `_reclaim_footmark`; a
        backend with a batch primitive overrides it (S3 → ``DeleteObjects``).

        Args:
            rels: Relative paths of footmark objects to remove.

        Returns:
            ``(removed, failed)``: the paths deleted, and ``(path, reason)`` for
            every path that resisted.
        """
        removed: list[str] = []
        failed: list[tuple[str, str]] = []
        for rel in rels:
            try:
                self._reclaim_footmark(rel)
            except Exception as exc:
                failed.append((rel, str(exc)))
                continue
            removed.append(rel)
        return removed, failed

    @abstractmethod
    def _obj_touch(self, full: str) -> None:
        """Create an empty marker object/file at `full` (used for DELETE markers)."""
        raise NotImplementedError

    def _obj_prepare_parent(self, full: str) -> None:
        """Ensure the parent directory of `full` exists (no-op on key/value backends)."""

    def _obj_check_transfer(self, verb: str, src_rel: str, dst_rel: str) -> None:
        """Refuse a `copy` / `move` pair the backend cannot carry; defaults to none.

        `_copy_object` and `_move_object` call it after their reserved checks and before the
        source check, so it is a path refusal → [STORAGE.md](STORAGE.md#backend-primitives).
        """
        del verb, src_rel, dst_rel

    @abstractmethod
    def _obj_lock(self) -> AbstractContextManager[None]:
        """Return the cross-process writer lock of this storage root → `history_lock`.

        The lock is not re-entrant; `history_lock` claims `_obj_lock_identity` before it waits.
        """
        raise NotImplementedError

    @abstractmethod
    def _obj_lock_identity(self) -> Hashable:
        """Return what makes two acquires the same root lock → [STORAGE.md](STORAGE.md#locking)."""
        raise NotImplementedError

    @abstractmethod
    def _obj_put_reserved(self, rel: str, data: bytes) -> None:
        """Write a reserved root object (the history) directly, outside any overlay."""
        raise NotImplementedError

    @abstractmethod
    def _obj_get_reserved(self, rel: str) -> bytes | None:
        """Read a reserved root object (the history) directly; ``None`` when it is missing."""
        raise NotImplementedError

    def _obj_check_reserved(self, rel: str) -> None:
        """Refuse a mutation of `rel` when it names a reserved object or no object at all.

        A last segment that is empty, `.`, or `..` names a prefix or directory, not an object:
        its footmark has no original, so a staged change could never commit
        → [VALIDATION.md](VALIDATION.md#filename-safety). `StorageFS` replaces this check with
        resolution: `pool/`, `pool/.`, and `pool/x/..` name the object `pool`, and a path that
        resolves to the root or outside it is refused.

        Raises:
            StorageException: If `rel` names a reserved object, or its last segment is empty,
                `.`, or `..`.
        """
        check_reserved_path(rel)
        if rel.rpartition("/")[2] in ("", ".", ".."):
            raise StorageException(f"Refusing to write a path that names no object: {rel!r}")

    # ------------------------------------------------------------------ #
    # Categorisation (single-pass classification of every key/path)
    # ------------------------------------------------------------------ #

    def _categorize(self, footmarks: Sequence[str] | None = None) -> _Categories:
        """Single backend-list pass; classify keys/paths by their footmark token.

        Args:
            footmarks: Tokens of the transaction whose footmarks to classify, indexed by
                `ObjectOperation`; ``None`` classifies only objects and hidden paths.
        """
        out = _Categories()
        by_operation = {
            ObjectOperation.PUT: out.put,
            ObjectOperation.DELETE: out.delete,
            ObjectOperation.BACKUP: out.backup,
        }
        # Token i belongs to `ObjectOperation(i)`; a surplus token raises `ValueError`.
        buckets = {
            token: by_operation.get(ObjectOperation(i)) for i, token in enumerate(footmarks or ())
        }

        for rel in self._obj_iter_rel_all():
            if not _is_hidden(rel):
                out.objects.add(rel)
                continue

            out.all_hidden.add(rel)
            # Objects vs hidden names above; the split picks this transaction's footmarks out.
            split = split_footmark_path(rel)
            if split is None or split.token not in buckets:
                continue
            out.footmark_paths.add(rel)
            bucket = buckets[split.token]
            if bucket is not None:
                bucket[rel] = split.original

        return out

    # ------------------------------------------------------------------ #
    # Overlay: what the public methods of a backend go through
    # ------------------------------------------------------------------ #

    def _read_target(self, rel: str) -> str:
        """`_source_target` for `get` / `get_stream`, counting a refusal as a read error.

        Raises:
            StorageException: If `rel` is marked for deletion in the active transaction.
        """
        try:
            return self._source_target(rel)
        except StorageException:
            self.metrics.read_errors_inc()
            raise

    def _source_target(self, rel: str) -> str:
        """Full path `rel` resolves to as a source: its staged PUT footmark, else the original.

        Used as is by `get_metadata` and `copy`, which count no read error, as outside a
        transaction → [STORAGE.md](STORAGE.md#footmarks).

        Returns:
            `_obj_full(rel)` itself, or its PUT footmark path, which never equals it, so a caller
            detects the redirect by comparing with `_obj_full(rel)`.

        Raises:
            StorageException: If `rel` is marked for deletion in the active transaction.
        """
        full = self._obj_full(rel)
        if not self.in_transaction():
            return full
        if self._obj_exists(self._add_footmark(full, ObjectOperation.DELETE)):
            raise StorageException(f"Cannot read {rel}: marked for deletion in transaction")
        put_full = self._add_footmark(full, ObjectOperation.PUT)
        if self._obj_exists(put_full):
            logger.debug("[txn %d] Read redirected: %s -> %s", self.transaction_id, full, put_full)
            return put_full
        return full

    def _existing_source(self, rel: str) -> str:
        """`_source_target` of `rel`, which must exist as the active transaction sees it.

        Raises:
            StorageException: If `rel` is marked for deletion in the active transaction.
            StorageNotFoundException: If `rel` does not exist.
        """
        full = self._source_target(rel)
        # A redirect to the staged PUT footmark already found it, so only the original is probed.
        if full == self._obj_full(rel) and not self._obj_exists(full):
            raise StorageNotFoundException(f"Source object not found: {rel}")
        return full

    def _overlay_exists(self, rel: str) -> bool:
        """Return True iff `rel` exists, as the active transaction sees it."""
        full = self._obj_full(rel)
        if self.in_transaction():
            if self._obj_exists(self._add_footmark(full, ObjectOperation.DELETE)):
                return False
            if self._obj_exists(self._add_footmark(full, ObjectOperation.PUT)):
                return True
        return self._obj_exists(full)

    def _overlay_list(self) -> list[str]:
        """Relative paths of regular objects, as the active transaction sees them."""
        if self._active is None:
            return list(self._categorize().objects)
        cat = self._categorize(self._active.footmarks)
        # A staged hidden original stays unlisted, as after commit; a DELETE marker wins over a
        # PUT footmark, as in `_overlay_exists`.
        added = {rel for rel in cat.put.values() if not _is_hidden(rel)}
        deleted = set(cat.delete.values())
        return list((cat.objects | added) - deleted)

    @contextmanager
    def _stage_write(self, rel: str) -> Iterator[str]:
        """Prepare a write of `rel` and yield the full path the block must write.

        Inside a transaction: back the original up, target the PUT footmark, and drop a DELETE
        marker of `rel` last, so a failing staging step leaves it. A block that raises restores
        the dropped marker, so a failed write leaves a staged delete in place.

        Args:
            rel: Relative path being written.

        Raises:
            StorageException: If `rel` names a reserved object → VALIDATION.md#filename-safety,
                or a staging step fails.
        """
        self._obj_check_reserved(rel)
        full = self._obj_full(rel)
        staged = self.in_transaction()
        if staged:
            self._backup(rel)
            full = self._add_footmark(full, ObjectOperation.PUT)
            logger.debug("[txn %d] Write redirected to %s", self.transaction_id, full)
        self._prepare_parent(rel, full)
        dropped_marker = staged and self._remove_delete_marker(rel)
        try:
            yield full
        except BaseException:
            if dropped_marker:
                self._restore_delete_marker(rel)
            raise
        if staged:
            self._staged_footmarks.add(full)

    def _prepare_parent(self, rel: str, full: str) -> None:
        """Run `_obj_prepare_parent` on `full`, the target of `rel`, naming `rel` on failure.

        Raises:
            StorageException: If the parent cannot be created.
        """
        self._wrap(f"Failed to create parent of {rel}", self._obj_prepare_parent, full)

    def _stage_delete(self, rel: str) -> None:
        """Delete `rel`, or stage its deletion inside a transaction.

        Inside a transaction an object created by it loses its PUT footmark; an original is
        backed up and gets a DELETE marker, and a PUT footmark beside it is dropped so commit
        applies the delete.

        Raises:
            StorageNotFoundException: If `rel` has neither an original nor a staged PUT; a
                second delete of an already staged delete is a no-op
                → [STORAGE.md](STORAGE.md#footmarks).
            StorageException: If `rel` names a reserved object, or a backend delete or the
                marker write fails.
        """
        self._obj_check_reserved(rel)
        full = self._obj_full(rel)
        if not self.in_transaction():
            if not self._obj_exists(full):
                raise StorageNotFoundException(f"Cannot delete non-existent object: {rel}")
            self._wrap(f"Failed to delete {rel}", self._obj_delete, full)
            return

        put_full = self._add_footmark(full, ObjectOperation.PUT)
        put_exists = self._obj_exists(put_full)
        original_exists = self._obj_exists(full)
        if not put_exists and not original_exists:
            raise StorageNotFoundException(f"Cannot delete non-existent object: {rel}")

        if original_exists:
            self._backup_existing(rel)
        # The delete supersedes a staged write, whether or not its PUT footmark was still there.
        put_spellings = self._staged_spellings_of(put_full) if put_exists else {put_full}
        if put_exists:
            self._wrap(f"Failed to drop PUT footmark of {rel}", self._obj_delete, put_full)
        self._staged_footmarks.discard(put_spellings)
        if original_exists:
            delete_full = self._add_footmark(full, ObjectOperation.DELETE)
            self._wrap(f"Failed to create DELETE marker of {rel}", self._obj_touch, delete_full)
            self._staged_footmarks.add(delete_full)

    def _metadata_target(self, rel: str) -> str:
        """Full path a metadata change of `rel` must mutate: a staged copy inside a transaction.

        Raises:
            StorageException: If `rel` names a reserved object.
        """
        self._obj_check_reserved(rel)
        if self.in_transaction():
            return self._stage_for_metadata_change(rel)
        return self._obj_full(rel)

    def _copy_object(self, src_rel: str, dst_rel: str) -> None:
        """Copy `src_rel` to `dst_rel`, both as the active transaction sees them.

        Args:
            src_rel: Relative source path.
            dst_rel: Relative destination path.

        Raises:
            StorageException: If the destination is reserved, the source is marked for
                deletion, or the copy fails.
            StorageNotFoundException: If the source does not exist.
        """
        # Refuse a reserved destination before the source check, as `_move_object` does.
        self._obj_check_reserved(dst_rel)
        self._obj_check_transfer("copy", src_rel, dst_rel)
        src_full = self._existing_source(src_rel)
        self._copy_existing(src_rel, src_full, dst_rel)

    def _copy_existing(self, src_rel: str, src_full: str, dst_rel: str) -> None:
        """Stage a copy of `src_rel` to `dst_rel`; `_existing_source` resolved it to `src_full`."""
        with self._stage_write(dst_rel) as dst_full:
            logger.debug("Copying: %s -> %s", src_full, dst_full)
            message = f"Failed to copy {src_rel} -> {dst_rel}"
            self._wrap(message, self._obj_copy, src_full, dst_full)

    def _move_object(self, src_rel: str, dst_rel: str) -> None:
        """Move `src_rel` to `dst_rel`, both as the active transaction sees them.

        Onto itself a no-op → `_is_self_move`. Outside a transaction `_obj_rename` may rename
        natively; otherwise this is `_copy_object` then `_stage_delete`, so both legs are tracked
        with footmarks.

        Args:
            src_rel: Relative source path.
            dst_rel: Relative destination path.

        Raises:
            StorageException: If either path is reserved, the source is marked for deletion, or
                a leg fails.
            StorageNotFoundException: If the source does not exist.
        """
        self._obj_check_reserved(src_rel)
        self._obj_check_reserved(dst_rel)
        self._obj_check_transfer("move", src_rel, dst_rel)
        src_full = self._existing_source(src_rel)
        if self._is_self_move(src_rel, dst_rel):
            return
        renamed = False
        if not self.in_transaction():
            message = f"Failed to move {src_rel} -> {dst_rel}"
            orig_src, orig_dst = self._obj_full(src_rel), self._obj_full(dst_rel)
            self._prepare_parent(dst_rel, orig_dst)
            renamed = self._wrap(message, self._obj_rename, orig_src, orig_dst)
        if not renamed:
            self._copy_existing(src_rel, src_full, dst_rel)
            self._stage_delete(src_rel)
        # A move removes its source whichever way it ran → metrics.py.
        self.metrics.delete_objects_inc()

    def _is_self_move(self, src_rel: str, dst_rel: str) -> bool:
        """Return True iff `src_rel` and `dst_rel` name one object, so `move` is a no-op.

        Copy-then-delete onto itself would stage a DELETE over the copy and lose the object
        on commit → [STORAGE.md](STORAGE.md#storage-abi). Inside a transaction the two must
        name one object as it sees them: a staged write on one alias, or a staged delete of
        the destination, makes the move real.
        """
        src_full = self._obj_full(src_rel)
        dst_full = self._obj_full(dst_rel)
        if src_full == dst_full:
            return True
        if not self.in_transaction():
            return self._obj_same(src_full, dst_full)
        put = ObjectOperation.PUT
        src_put = self._add_footmark(src_full, put)
        dst_put = self._add_footmark(dst_full, put)
        # Only aliased originals or PUT footmarks can name one object; S3 stops here.
        if not (self._obj_same(src_full, dst_full) or self._obj_same(src_put, dst_put)):
            return False
        if self._obj_exists(self._add_footmark(dst_full, ObjectOperation.DELETE)):
            return False
        src_view = src_put if self._obj_exists(src_put) else src_full
        dst_view = dst_put if self._obj_exists(dst_put) else dst_full
        return self._obj_same(src_view, dst_view)

    @staticmethod
    def _wrap[R, *Ts](message: str, operation: Callable[[*Ts], R], *args: *Ts) -> R:
        """Run a backend primitive, raising its failure as `StorageException` with *message*."""
        try:
            return operation(*args)
        except StorageException:
            raise
        except Exception as exc:
            raise StorageException(f"{message}: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Footmark helpers used by the overlay
    # ------------------------------------------------------------------ #

    def _remove_delete_marker(self, rel: str) -> bool:
        """Drop the DELETE marker of original `rel` (delete-then-recreate); True if dropped.

        Raises:
            StorageException: If the marker cannot be dropped; it stays, so the delete stays
                staged and commit cannot remove the object the write was about to create.
        """
        delete_full = self._add_footmark(self._obj_full(rel), ObjectOperation.DELETE)
        if not self._obj_exists(delete_full):
            return False
        spellings = self._staged_spellings_of(delete_full)
        self._wrap(f"Failed to drop DELETE marker of {rel}", self._obj_delete, delete_full)
        self._staged_footmarks.discard(spellings)
        return True

    def _restore_delete_marker(self, rel: str) -> None:
        """Re-create the DELETE marker of original `rel` after a failed write dropped it."""
        delete_full = self._add_footmark(self._obj_full(rel), ObjectOperation.DELETE)
        try:
            self._obj_touch(delete_full)
        except Exception as exc:  # the write's own failure is the one raised
            logger.warning("Failed to restore DELETE marker %s: %s", delete_full, exc)
            return
        self._staged_footmarks.add(delete_full)

    def _backup(self, rel: str) -> None:
        """Snapshot the original of `rel` for transaction rollback, when it exists."""
        if self._obj_exists(self._obj_full(rel)):
            self._backup_existing(rel)

    def _backup_existing(self, rel: str) -> None:
        """Snapshot the original of `rel`, which the caller found to exist. First write wins.

        Raises:
            StorageException: If the BACKUP this transaction staged is gone: a copy now could
                snapshot an original a failed commit already overwrote.
        """
        original_full = self._obj_full(rel)
        backup_full = self._add_footmark(original_full, ObjectOperation.BACKUP)
        if self._obj_exists(backup_full):
            return
        self._refuse_gone_footmark(rel, backup_full)

        logger.debug(
            "[txn %d] Creating backup: %s -> %s",
            self.transaction_id,
            original_full,
            backup_full,
        )
        self._wrap(
            f"Failed to create backup of {original_full}",
            self._obj_copy,
            original_full,
            backup_full,
        )
        self._staged_footmarks.add(backup_full)

    def _stage_for_metadata_change(self, rel: str) -> str:
        """Force-create a PUT footmark + BACKUP for `rel`.

        Used by the metadata methods inside a transaction so the mutation lands on a staged
        copy instead of the live original. Both `transaction_discard` and
        `transaction_revert` can then roll the change back.

        Returns:
            The backend-full path the mutation must target: the existing PUT footmark, or a
            new one copied from the original after a BACKUP.

        Raises:
            StorageException: If `rel` is marked for deletion in the active transaction, if
                neither the PUT footmark nor the original exist, if the PUT footmark this
                transaction staged is gone, or if the original-to-footmark copy fails.
        """
        original_full = self._obj_full(rel)
        if self._obj_exists(self._add_footmark(original_full, ObjectOperation.DELETE)):
            raise StorageException(
                f"Cannot stage metadata change: '{rel}' is marked for deletion in transaction"
            )
        put_full = self._add_footmark(original_full, ObjectOperation.PUT)

        if self._obj_exists(put_full):
            return put_full
        # Copying the original now would replace the staged write commit is about to apply.
        self._refuse_gone_footmark(rel, put_full)

        if not self._obj_exists(original_full):
            raise StorageException(f"Cannot stage metadata change: '{rel}' does not exist")

        self._backup_existing(rel)
        self._prepare_parent(rel, put_full)

        logger.debug(
            "[txn %d] Staging metadata change: %s -> %s",
            self.transaction_id,
            original_full,
            put_full,
        )
        self._wrap(
            f"Failed to stage {original_full} for metadata change",
            self._obj_copy,
            original_full,
            put_full,
        )
        self._staged_footmarks.add(put_full)
        return put_full

    # ------------------------------------------------------------------ #
    # Transaction terminators
    # ------------------------------------------------------------------ #

    def transaction_discard(self) -> None:
        """Drop every staged footmark without touching originals.

        Raises:
            StorageException: If no transaction is active or cleanup fails.
        """
        if self._active is None:
            raise StorageException("Cannot discard: no active transaction")

        logger.debug("Discarding transaction %d", self._active.id)

        # Listed paths, so the same confined delete reclamation uses: a planted
        # symlink named with this transaction's token fails the discard loudly
        # instead of reaching its target. One batch, then each failure alone, so the
        # raised error is chained from its cause and a refusal surfaces unchanged.
        rels = sorted(self._categorize(self._active.footmarks).footmark_paths)
        removed, failed = self._obj_reclaim_delete_many(rels)
        for rel in removed:
            logger.debug("[txn %d] Removed footmark: %s", self._active.id, rel)
        first_error: StorageException | None = None
        for rel, reason in failed:
            logger.debug("[txn %d] Batch delete of %s failed (%s)", self._active.id, rel, reason)
            try:
                self._wrap(f"Failed to remove footmark {rel}", self._reclaim_footmark, rel)
            except StorageException as exc:
                if first_error is None:
                    first_error = exc
                else:
                    logger.warning("[txn %d] %s", self._active.id, exc)
                continue
            logger.debug("[txn %d] Removed footmark: %s", self._active.id, rel)
        if first_error is not None:
            raise first_error

        self._active = None

    def transaction_commit(self) -> bool:
        """Apply staged PUTs over originals; remove DELETE-marked originals.

        BACKUP / PUT / DELETE footmark files/keys stay in the storage so
        `transaction_revert` can undo the commit.

        Returns:
            bool: True iff the transaction staged any work (PUT, DELETE or
            BACKUP footmarks were present), False for an empty transaction.

        Raises:
            StorageException: If no transaction is active or commit fails.
        """
        return self._commit_listed().changed

    def _commit_listed(self) -> _CommitResult:
        """Commit as `transaction_commit` does, also returning the hidden objects it listed.

        The listing is sorted, and exact for footmarks while the caller still holds
        `history_lock`: commit creates and deletes no footmark, so `maybe_transaction` hands it
        to the prune instead of listing again → [STYLE.md](STYLE.md#performance). A dotfile
        original that commit writes or deletes may differ from it, which the footmark
        classifiers ignore. It is the commit hook a subclass would override
        → [STORAGE.md](STORAGE.md#backend-primitives).

        Returns:
            Whether the transaction staged any work, and the hidden objects commit listed.

        Raises:
            StorageException: If no transaction is active or commit fails.
        """
        if self._active is None:
            raise StorageException("Cannot commit: no active transaction")
        txn = self._active

        logger.debug("Committing transaction %d", txn.id)

        cat = self._categorize(txn.footmarks)
        listed_rels = cat.put.keys() | cat.delete.keys() | cat.backup.keys()
        listed = {self._obj_full(rel) for rel in listed_rels}
        # A footmark this transaction staged and that is gone would be skipped silently: its PUT
        # or DELETE dropped, its BACKUP lost to a later revert. Commit deletes no footmark, so a
        # retry checks the same set. One the listing spells differently, a case or Unicode alias,
        # still exists.
        gone = [
            full
            for full in self._staged_footmarks
            if full not in listed and not self._obj_exists(full)
        ]
        if gone:
            raise StorageException(f"Cannot commit: staged footmark gone: {min(gone)}")
        has_changes = bool(cat.put or cat.delete or cat.backup)
        # Every original and copied footmark is checked before the first write: a refusal is
        # permanent, so a check inside the loops would leave a half-applied commit no retry could
        # finish.
        puts = self._checked_footmarks(cat.put)
        deletes = [self._checked_full(original) for original in cat.delete.values()]
        # Checked, never copied: if revert would refuse a BACKUP, commit would overwrite the
        # only other copy of its original. Only the refusal matters; the mapping is unused.
        self._checked_footmarks(cat.backup)

        for footmark_full, original_full in puts.items():
            logger.debug("[txn %d] Committing PUT: %s -> %s", txn.id, footmark_full, original_full)
            self._wrap(
                f"Failed to commit PUT {footmark_full} -> {original_full}",
                self._obj_copy,
                footmark_full,
                original_full,
            )

        for original_full in deletes:
            logger.debug("[txn %d] Committing DELETE: %s", txn.id, original_full)
            self._wrap(f"Failed to commit DELETE {original_full}", self._obj_delete, original_full)

        # Cleared last, and never in a `finally`: `StorageS3._obj_copy` reads its ACL sidecar
        # only while a transaction is active → [STORAGE.md](STORAGE.md#s3-acl-rules), and a
        # failed commit stays active so it can be retried → [STORAGE.md](STORAGE.md#transactions).
        self._active = None
        return _CommitResult(has_changes, sorted(cat.all_hidden))

    def _checked_footmarks(self, footmarks: Mapping[str, str]) -> dict[str, str]:
        """Return ``{footmark_full: original_full}`` for listed footmarks, both once checked.

        Args:
            footmarks: Listed footmark relative path → original relative path.

        Raises:
            StorageException: If the backend refuses a footmark as a copy source or it is gone
                since the listing, or `_checked_full` refuses an original.
        """
        checked: dict[str, str] = {}
        for rel, original in footmarks.items():
            try:
                full = self._obj_checked_footmark(rel)
            except StorageNotFoundException as exc:
                # The caller's object is not what is missing, so the not-found signal stops here.
                raise StorageException(f"Footmark gone since the listing: {rel}") from exc
            checked[full] = self._checked_full(original)
        return checked

    def _checked_full(self, original: str) -> str:
        """Full path of `original`, the original a listed footmark stands for, once checked.

        `_categorize` split the listed name, never the resolved path: a footmark planted as a
        symlink would otherwise map to its target. The original passes the reserved check, which
        on `fs` also refuses a path that resolves outside the root, so a planted footmark never
        makes commit or revert touch a reserved object, a nameless path, or anything outside the
        root → [VALIDATION.md](VALIDATION.md#filename-safety).

        Raises:
            StorageException: If the original is reserved, names no object, is the root, or
                lies outside it.
        """
        self._obj_check_reserved(original)
        return self._obj_full(original)

    def transaction_revert(self, transaction: TransactionModel) -> None:
        """Roll back a previously committed transaction.

        Args:
            transaction: The handle returned by `transaction_begin`.

        Raises:
            StorageException: If a transaction is already active or revert fails.
        """
        self._require_idle("revert")

        txn_id, footmarks = transaction.id, transaction.footmarks
        logger.debug("Reverting transaction %d", txn_id)

        cat = self._categorize(footmarks)
        # Every original and copied footmark is checked before the first write, as in commit.
        backups = self._checked_footmarks(cat.backup)
        restored = set(backups.values())
        created = [
            full for full in map(self._checked_full, cat.put.values()) if full not in restored
        ]

        # 1. Remove newly created originals (PUT footmark without BACKUP).
        for original_full in created:
            logger.debug("[txn %d] Removing newly created object: %s", txn_id, original_full)
            self._wrap(
                f"Failed to remove newly created object {original_full}",
                self._obj_delete,
                original_full,
            )

        # 2. Restore from backups (covers both modify-revert and delete-revert).
        for backup_full, original_full in backups.items():
            logger.debug(
                "[txn %d] Restoring from backup: %s -> %s", txn_id, backup_full, original_full
            )
            self._wrap(
                f"Failed to restore {original_full} from backup",
                self._obj_copy,
                backup_full,
                original_full,
            )

        # A transaction whose footmarks are all gone reverts to a no-op: retention
        # forgot them and a crash cut the prune short before the history rewrite. Say so
        # rather than reporting a successful revert that changed nothing.
        if not cat.footmark_paths:
            logger.warning("Transaction %d has no footmarks left; revert changed nothing", txn_id)

        # 3. Best-effort cleanup of every footmark for the transaction, BACKUPs last, so a
        # retry after a crash here restores again instead of deleting a restored original.
        self._delete_footmark_paths(cat.footmark_paths, f"txn {txn_id}", [transaction])

    # ------------------------------------------------------------------ #
    # Listing for reclamation (classification lives in `ReclaimMixin`)
    # ------------------------------------------------------------------ #

    def hidden_objects(self) -> list[str]:
        """List every hidden object once, for the reclamation helpers.

        Returns:
            Relative paths whose basename starts with ``.``, sorted. Footmarks,
            reserved objects, and foreign dotfiles alike; the helpers classify.
        """
        return sorted(self._categorize().all_hidden)


def is_transactional(storage: object) -> TypeIs[FootmarkMixin]:
    """Return True iff *storage* supports transactions.

    `FootmarkMixin` is the discriminator: the `TransactionNotSupportedMixin` stubs of
    `StorageHTTP` and `StorageNull` carry the same method names, so a structural check would
    match them. `TypeIs` narrows a `Storage` argument to the intersection with
    `FootmarkMixin`, so the caller keeps both the `Storage` methods and the transaction ones.

    Args:
        storage: Any storage object.

    Returns:
        True iff *storage* is a `FootmarkMixin` instance.
    """
    return isinstance(storage, FootmarkMixin)
