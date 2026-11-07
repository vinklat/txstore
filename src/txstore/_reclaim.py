"""Footmark classification and reclamation, inherited by `FootmarkMixin`.

Finds the footmark objects of recorded transactions and orphaned ones, and deletes them
best-effort, for retention and the orphan sweep → [STORAGE.md](STORAGE.md#lifecycle). The class
touches no transaction state, only the three host members it declares for the type checker.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from typing import TYPE_CHECKING

from txstore._reserved import is_reserved_path
from txstore.footmark import ObjectOperation, parse_footmark_token, split_footmark_path

if TYPE_CHECKING:
    from txstore.transaction import TransactionModel

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class ReclaimMixin:
    """Footmark reclamation for `FootmarkMixin`, its only subclass."""

    if TYPE_CHECKING:  # provided by `FootmarkMixin` and the backend primitives

        def _require_idle(self, action: str) -> None: ...

        def hidden_objects(self) -> list[str]: ...

        def _obj_reclaim_delete_many(
            self, rels: Sequence[str]
        ) -> tuple[list[str], list[tuple[str, str]]]: ...

    def _delete_footmark_paths(
        self,
        rels: Iterable[str],
        context: str,
        models: Sequence[TransactionModel] = (),
    ) -> list[str]:
        """Best-effort delete of footmark objects, the BACKUPs of *models* last.

        A footmark that resists deletion is logged and skipped: the caller is
        reclaiming space, so one stubborn object must not abort the rest or
        fail the operation that triggered the reclaim. A revert infers a created
        object from a PUT footmark without its BACKUP, so a BACKUP goes only after
        every other footmark of its original, and stays when one of them resisted:
        a reclaim cut short never leaves a later revert deleting a restored original
        → [STORAGE.md](STORAGE.md#lifecycle).

        Args:
            rels: Relative paths of footmark objects to remove.
            context: Short label for the log lines (`txn 7`, `orphan sweep`).
            models: Transactions whose BACKUP footmarks are among *rels*; an orphan sweep
                passes none, since no record can revert an orphan.

        Returns:
            The relative paths actually deleted, sorted.
        """
        backup_tokens = {model.footmarks[ObjectOperation.BACKUP.value] for model in models}
        backups: list[tuple[str, str]] = []  # (footmark, its original)
        others: list[str] = []
        for rel in sorted(rels):
            split = split_footmark_path(rel)
            if split is not None and split.token in backup_tokens:
                backups.append((rel, split.original))
            else:
                others.append(rel)
        removed, failed = self._obj_reclaim_delete_many(others)
        resisted = {split.original for rel, _ in failed if (split := split_footmark_path(rel))}
        kept = [rel for rel, original in backups if original in resisted]
        removed_backups, failed_backups = self._obj_reclaim_delete_many(
            [rel for rel, original in backups if original not in resisted]
        )
        for rel, reason in [*failed, *failed_backups]:
            logger.warning("[%s] Failed to remove footmark %s: %s", context, rel, reason)
        for rel in kept:
            logger.warning("[%s] Kept BACKUP footmark %s beside one that resisted", context, rel)
        removed = [*removed, *removed_backups]
        for rel in removed:
            logger.debug("[%s] Removed footmark: %s", context, rel)
        # A batched backend answers in its own order.
        return sorted(removed)

    def _footmark_candidates(self, listing: Sequence[str] | None) -> Iterator[tuple[str, str]]:
        """Yield ``(rel, token)`` for every hidden object that may be a footmark.

        Shared filter of the reclamation helpers: a `.<file>.<token>` basename, split by
        `split_footmark_path` as `_categorize` splits it, that is not a reserved object. Whether
        the segment is a token stays with the caller, since a forget knows the tokens it wants
        and an orphan sweep does not.

        Args:
            listing: Hidden relative paths to classify, a full `hidden_objects` walk or the
                paths a `find_*` call returned; ``None`` walks `hidden_objects` once.
        """
        for rel in self.hidden_objects() if listing is None else listing:
            split = split_footmark_path(rel)
            if split is None or is_reserved_path(rel):
                continue
            yield rel, split.token

    def find_transaction_footmarks(
        self, models: Sequence[TransactionModel], *, listing: Sequence[str] | None = None
    ) -> list[str]:
        """Return the footmark objects belonging to *models*.

        A hidden object belongs to a model iff its footmark path carries one of
        the model's tokens. That is the split `_categorize` classifies a live
        transaction with, so a reclaim and a revert agree about what a token owns.
        Reserved objects are never matched. The tokens are a set, so the match is linear
        in the listing, not in the models.

        Args:
            models: Transactions whose footmarks are wanted.
            listing: A full `hidden_objects` walk taken by the caller, so one listing serves
                several classifiers; ``None`` walks once.

        Returns:
            Relative paths, sorted.

        Raises:
            StorageException: If a transaction is active on this instance. The
                active transaction's own footmarks would be indistinguishable
                from the ones looked up.
        """
        self._require_idle("forget transactions")

        tokens = {token for model in models for token in model.footmarks}
        if not tokens:
            return []

        return sorted(rel for rel, token in self._footmark_candidates(listing) if token in tokens)

    def transaction_forget(
        self, models: Sequence[TransactionModel], *, paths: Sequence[str] | None = None
    ) -> list[str]:
        """Drop every footmark object belonging to *models*.

        Unlike `transaction_revert` this touches no original object: the
        transactions stay committed and merely stop being revertable. Retention
        uses it to reclaim the footmarks of records it is about to remove from
        the history.

        Args:
            models: Transactions whose footmarks are no longer needed.
            paths: The paths `find_transaction_footmarks` returned for *models*. They are
                re-checked against the models' tokens without listing, so a path that is not
                theirs is spared; ``None`` finds them with one listing.

        Returns:
            The relative paths actually deleted, sorted. A delete that fails is
            logged and skipped, so this can be shorter than the paths found.

        Raises:
            StorageException: If a transaction is active on this instance.
        """
        doomed = self.find_transaction_footmarks(models, listing=paths)
        return self._delete_footmark_paths(doomed, f"forget {len(models)} txn", models)

    def find_orphaned_footmarks(
        self, known: Iterable[Sequence[str]], *, listing: Sequence[str] | None = None
    ) -> list[str]:
        """Return footmark objects that belong to no recorded transaction.

        Residue of a writer that died between begin and commit, of a discarded
        transaction whose cleanup failed, or of a history file that was moved
        away to recover from corruption. Only a segment that decodes as a
        footmark this build could have written is reported → `parse_footmark_token`;
        reserved objects and every other dotfile are left alone.

        The caller must exclude concurrent writers (retention holds
        `history_lock`), otherwise a peer's in-flight footmarks look orphaned.

        Args:
            known: Footmark token lists of the transactions still recorded.
            listing: A full `hidden_objects` walk taken by the caller, so one listing serves
                several classifiers; ``None`` walks once.

        Returns:
            Relative paths of orphaned footmark objects, sorted.

        Raises:
            StorageException: If a transaction is active on this instance.
        """
        self._require_idle("sweep orphans")

        known_tokens = {token for tokens in known for token in tokens}
        orphans: list[str] = []
        for rel, token in self._footmark_candidates(listing):
            # A sweep has no token to match against, so it demands that the segment decode.
            if token in known_tokens or parse_footmark_token(token) is None:
                continue
            orphans.append(rel)
        return sorted(orphans)

    def transaction_forget_orphans(
        self, known: Iterable[Sequence[str]], *, orphans: Sequence[str] | None = None
    ) -> list[str]:
        """Delete every footmark object `find_orphaned_footmarks` reports.

        Args:
            known: Footmark token lists of the transactions still recorded.
            orphans: The paths `find_orphaned_footmarks` returned for *known*. They are
                re-checked without listing, so a recorded or foreign path is spared; ``None``
                finds them with one listing.

        Returns:
            The relative paths actually deleted, sorted.

        Raises:
            StorageException: If a transaction is active on this instance.
        """
        doomed = self.find_orphaned_footmarks(known, listing=orphans)
        return self._delete_footmark_paths(doomed, "orphan sweep")
