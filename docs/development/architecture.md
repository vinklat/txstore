# Architecture

How the modules of `txstore` depend on each other, why no import is deferred, and where
configuration enters. The rules are owned by [STRUCTURE.md](../../STRUCTURE.md#dependencies) and
[CONFIG.md](../../CONFIG.md#resolution); the user view is
[introduction.md](../guides/introduction.md).

## Code map

| Concern | Module | Notes |
| -- | -- | -- |
| Public surface | `txstore/__init__.py` | Re-exports; `__all__` is the API. |
| ABI | `txstore/base.py` | `Storage`, exceptions, `transfer()`. |
| Construction | `txstore/factory.py` | `make_storage()`; reads `TX_STORE_*` through `_env`. |
| Transactions | `txstore/transaction.py` | `FootmarkMixin` owns the algorithm; backends supply primitives. |
| Reclamation | `txstore/_reclaim.py` | `ReclaimMixin`: footmark classification and reclamation. |
| History | `txstore/history.py` | Records, retention, `history_lock`. |
| Wire and time | `txstore/_footmark_wire.py`, `txstore/_timestamp.py` | Pure, dependency-free. |
| Environment | `txstore/_env.py` | The single `os.environ` reader. |

## Module graph

```plantuml
package leaves {
  [_env]
  [_timestamp]
  [_footmark_wire]
  [_s3_client]
  [_capabilities]
  [hexdigest]
  [metrics]
}
[base] --> [hexdigest]
[base] --> [metrics]
[url] --> [base]
[_reserved] --> [base]
[_fs_paths] --> [base]
[_s3_acl] --> [base]
[_s3_acl] --> [_s3_client]
[_s3_errors] --> [base]
[_s3_errors] --> [_s3_client]
[_s3_lock] --> [base]
[_s3_lock] --> [_s3_client]
[_s3_lock] --> [_s3_errors]
[footmark] --> [_footmark_wire]
[footmark] --> [_timestamp]
[transaction] --> [base]
[transaction] --> [footmark]
[transaction] --> [_reserved]
[transaction] --> [_reclaim]
[_reclaim] --> [footmark]
[_reclaim] --> [_reserved]
[history] --> [base]
[history] --> [footmark]
[history] --> [_timestamp]
[history] --> [transaction]
[history] --> [_env]
[history] --> [_reserved]
[storage_fs] --> [transaction]
[storage_fs] --> [_fs_paths]
[storage_fs] --> [_reserved]
[storage_s3] --> [transaction]
[storage_s3] --> [_s3_client]
[storage_s3] --> [_s3_acl]
[storage_s3] --> [_s3_errors]
[storage_s3] --> [_s3_lock]
[storage_s3] --> [_reserved]
[storage_http] --> [_capabilities]
[storage_null] --> [_capabilities]
[factory] --> [storage_fs]
[factory] --> [storage_s3]
[factory] --> [storage_http]
[factory] --> [_env]
[factory] --> [_s3_client]
```

### Details that matter

- The graph leaves out the edges from each backend and `factory` into `base` and `url`; every other
  import is drawn → [STRUCTURE.md](../../STRUCTURE.md#dependencies).
- `maybe_transaction` lives in `history` beside `TransactionHistory` and `history_lock`, so the
  dependency runs one way, `history` → `transaction`, and no import is deferred;
  `tests/test_package.py` fails on a new one.
- `history_lock` enters the backend's `_obj_lock` primitive (`flock` in `storage_fs`, `s3_lock` from
  `_s3_lock` in `storage_s3`), and the reserved-object ACL belongs to `StorageS3`, so `history`
  imports no backend and no `_s3_*` module → [STRUCTURE.md](../../STRUCTURE.md#dependencies).
- Configuration never enters at import: `_env` is read by `make_storage` and
  `HistoryRetention.from_env` on each call, so a test or caller can set variables at any time.
- The `mypy_boto3_s3` stubs appear only under `TYPE_CHECKING`; the runtime dependency set stays
  small enough for Debian testing → [SETUP.md](../../SETUP.md#dependency-policy).

## Testing notes

`tests/test_package.py` walks the source with `ast` and asserts every rule of the graph above: the
leaves, the façade direction, who may import a backend, the deferred-import set, the runtime
dependency allowlist, and the single environment reader.

## Related contracts

- [STRUCTURE.md](../../STRUCTURE.md) owns layout and import directions;
  [STYLE.md](../../STYLE.md#python-baseline) owns the deferred-import rule.
- [CONFIG.md](../../CONFIG.md) owns the settings `make_storage` resolves.
- Sibling pages: [class.md](class.md), [transactions.md](transactions.md), [s3.md](s3.md),
  [footmark.md](footmark.md).
