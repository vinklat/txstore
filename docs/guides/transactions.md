# Transactions

How to group writes on a filesystem or S3 storage so they land together, and how to undo them.

## How it works

Inside a transaction, `put`, `delete`, and metadata changes are written to hidden footmark objects
next to their targets. Reads through the same storage instance see the staged state; another
instance, even in the same process, sees the originals until commit. Commit copies the footmarks
over the originals and deletes what was marked for deletion; discard drops the footmarks. Footmarks
stay after commit so the transaction can be reverted, until retention reclaims them.

## The usual way: `maybe_transaction`

```python
from txstore import HistoryRetention, make_storage, maybe_transaction

storage = make_storage("s3://s3.example.com/releases")
with maybe_transaction(storage, HistoryRetention.from_env()):
    storage.put("v1.3/app.tar.gz", payload)
    storage.delete("v1.0/app.tar.gz")
```

- On success the transaction commits and, if it changed anything, is recorded in the history.
- On an exception the staged writes are discarded and the exception propagates unchanged.
- On a backend without transactions (HTTP, null) the body simply runs.
- Writers on the same storage root are serialized by a lock for the whole block →
  [history.md](history.md).

## Lower level: begin, commit, revert

```python
model = storage.transaction_begin()
storage.put("a.txt", b"1")
storage.transaction_commit()
storage.transaction_revert(model)  # later, with the same model
```

Raw calls leave no history record, so `TransactionHistory.revert_last` cannot see them. Prefer
`maybe_transaction` unless you manage the model yourself.

## Rules worth knowing

| Situation | Result |
| -- | -- |
| `put` then `delete` of the same path | No object after commit. |
| `delete` then `put` | A new object after commit. |
| Metadata change | The original is backed up first, so revert restores it. |
| Two transactions on one instance | Refused: one active transaction per instance. |
| Backend fails during commit | Atomicity is best-effort; some objects may already be applied. |

## Errors you will see

| Error | When |
| -- | -- |
| `StorageException` | Begin or revert while a transaction is active, commit or discard with none active, reading, copying, moving, or changing the metadata of an object deleted earlier in the same transaction, a malformed history file, a backend failure. |
| `StorageNotFoundException` | Deleting an object with neither an original nor a staged write, or copying or moving a source that does not exist as the transaction sees it. |
| `NotImplementedError` | Transaction calls on HTTP or null storage, except `in_transaction()`, which returns `False`. |

## Further reading

- [STORAGE.md](../../STORAGE.md#transactions), including the failure semantics.
- [ROADMAP.md](../../ROADMAP.md#known-deviations) for the atomicity limits.
