# txstore roadmap

Planned releases, deferred work, and places the code does not yet meet a contract. Shipped work →
[CHANGELOG.md](CHANGELOG.md). Nothing here is dated or committed.

## Releases

| Version | Goal | Blocked by |
| -- | -- | -- |
| 0.2.0 | Debian package `python3-txstore` for forky, maintained by the Debian Python Team → [SETUP.md](SETUP.md#debian-package) | Team membership, ITP, team repository on salsa, team sponsor, upload to unstable, migration to testing before the forky soft freeze |
| 0.2.1 | A lock put that fails in transport rereads the lock and holds it when the body carries its own `owner`, instead of aborting with the body left inside its TTL → [STORAGE.md](STORAGE.md#locking) | — |
| 1.0.0 | Stable API: the façade frozen before the forky freeze, so forky ships an API maintained for the whole release | Backlog rows whose trigger names 1.0.0 |

## Backlog

Unscheduled and unordered work, each row waiting on a trigger. Promote a row to a release when its
trigger fires or the work is planned; drop it when it no longer applies.

### Features

Changes a caller of the library or a reader of a stored root would see.

| Feature | Revisit when | Area |
| -- | -- | -- |
| Generated API reference in `docs/reference/` | The façade stabilizes (1.0.0) | [STRUCTURE.md](STRUCTURE.md#layout) |
| GCS and Azure Blob backends | A user needs transactions on another object store | [STORAGE.md](STORAGE.md#backends) |
| Async API; the per-thread writer-lock claim in `history_lock` then becomes per task (a `ContextVar` of a `frozenset`) | A caller needs concurrent transfers without threads | [STORAGE.md](STORAGE.md#storage-abi) |
| One owner of read metrics: `GuardedStream` counts the bytes a caller reads and an object only once its body ends cleanly, so read objects and read errors stay disjoint | A caller needs read metrics from `get_stream` itself, or with the mixin-owned pipeline (1.0.0) | [STORAGE.md](STORAGE.md#storage-abi) |
| Total deadline on `get`, `get_stream`, and `transfer` | A hostile origin trickles a body under the 0.1.3 size cap slowly enough to stall a caller | [STORAGE.md](STORAGE.md#storage-abi) |
| Renewable S3 lock | Critical sections approach the S3 lock TTL | [STORAGE.md](STORAGE.md#locking) |
| Transactional writes as one manifest object | Partial commits on unreachable backends cost real data | [STORAGE.md](STORAGE.md#failure-semantics) |
| Commit and discard from the transaction's own footmarks, not a full listing; auto-prune lists only when a record expires | Roots reach ~100k keys: every recorded write lists every key under the root while holding `history_lock`; this also retires the commit listing the prune reuses | [STORAGE.md](STORAGE.md#backend-primitives) |
| Footmarks under a dedicated namespace prefix, so commit, revert, prune, and sweeps list only footmarks | Commit and discard from the transaction's own footmarks is not enough: a footmark layout change, cheapest before 1.0.0 | [STORAGE.md](STORAGE.md#footmarks) |
| The overlay records which of its own footmarks exist, so a staged write probes only the original | S3 syncs of thousands of puts: a staged put of a new key costs three HEAD requests (BACKUP, original, DELETE marker) under `history_lock` | [STORAGE.md](STORAGE.md#footmarks) |
| A pooled `requests.Session` per `StorageHTTP` instance, closed through a backend `close()` lifecycle | A caller syncs many small objects from an https mirror: every request opens a new connection and TLS handshake, since `requests.request` builds a throwaway session; netrc stays read per request, so an edit takes effect at once | [STORAGE.md](STORAGE.md#backends) |
| Lazy probe that the S3 endpoint enforces conditional writes | A user reports lost writes on a non-AWS endpoint; one probe on first lock, never in the constructor | [STORAGE.md](STORAGE.md#locking) |
| `StorageFS` without symlink resolution on mutation or read | A shared root with untrusted writers: a directory swapped for a link between `validate_path` and a write, read, or unlink is followed; closing it needs one root object that opens, unlinks, renames, and stats by relative path through `openat`-style descriptors, replacing per-call-site checks | [VALIDATION.md](VALIDATION.md#filename-safety) |
| `StorageFS` refuses a hard-linked object, footmark, or `.transactions.lock` | A shared root with untrusted writers where `fs.protected_hardlinks` is off or absent (macOS, the BSDs): a hard link to a file outside the root is read as an object and copied by commit and revert, and a linked `.transactions.lock` puts the history `flock` on the outside file, a lock denial of service but no write; refusing `st_nlink > 1` would also refuse roots backed up with hard-link snapshots | [VALIDATION.md](VALIDATION.md#filename-safety) |
| `StorageFS` commit pre-flight that refuses a staged file whose path became a directory | A caller stages a file at `d` and then `d/child` or `mkdir("d")` in one transaction: commit fails part-way in set order | [STORAGE.md](STORAGE.md#failure-semantics) |
| Staged `mkdir` / `rmdir` on `StorageFS` | A caller needs a directory change that discard or revert undoes: both act immediately inside a transaction | [STORAGE.md](STORAGE.md#failure-semantics) |
| Reclaim `StorageFS` write temp files left by a crash | Leftovers accumulate; they are hidden from `list()` but never deleted | [STORAGE.md](STORAGE.md#backend-primitives) |
| Fixed `0600` mode for `StorageFS` history and lock files | A root is shared with users who must not read the audit log; today the umask applies as for objects | [VALIDATION.md](VALIDATION.md#filename-safety) |
| Maintenance CLI for transactional roots: retention prune, orphan check and sweep, temp-file cleanup, history inspection | Operators run these from ad-hoc scripts; the CLI needs its own entry point and a layout decision | [STORAGE.md](STORAGE.md#retention) |

### Internal design

Restructuring that adds no capability; some rows reshape the public API before 1.0.0.

| Change | Revisit when | Area |
| -- | -- | -- |
| Split `Storage` into readable, writable, and transactional interfaces | Before freezing the façade (1.0.0): the read-only and write-only mixins exist mainly to stub half of a 17-method ABC, and `TransactionNotSupportedMixin` only stubs transaction names on non-transactional backends; the split also adds the public transactional type that `is_transactional` narrows to, leaving `FootmarkMixin` an implementation mixin | [STORAGE.md](STORAGE.md#storage-abi) |
| Inject `S3Client` into `StorageS3`; no network I/O in the constructor | A caller needs a fake client or lazy bucket creation; `create_bucket` runs in `__init__` today | [STORAGE.md](STORAGE.md#construction) |
| Bind the bucket inside `S3Client` | The `S3Client` surface is next refactored: every call passes the same bucket, and `_LockObject` and `is_public` carry it too; `_LockObject` and its eight helpers then become one `_S3Lock` class | [STRUCTURE.md](STRUCTURE.md#layout) |
| Move the canned-ACL choice sets, parsers, and public-ACL set beside the `S3Client` literals, behind an `AclPolicy` holding the default, bucket, reserved, and verify-public settings | Binding the bucket inside `S3Client` lands: ACL rules span `_s3_client`, `storage_s3`, `_s3_acl`, and `factory`, the four settings always travel together, and `acl: str \| None` means an octal mode on `fs` and a canned ACL on `s3` | [STORAGE.md](STORAGE.md#s3-acl-rules) |
| Public commit hook: `maybe_transaction` calls `transaction_commit()`, which keeps its `bool` result, and `_commit_listed` goes away | A subclass needs a commit hook, or before freezing the façade (1.0.0): today the hook is the private `_commit_listed`. Lands with or after commit and discard from the transaction's own footmarks, which retires the `hidden` listing `_commit_listed` hands the prune, so that listing never becomes public; a result tuple would also be truthy whatever `changed` is | [STORAGE.md](STORAGE.md#backend-primitives) |
| Mixin-owned public read, write, delete, and metadata pipeline; backends supply primitives only | With the interface split (1.0.0): backends still sequence path validation, overlay call, ACL sidecar, and metrics by hand; `StorageFS` calls `_validate_path` in every verb, `_obj_full` does not confine, three resolve-under-root checks differ (`validate_path`, `_obj_check_reserved`, `_obj_checked_footmark`), and `url.join_path` keeps an unused `no_root=True` branch | [STORAGE.md](STORAGE.md#backend-primitives) |
| Reclaim as pure classifiers plus one path delete, with a separate prune-plan type | With the interface split (1.0.0): each `find_*` / forget pair re-classifies the paths it is handed, and `PruneResult` is both the plan and the outcome; `_prune_locked` / `_apply_prune` then move from `TransactionHistory` onto the plan type | [STORAGE.md](STORAGE.md#lifecycle) |
| `TransactionHistory` takes a typed transactional port instead of any `Storage` | With the interface split (1.0.0): every history entry point (`history_lock`, `maybe_transaction`, and `TransactionHistory.load` / `save` / `revert_last` / `pop_last` / `prune`) re-checks `is_transactional`, `append` / `save` rely on the caller holding `history_lock`, and history reaches the private `_commit_listed`, `_transaction_abandon`, `_obj_lock`, `_obj_get_reserved`, and `_obj_put_reserved`; a raw `transaction_begin` auto-increments the instance id, which ignores history, while `maybe_transaction` takes `next_id` | [STORAGE.md](STORAGE.md#operations) |
| Per-transaction object attributes owned by `FootmarkMixin` | The mixin-owned pipeline lands: `StorageS3` keeps the ACL sidecar and three `in_transaction()` branches itself | [STORAGE.md](STORAGE.md#s3-acl-rules) |
| Transaction overlay as an object `transaction_begin` installs and commit, discard, or revert remove, over a typed primitives port | The mixin-owned pipeline and per-transaction object attributes land (1.0.0): seven overlay helpers branch on `in_transaction()`, `transaction_begin` resets the staged-footmark ledger by hand, and `FootmarkMixin` holds the lifecycle, overlay, and primitives in one class | [STORAGE.md](STORAGE.md#backend-primitives) |
| A `Footmarks` object on `TransactionModel` with `path_for(operation, path)` and `operation_of(token)`, still a `Sequence[str]` | With the transaction overlay object: two sites index or enumerate the token tuple by `ObjectOperation` value | [STORAGE.md](STORAGE.md#footmarks) |
| A parsed storage-URL value type that renders redacted in `str`, `repr`, and messages, and owns the per-scheme host and userinfo rules | Before freezing the façade (1.0.0), a URL secret leaking at a new site, a third URL backend, or a new authority check outside `url.py`: `parse_storage_url` returns a public tuple, so redaction is applied per call site, `make_storage` and both URL backends re-parse the authority from strings, and each backend constructor repeats the query, host, and userinfo-stripping steps; `redact_url` (public since 0.1.2) stays the string form for any URL, and the type's `str` delegates to it | [VALIDATION.md](VALIDATION.md#storage-urls) |

## Known deviations

Implementation that does not yet meet a root contract. Contracts link here instead of carrying
TODOs; remove a row when the code meets the rule. `Fix` names the release or backlog row that closes
the deviation by its first words and links its section; `—` means none is recorded.

| Contract | Deviation | Source | Fix |
| -- | -- | -- | -- |
| [STORAGE.md](STORAGE.md#recording-rules) | History append after commit, `save` after revert, and a prune's forget-then-rewrite are not atomic with the footmark lifecycle; a crash between them leaves committed objects without a record, or a record whose footmarks are gone. | `txstore/history.py` `maybe_transaction`, `revert_last`, `prune` | — |
| [STORAGE.md](STORAGE.md#lifecycle) | Revert infers a created object from a PUT footmark without its BACKUP, so a BACKUP removed outside txstore (by hand, an S3 lifecycle rule, a peer's orphan sweep during a raw transaction) makes revert delete the original; an explicit created marker in the footmark set would close it. | `txstore/transaction.py` `transaction_revert` | — |
| [STORAGE.md](STORAGE.md#lifecycle) | Off `fs`, commit checks a staged BACKUP footmark only against its listing, so one removed outside txstore after the listing goes unnoticed and commit overwrites the only other copy of its original. | `txstore/transaction.py` `_commit_listed`, `_obj_checked_footmark` | — |
| [STORAGE.md](STORAGE.md#lifecycle) | Commit takes a staged footmark the listing lacks for an alias once `_obj_exists` finds it, so on `fs` one replaced outside txstore by a directory of its name is skipped instead of refused. | `txstore/transaction.py` `_commit_listed` | — |
| [STORAGE.md](STORAGE.md#failure-semantics) | Transaction atomicity is best-effort when the backend fails mid-commit or mid-discard; remote state may be left partially updated. | `txstore/transaction.py` `FootmarkMixin` | Transactional writes as one manifest object → [features](#features) |
| [STORAGE.md](STORAGE.md#failure-semantics) | `maybe_transaction` reads the history before beginning, so a malformed `.transactions.json` blocks every transactional write; nothing bypasses history recording. | `txstore/history.py` `maybe_transaction` | — |
| [STORAGE.md](STORAGE.md#s3-acl-rules) | A copy without a sidecar entry carries only a group canned ACL; `private`, `bucket-owner-*`, or `aws-exec-read` on a key not written in the transaction is reset by a revert, which always copies outside a transaction, or by any other copy outside a transaction. | `txstore/storage_s3.py` `_obj_copy` | — |
| [STORAGE.md](STORAGE.md#locking) | `history_lock` spans the whole `maybe_transaction` body, including bulk transfers, and the whole `prune` run; a peer writer that waits past the S3 acquire timeout fails instead of queueing. | `txstore/history.py` `maybe_transaction`, `prune` | — |
| [STORAGE.md](STORAGE.md#locking) | Expiry is judged by the breaker's clock against the holder's stamp, so a clock skew approaching the 12 h TTL breaks a live lock early or keeps an expired one. | `txstore/_s3_lock.py` `_body_is_breakable` | — |
| [STORAGE.md](STORAGE.md#locking) | The S3 lock stamps `expires_at` once at acquire and never renews it, so a critical section outliving the TTL can be taken over while its holder still runs; a crashed holder must be cleared by deleting `.transactions.lock`. | `txstore/_s3_lock.py` `s3_lock` | Renewable S3 lock → [features](#features) |
| [STORAGE.md](STORAGE.md#locking) | A lock write that commits while every botocore attempt fails in transport aborts the acquire with the body left inside its TTL; peers wait for it to expire. | `txstore/_s3_lock.py` `_try_create`, `_try_take_over` | Release 0.2.1 → [releases](#releases) |
