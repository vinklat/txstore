# Class model

The `Storage` hierarchy: the abstract base, the transaction mixin, the capability stubs, and the
four backends. This page is a class diagram plus commentary rather than a flow. Every rule shown is
owned by [STORAGE.md](../../STORAGE.md); the user view is [backends.md](../guides/backends.md).

## Code map

| Concern | Module | Notes |
| -- | -- | -- |
| ABI and shared types | `txstore/base.py` | `Storage`, `StorageException`, `StorageNotFoundException`, `StorageType`, `transfer()` |
| Transactions | `txstore/transaction.py` | `FootmarkMixin`, `TransactionModel`, `is_transactional` |
| Reclamation | `txstore/_reclaim.py` | `ReclaimMixin`, inherited by `FootmarkMixin` |
| Capability stubs | `txstore/_capabilities.py` | `ReadOnlyStorageMixin`, `WriteOnlyStorageMixin`, `TransactionNotSupportedMixin` |
| Backends | `txstore/storage_fs.py`, `storage_s3.py`, `storage_http.py`, `storage_null.py` | Concrete classes |

## Class diagram

```plantuml
abstract class Storage {
  metrics: StorageMetrics
  + put(path, data, [metadata], [acl])
  + transfer(path_src, storage_dst, [path_dst], [acl])
  -- abstract --
  + list() / get(path) / get_stream(path) / exists(path)
  + put_stream(path, stream, [metadata], [acl])
  + get_metadata(path) / add_metadata(path, metadata, [acl])
  + delete(path) / delete_metadata(path) / delete_metadata_fields(path, keys)
  + copy(path_src, path_dst) / move(path_src, path_dst)
  + mkdir(path) / rmdir(path)
  + format() / get_type()
}

abstract class FootmarkMixin {
  transaction_id: int
  _active: TransactionModel | None
  + in_transaction() / transaction_begin() / transaction_commit()
  + transaction_revert(transaction) / transaction_discard()
  + hidden_objects()
  -- overlay --
  # _read_target / _source_target / _overlay_exists / _overlay_list
  # _stage_write / _stage_delete / _metadata_target / _copy_object / _move_object
  # _commit_listed() / _reclaim_footmark()
  -- abstract primitives --
  # _obj_full / _obj_iter_rel_all / _obj_exists / _obj_copy / _obj_delete / _obj_touch
  # _obj_lock / _obj_lock_identity / _obj_get_reserved / _obj_put_reserved
  -- primitives with defaults --
  # _obj_same / _obj_rename / _obj_prepare_parent / _obj_checked_footmark
  # _obj_reclaim_delete_many / _obj_check_reserved / _obj_check_transfer
}

class ReclaimMixin {
  + find_transaction_footmarks(models) / transaction_forget(models)
  + find_orphaned_footmarks(known) / transaction_forget_orphans(known)
  # _delete_footmark_paths(rels, context)
}

class ReadOnlyStorageMixin
class WriteOnlyStorageMixin
class TransactionNotSupportedMixin

class StorageFS {
  base_dir: str
}
class StorageS3 {
  endpoint: str
  bucket: str
  prefix: str | None
  _s3_client: S3Client
  _acls: dict[str, str]
  + create_bucket() / delete_bucket() / verify_public(path)
}
class StorageHTTP {
  base_url: str
}
class StorageNull

Storage <|-- StorageFS
Storage <|-- StorageS3
Storage <|-- StorageHTTP
Storage <|-- StorageNull
ReclaimMixin <|-- FootmarkMixin
FootmarkMixin <|-- StorageFS
FootmarkMixin <|-- StorageS3
ReadOnlyStorageMixin <|-- StorageHTTP
WriteOnlyStorageMixin <|-- StorageNull
TransactionNotSupportedMixin <|-- StorageHTTP
TransactionNotSupportedMixin <|-- StorageNull
```

### Details that matter

- `maybe_transaction` and `is_transactional` test `isinstance(storage, FootmarkMixin)`, not a
  structural check: `TransactionNotSupportedMixin` exposes the same method names as
  `NotImplementedError` stubs, so a structural check would also match the HTTP and null backends.
- The algorithm and the overlay live once in `FootmarkMixin`; a backend's public methods route
  through the overlay helpers, and the backend only maps relative paths to its own keys and supplies
  the primitives → [STORAGE.md](../../STORAGE.md#backend-primitives).
- `put` is shared: `Storage.put` is a `put_stream` of the bytes, so the byte count lives in one
  place per backend; the reserved-path guard is the `_obj_check_reserved` primitive, its callers →
  [STORAGE.md](../../STORAGE.md#backend-primitives); FS overrides it to resolve symlinks and refuse
  the root → [VALIDATION.md](../../VALIDATION.md#filename-safety).
- `format()` differs per backend on purpose: deleting a local tree is too easy to misuse, while
  recreating a bucket is the S3-native reset, refused when a prefix is set since the bucket holds
  keys outside the root → [STORAGE.md](../../STORAGE.md#backends).
- `StorageS3.put_stream` keeps the ABI signature; the post-write public probe is a constructor flag,
  not a write argument → [STORAGE.md](../../STORAGE.md#s3-acl-rules).

## Testing notes

`tests/test_capabilities.py` proves the stubs; `tests/test_base.py` the shared `put` and `transfer`;
`tests/test_storage_*.py` each backend; `tests/test_transactions.py` the mixin on the in-memory
`MemoryTxnStorage` from `tests/helpers/storage.py` as well as on FS and S3;
`tests/test_transaction.py` `maybe_transaction` with stubs.

## Related contracts

- [STORAGE.md](../../STORAGE.md#storage-abi) owns the ABI and every backend row.
- [EXCEPTIONS.md](../../EXCEPTIONS.md#storage-exceptions) owns the exception classes.
- Sibling pages: [architecture.md](architecture.md), [transactions.md](transactions.md),
  [s3.md](s3.md), [footmark.md](footmark.md).
