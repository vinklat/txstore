"""Transaction history persistence for storage backends.

Maintains a `.transactions.json` file at the storage root that records every committed,
non-empty transaction as a minimal ``{id, timestamp}`` object. Footmark tokens are a pure
function of ``(id, timestamp)`` and are regenerated when the file is loaded, so transactions can
be listed and reverted without storing any extra per-object state. Records are parsed once, in
`TransactionHistory.load`, into `TransactionModel`.

Concurrent writers are serialised by `history_lock`, which holds the backend's writer lock →
[STORAGE.md](STORAGE.md#locking). `maybe_transaction` wraps a block of writes in one recorded
transaction under that lock → [STORAGE.md](STORAGE.md#failure-semantics).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Hashable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from txstore._env import env_int
from txstore._reserved import HISTORY_FILENAME
from txstore._timestamp import Timestamp
from txstore.base import Storage, StorageException, StorageNotFoundException
from txstore.footmark import TRANSACTION_ID_MAX, is_transaction_id
from txstore.transaction import FootmarkMixin, TransactionModel, is_transactional

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Refusals of the entry points that need a transactional backend.
_NO_REVERT = "Storage backend does not support transaction revert."
_NO_RETENTION = "Storage backend does not support transaction retention."
_NO_HISTORY = "Storage backend does not record transaction history."

# Recovery hint on every malformed-history failure: the file is a hard
# dependency of the write path, so operators need the way out in the message.
_HISTORY_RECOVERY_HINT = (
    f"move or delete {HISTORY_FILENAME} out of band to resume writes "
    "(transactions already committed become unrevertable)"
)

# Retention defaults → [CONFIG.md](CONFIG.md#history-retention).
HISTORY_KEEP_DEFAULT = 20
HISTORY_MAX_AGE_DAYS_DEFAULT = 90
# 100 years; far below the ~739,000 days at which the expiry cutoff leaves `datetime`'s range.
_MAX_AGE_DAYS_LIMIT = 36_500

# Strict RFC 3339 (`Timestamp.to_json` form, any offset); ASCII only, so no control characters.
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$", re.ASCII
)


def _malformed(field: str, value: object) -> StorageException:
    """The error for a history record whose `field` holds `value`."""
    return StorageException(
        f"{HISTORY_FILENAME}: malformed transaction {field} {value!r}; {_HISTORY_RECOVERY_HINT}"
    )


@dataclass(frozen=True)
class HistoryRetention:
    """How much transaction history to keep.

    Age decides what expires; the count is a floor under it, so a busy
    repository never drops below ``keep`` revertable transactions no matter how
    old they are. The record holding the highest id is always inside that floor,
    which keeps ``next_id`` monotonic.

    Attributes:
        keep: Newest transactions (by id) that are never pruned; at least 1.
        max_age_days: Age past which an unprotected record expires; ``0``
            disables pruning entirely.
    """

    keep: int = HISTORY_KEEP_DEFAULT
    max_age_days: int = HISTORY_MAX_AGE_DAYS_DEFAULT

    def __post_init__(self) -> None:
        """Reject out-of-range values → [VALIDATION.md](VALIDATION.md#history-retention).

        Raises:
            ValueError: If ``keep`` is below 1 or ``max_age_days`` is outside 0..36500.
        """
        if isinstance(self.keep, bool) or not isinstance(self.keep, int) or self.keep < 1:
            raise ValueError(f"history keep must be an integer of 1 or more, got {self.keep!r}")
        if (
            isinstance(self.max_age_days, bool)
            or not isinstance(self.max_age_days, int)
            or not 0 <= self.max_age_days <= _MAX_AGE_DAYS_LIMIT
        ):
            raise ValueError(
                f"history max age must be an integer of 0 to {_MAX_AGE_DAYS_LIMIT} days, "
                f"got {self.max_age_days!r}"
            )

    @classmethod
    def from_env(
        cls, *, keep: int | None = None, max_age_days: int | None = None
    ) -> HistoryRetention:
        """Build the policy from arguments, then ``TX_STORE_HISTORY_*`` variables, then defaults.

        Args:
            keep: Newest transactions never pruned.
            max_age_days: Age in days past which a record expires; ``0`` disables pruning.

        Returns:
            The resolved retention policy.

        Raises:
            ValueError: If a value or variable is not an integer or is out of range.
        """
        if keep is None:
            keep = env_int("TX_STORE_HISTORY_KEEP")
        if max_age_days is None:
            max_age_days = env_int("TX_STORE_HISTORY_MAX_AGE_DAYS")
        return cls(
            keep=HISTORY_KEEP_DEFAULT if keep is None else keep,
            max_age_days=HISTORY_MAX_AGE_DAYS_DEFAULT if max_age_days is None else max_age_days,
        )

    @property
    def enabled(self) -> bool:
        """True iff age-based pruning is switched on."""
        return self.max_age_days > 0

    def select_expired(
        self, records: Sequence[TransactionModel], now: datetime | None = None
    ) -> list[TransactionModel]:
        """Return the records this policy expires, in file order; pure.

        Protection is by id, not by position: ``next_id`` is ``max(id) + 1``, so
        the floor must cover the highest ids even if the file is not ordered by
        id. Ids are uniqued first, so a repeated id spends one slot.

        Args:
            records: Loaded history.
            now: Reference time; defaults to the current UTC time.

        Returns:
            The expired records, or an empty list when retention is disabled.
        """
        if not self.enabled or len(records) <= self.keep:
            return []
        protected = set(sorted({m.id for m in records}, reverse=True)[: self.keep])
        cutoff = (now or datetime.now(UTC)) - timedelta(days=self.max_age_days)
        return [m for m in records if m.id not in protected and m.timestamp.to_datetime() < cutoff]


class PruneResult(NamedTuple):
    """What a prune removed, or would remove under ``dry_run``.

    Attributes:
        records: Transactions dropped from the history, in file order.
        footmark_paths: Relative paths of the dropped records' footmark objects
            that were deleted; under ``dry_run``, the ones that would be.
        orphans: Relative paths of orphaned footmark objects deleted; under
            ``dry_run``, the ones that would be.
    """

    records: list[TransactionModel]
    footmark_paths: list[str]
    orphans: list[str]


def _parse_txn_timestamp(raw: object) -> Timestamp:
    """Parse a stored record timestamp.

    The regex is strict where `Timestamp.from_json` is lenient (padding, a bare ``.``), and the
    parse rejects what the regex admits but no instant has, such as month 13.

    Raises:
        StorageException: If *raw* is not an RFC 3339 string naming a real instant.
    """
    if not isinstance(raw, str) or not _RFC3339_RE.fullmatch(raw):
        raise _malformed("timestamp", raw)
    try:
        return Timestamp.from_json(raw)
    except ValueError as exc:
        raise _malformed("timestamp", raw) from exc


def _plan_prune(
    storage: FootmarkMixin,
    records: Sequence[TransactionModel],
    retention: HistoryRetention,
    now: datetime | None,
    *,
    orphans: bool,
    listing: Sequence[str] | None,
) -> PruneResult:
    """Decide what a prune removes, changing nothing: the dry-run answer.

    Orphans are classified against every record, expired ones included: their footmarks
    are the reclaim's to delete, so a dry run and a real run report the same paths.
    """
    expired = retention.select_expired(records, now)
    if not expired and not orphans:
        # The common commit pays nothing: no listing, no rewrite.
        return PruneResult([], [], [])
    if listing is None:
        # One walk serves the reclaim and the orphan sweep alike.
        listing = storage.hidden_objects()
    footmarks = storage.find_transaction_footmarks(expired, listing=listing)
    swept = (
        storage.find_orphaned_footmarks([m.footmarks for m in records], listing=listing)
        if orphans
        else []
    )
    return PruneResult(expired, footmarks, swept)


class _HeldWriterLocks(threading.local):
    """Identities of the writer locks the current thread holds; one set per thread."""

    def __init__(self) -> None:
        self.identities: set[Hashable] = set()


_HELD_WRITER_LOCKS = _HeldWriterLocks()


@contextmanager
def _claim_writer_lock(storage: FootmarkMixin) -> Iterator[None]:
    """Mark *storage*'s root lock as held by the current thread for the block.

    The lock is not re-entrant, so a thread waiting on a lock it holds itself would never
    return; other threads are unaffected and wait on the lock → [STORAGE.md](STORAGE.md#locking).

    Raises:
        StorageException: If the current thread already holds the lock.
    """
    identity = storage._obj_lock_identity()
    held = _HELD_WRITER_LOCKS.identities
    if identity in held:
        raise StorageException(
            f"Cannot lock history: this thread already holds the history lock of {storage!r}"
        )
    held.add(identity)
    try:
        yield
    finally:
        held.discard(identity)


@contextmanager
def history_lock(storage: Storage) -> Iterator[None]:
    """Serialise transactional writers on the same storage root.

    Holds the backend's writer lock (`FootmarkMixin._obj_lock`): an exclusive ``flock`` on FS,
    a conditional-write lock object on S3 → [STORAGE.md](STORAGE.md#locking). A
    non-transactional backend keeps no history, so the lock is a no-op there. The lock is not
    re-entrant: a nested acquire is refused rather than left waiting on itself.

    Args:
        storage: Concrete storage backend.

    Yields:
        None while the lock is held.

    Raises:
        StorageException: If *storage* has an active transaction, the current thread already
            holds this root's lock, or the backend's lock step fails.
    """
    if not is_transactional(storage):
        yield
        return
    # A transaction begun by hand holds no lock, so the claim cannot see it: refuse history work
    # while this instance has staged changes.
    storage._require_idle("lock history")
    with _claim_writer_lock(storage), storage._obj_lock():
        yield


class TransactionHistory:
    """Read/write the transaction history array through a Storage instance.

    Args:
        storage: Any concrete Storage backend; writing, reverting, and pruning need a
            transactional one.
    """

    def __init__(self, storage: Storage) -> None:
        """Initialize with the given storage backend."""
        self._storage = storage

    def load(self) -> list[TransactionModel]:
        """Load and parse the history array from storage.

        A transactional storage reads it with `_obj_get_reserved`, outside the metrics
        → [STORAGE.md](STORAGE.md#storage-abi); any other storage (an HTTP mirror) with a single
        ``get``. Either way one read, no ``exists`` probe, so S3 pays one round-trip. A missing
        file is an empty history.

        Returns:
            The recorded transactions in file order (may be empty).

        Raises:
            StorageException: If the file exists but is malformed. Every transactional write
                reads the history before beginning, so this aborts it before any change is
                staged → [STORAGE.md](STORAGE.md#failure-semantics).
        """
        if is_transactional(self._storage):
            raw = self._storage._obj_get_reserved(HISTORY_FILENAME)
            if raw is None:
                return []
        else:
            try:
                raw = self._storage.get(HISTORY_FILENAME)
            except StorageNotFoundException:
                return []
        try:
            data = json.loads(raw)
        # Not only JSONDecodeError: too-deep nesting and an int past the digit limit raise too.
        except (ValueError, RecursionError) as exc:
            raise StorageException(
                f"Cannot parse {HISTORY_FILENAME}: {exc}; {_HISTORY_RECOVERY_HINT}"
            ) from exc
        if not isinstance(data, list):
            raise StorageException(
                f"{HISTORY_FILENAME} root is not a JSON array; {_HISTORY_RECOVERY_HINT}"
            )
        if not all(isinstance(r, dict) for r in data):
            raise StorageException(
                f"{HISTORY_FILENAME} contains non-object entries; {_HISTORY_RECOVERY_HINT}"
            )
        return [self.record_to_model(rec) for rec in data]

    def next_id(self, records: Sequence[TransactionModel] | None = None) -> int:
        """Return the next monotonic transaction id.

        Args:
            records: Pre-loaded history. When ``None``, loads from storage.

        Returns:
            ``max(id) + 1`` across records, or 1 if history is empty.

        Raises:
            StorageException: If the int32 id space is exhausted.
        """
        if records is None:
            records = self.load()
        max_id = max((m.id for m in records), default=0)
        if max_id >= TRANSACTION_ID_MAX:
            raise StorageException(
                f"{HISTORY_FILENAME}: transaction id space exhausted ({max_id}); rotate history"
            )
        return max_id + 1

    def append(
        self,
        model: TransactionModel,
        records: Sequence[TransactionModel] | None = None,
    ) -> None:
        """Append a committed transaction record to the history.

        Stores only ``{id, timestamp}``; the footmark tokens are regenerated
        from those two fields on load.

        Args:
            model: The TransactionModel returned by transaction_begin.
            records: Pre-loaded history to extend; not modified. When ``None``, loads from
                storage first. Under ``history_lock`` the begin-time list is safe to reuse.
        """
        if records is None:
            records = self.load()
        self.save([*records, model])
        logger.debug("Appended transaction %d to history", model.id)

    @staticmethod
    def record_to_model(record: Mapping[str, object]) -> TransactionModel:
        """Parse one stored history record into a `TransactionModel`.

        Footmark tokens are regenerated deterministically from ``id`` +
        ``timestamp``.

        Args:
            record: A single history record with ``id`` and ``timestamp``.

        Returns:
            TransactionModel: Handle suitable for `transaction_revert`.

        Raises:
            StorageException: If the record is malformed.
        """
        tid = record.get("id")
        if not is_transaction_id(tid):
            raise _malformed("id", tid)
        return TransactionModel(tid, _parse_txn_timestamp(record.get("timestamp")))

    def revert_last(self) -> TransactionModel | None:
        """Revert the latest recorded transaction and drop its history entry.

        Held under ``history_lock`` so a concurrent ``maybe_transaction`` cannot append a
        record that this call then clobbers with a stale snapshot. History is always re-loaded
        inside the lock.

        Returns:
            The reverted transaction, or ``None`` if history was empty.

        Raises:
            StorageException: If the backend cannot revert, or the history is malformed.
        """
        storage = self._storage
        if not is_transactional(storage):
            raise StorageException(_NO_REVERT)
        with history_lock(storage):
            records = self.load()
            if not records:
                return None
            last = records[-1]
            storage.transaction_revert(last)
            self.save(records[:-1])
            logger.debug("Reverted transaction %d and removed history entry", last.id)
            return last

    def pop_last(self) -> TransactionModel | None:
        """Remove and return the last history record without reverting storage.

        For the full revert protocol (undo objects + drop history entry), use
        ``revert_last`` instead. Held under ``history_lock`` with a fresh load
        so concurrent writers cannot race on ``.transactions.json``.

        Returns:
            The removed transaction, or None if history is empty.

        Raises:
            StorageException: If the backend records no history, or the history is malformed.
        """
        if not is_transactional(self._storage):
            raise StorageException(_NO_HISTORY)
        with history_lock(self._storage):
            records = self.load()
            if not records:
                return None
            self.save(records[:-1])
            logger.debug("Removed transaction %d from history", records[-1].id)
            return records[-1]

    def prune(
        self,
        retention: HistoryRetention,
        *,
        now: datetime | None = None,
        orphans: bool = False,
        dry_run: bool = False,
    ) -> PruneResult:
        """Take ``history_lock``, then drop expired records and the footmark objects behind them.

        Footmarks are deleted before the history is rewritten. The reverse order
        would strand footmarks whose tokens no surviving record can regenerate;
        this order leaves at worst a record whose footmarks are already gone,
        which the next prune expires again and `transaction_revert` reports as a
        no-op → [STORAGE.md](STORAGE.md#retention).

        Args:
            retention: Policy to apply.
            now: Reference time for expiry; defaults to the current UTC time.
            orphans: Also sweep footmark objects belonging to no recorded transaction.
            dry_run: Report what would be removed without changing anything. The lock is
                still taken, since orphan classification is only sound with writers excluded.

        Returns:
            What was removed, or would be removed under ``dry_run``.

        Raises:
            StorageException: If the backend is not transactional, the history
                is malformed, or a listing or rewrite fails.
        """
        storage = self._storage
        if not is_transactional(storage):
            raise StorageException(_NO_RETENTION)
        with history_lock(storage):
            records = self.load()
            plan = _plan_prune(storage, records, retention, now, orphans=orphans, listing=None)
            return plan if dry_run else self._apply_prune(storage, records, plan)

    def _prune_locked(
        self,
        storage: FootmarkMixin,
        retention: HistoryRetention,
        records: Sequence[TransactionModel],
        *,
        listing: Sequence[str] | None = None,
        now: datetime | None = None,
    ) -> PruneResult:
        """Prune *records* while the caller already holds ``history_lock``.

        `maybe_transaction` calls this after a recorded commit, passing this history's storage
        as it already narrowed it, the history it holds, and the hidden objects the commit
        listed. Never sweeps orphans.

        Args:
            storage: This history's storage, already known to be transactional.
            retention: Policy to apply.
            records: The current history, as the caller loaded and extended it; not modified.
            listing: A full `hidden_objects` walk taken under the lock; ``None`` lists once.
            now: Reference time for expiry; defaults to the current UTC time.

        Returns:
            What was removed.

        Raises:
            StorageException: If a rewrite fails.
        """
        plan = _plan_prune(storage, records, retention, now, orphans=False, listing=listing)
        return self._apply_prune(storage, records, plan)

    def _apply_prune(
        self, storage: FootmarkMixin, records: Sequence[TransactionModel], plan: PruneResult
    ) -> PruneResult:
        """Carry out *plan*: forget the expired footmarks, rewrite the history, sweep orphans.

        Returns:
            The records dropped and the paths actually deleted; a delete that fails is logged
            and left out.
        """
        removed: list[str] = []
        if plan.records:
            removed = storage.transaction_forget(plan.records, paths=plan.footmark_paths)
            # By value: expiry is a function of (id, timestamp) alone, so equal records expire
            # together, and a set keeps a large backlog linear.
            expired = set(plan.records)
            self.save([m for m in records if m not in expired])
            logger.info(
                "Pruned %d transaction(s) from history; removed %d footmark object(s)",
                len(plan.records),
                len(removed),
            )
            if len(removed) < len(plan.footmark_paths):
                logger.warning(
                    "%d footmark object(s) resisted removal; `prune --orphans` reclaims them",
                    len(plan.footmark_paths) - len(removed),
                )
        swept: list[str] = []
        if plan.orphans:
            swept = storage.transaction_forget_orphans(
                [m.footmarks for m in records], orphans=plan.orphans
            )
            logger.info("Removed %d orphaned footmark object(s)", len(swept))
        return PruneResult(plan.records, removed, swept)

    def save(self, records: Sequence[TransactionModel]) -> None:
        """Write the records array back to storage as ``{id, timestamp}`` objects.

        Uses compact JSON (no pretty-print) to keep the rewrite payload small; every write
        replaces the full file. A `TransactionModel` holds only an id ``load()`` accepts, so a
        write never leaves a history it rejects. The file is a reserved object written through
        `_obj_put_reserved` → [STORAGE.md](STORAGE.md#files).

        Args:
            records: The full history to persist.

        Raises:
            StorageException: If the backend is not transactional or the write fails.
        """
        storage = self._storage
        if not is_transactional(storage):
            raise StorageException(_NO_HISTORY)
        payload = json.dumps(
            [{"id": m.id, "timestamp": m.timestamp.to_json()} for m in records],
            separators=(",", ":"),
        ).encode("utf-8")
        storage._obj_put_reserved(HISTORY_FILENAME, payload)


@contextmanager
def maybe_transaction(
    storage: Storage, retention: HistoryRetention | None = None
) -> Iterator[None]:
    """Run the block in one recorded transaction iff *storage* supports transactions.

    Non-transactional backends are a complete no-op. Otherwise the whole begin, body, commit,
    and history append run under `history_lock`, so concurrent writers on the same root cannot
    collide on ids or overwrite the history. On an exception in the body the staged footmarks
    are discarded; a failing discard is logged and the original exception propagates unchanged.
    A failing commit propagates with nothing recorded. After a failed discard or commit the
    transaction is abandoned with its footmarks kept, so the instance can begin again.
    A recorded commit is then pruned under the same lock when *retention* asks for it, reusing
    the commit's listing; a prune failure is logged and swallowed because the commit is
    already durable → [STORAGE.md](STORAGE.md#failure-semantics).

    Args:
        storage: Any storage backend.
        retention: History retention policy, or `None` to record without pruning.

    Yields:
        None — control returns to the caller inside the transaction.
    """
    if not is_transactional(storage):
        yield
        return

    history = TransactionHistory(storage)
    with history_lock(storage):
        records = history.load()
        model = storage.transaction_begin(transaction_id=history.next_id(records))
        try:
            yield
        except BaseException:
            try:
                storage.transaction_discard()
            except Exception:
                logger.exception("Failed to discard transaction during rollback")
                storage._transaction_abandon()
            raise
        # The commit hook, not `transaction_commit`: it also returns the listing the prune
        # reuses → [STORAGE.md](STORAGE.md#backend-primitives). A failed commit is abandoned,
        # not discarded: a discard would delete the BACKUPs of originals already overwritten.
        try:
            commit = storage._commit_listed()
        except BaseException:
            storage._transaction_abandon()
            raise
        if not commit.changed:
            return
        history.append(model, records)
        if retention is None or not retention.enabled:
            return
        try:
            history._prune_locked(storage, retention, [*records, model], listing=commit.hidden)
        except Exception:
            logger.warning(
                "Failed to prune transaction history after transaction %d",
                model.id,
                exc_info=True,
            )
