# Introduction

txstore gives a filesystem directory and an S3 bucket one storage interface, and lets you group
writes to either into a transaction: the writes are staged and applied together on commit, a failing
block discards them, and a committed transaction can be reverted later. If the backend fails partway
through a commit, some writes may already be applied → [transactions.md](transactions.md).

## The ideas

- **One interface.** `get`, `put`, `delete`, `list`, `copy`, streams, and metadata behave the same
  on every backend that supports them.
- **Footmarks.** Inside a transaction a write goes to a hidden sibling object, a *footmark*. Commit
  copies the footmarks over the originals; discard deletes them. Reads through the same storage
  instance already see the staged state.
- **History.** Each committed transaction is recorded in `.transactions.json` at the storage root,
  so the latest one can be reverted and old ones expire by a retention policy.
- **Streaming digests.** `transfer` copies one object between any two storages in a single pass and
  returns its MD5, SHA-1, SHA-256, and SHA-512 digests.

## End-to-end example

```python
from txstore import HistoryRetention, TransactionHistory, make_storage, maybe_transaction

src = make_storage("https://downloads.example.com/releases")
dst = make_storage("fs:///srv/data", create_path=True)

with maybe_transaction(dst, HistoryRetention.from_env()):
    digests = src.transfer("v1.2/app.tar.gz", dst)
    dst.put("v1.2/app.tar.gz.sha256", digests["SHA256"].encode())

history = TransactionHistory(dst)
print([(t.id, t.timestamp.to_json()) for t in history.load()])  # [(1, '...Z')]
history.revert_last()  # both files are gone again
```

If the body raises, nothing is written: the staged footmarks are discarded and the exception
propagates.

## Errors you will see

| Error | When |
| -- | -- |
| `StorageException` | A backend operation, transaction, history, or lock step failed. |
| `StorageNotFoundException` | A missing object → [STORAGE.md](../../STORAGE.md#storage-abi); a `StorageException`. In a transaction → [STORAGE.md](../../STORAGE.md#footmarks). |
| `ValueError` | A bad URL scheme, ACL name, environment value, or retention value. |
| `NotImplementedError` | The backend lacks the operation (writes on HTTP, reads on the null sink). |

## Further reading

- [STORAGE.md](../../STORAGE.md) for every behavior above.
- [EXCEPTIONS.md](../../EXCEPTIONS.md) for the full error list.
