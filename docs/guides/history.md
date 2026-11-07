# History and retention

The transaction log at the storage root: listing it, reverting the latest transaction, pruning old
ones, and the lock that serializes writers.

## The log

`maybe_transaction` appends `{"id": ..., "timestamp": ...}` to `.transactions.json` for every commit
that changed something. The file and its lock, `.transactions.lock`, are reserved names, matched
whatever their case: writes, deletes, metadata calls, and `move` refuse them, and `copy` refuses
them as a destination → [VALIDATION.md](../../VALIDATION.md#filename-safety).

```python
from txstore import TransactionHistory

history = TransactionHistory(storage)
history.load()  # [TransactionModel(id=1, timestamp=Timestamp(...)), ...]
history.revert_last()  # undo the newest transaction, drop its record, and return it
history.pop_last()  # drop the newest record without touching objects
```

Revert is latest-only; walk back by calling it repeatedly.

## Retention

Old records and their footmarks are pruned by age, with a floor of the `keep` highest transaction
ids that never expire:

```python
from txstore import HistoryRetention

policy = HistoryRetention(keep=20, max_age_days=90)  # explicit
policy = HistoryRetention.from_env()  # TXSTORE_HISTORY_* or the defaults
history.prune(policy)  # PruneResult(records, footmark_paths, orphans)
history.prune(policy, orphans=True, dry_run=True)  # also list leftover footmarks, change nothing
```

Passing a policy to `maybe_transaction` prunes after each recorded commit. `max_age_days=0` disables
expiry; an explicit `prune(..., orphans=True)` still sweeps orphans →
[STORAGE.md](../../STORAGE.md#retention). A pruned transaction stays applied; it just cannot be
reverted any more.

The orphan sweep (`orphans=True`) removes footmarks that belong to no record: leftovers of a crashed
writer, a failed discard or commit, a raw `transaction_begin` / `transaction_commit` (which records
nothing, so its model stops being revertable), or a history file you moved away. It only runs when
you ask for it.

## The writer lock

| Backend | Lock |
| -- | -- |
| Filesystem | `flock` on `.transactions.lock`; waits until free. |
| S3 | A conditional write of `.transactions.lock`; retries until the acquire timeout, and takes over a lock whose TTL has passed → [STORAGE.md](../../STORAGE.md#locking). |

The lock covers the whole `maybe_transaction` block, so a long transfer holds it for its duration.
It is not re-entrant: `maybe_transaction`, `revert_last`, `pop_last`, or `prune` called inside a
block on the same root raises `StorageException` instead of waiting, through any instance of the
root (on S3, with the same endpoint spelling) → [STORAGE.md](../../STORAGE.md#locking).

## Errors you will see

| Error | When |
| -- | -- |
| `StorageException` | Malformed history file or record, lock timeout or failure, a nested lock on the same root, revert on a non-transactional backend. |
| `ValueError` | `keep` or `max_age_days` out of range, or a non-integer `TXSTORE_HISTORY_*` value → [VALIDATION.md](../../VALIDATION.md#history-retention). |

## FAQ

**The history file is corrupt; how do I recover?** Move it away outside txstore, for example with
`mv` or the S3 console; `storage.move` refuses reserved names. Writes resume, the recorded
transactions can no longer be reverted, and an orphan sweep reclaims their footmarks.

**A process crashed holding the S3 lock.** It expires once its TTL passes →
[STORAGE.md](../../STORAGE.md#locking). To clear it sooner, once you are sure no writer is running,
delete `.transactions.lock` outside txstore, for example with the S3 console or CLI;
`storage.delete` refuses reserved names.

## Further reading

- [STORAGE.md](../../STORAGE.md#transaction-history) for records, retention, and locking.
- [CONFIG.md](../../CONFIG.md#history-retention) for the retention settings.
