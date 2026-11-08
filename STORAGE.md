# Storage contract

Owns: the `Storage` ABI and backend primitives, backends and construction, transaction, footmark,
timestamp, and history semantics, S3 ACL and metadata rules, and digests. Routing →
[AGENTS.md](AGENTS.md#canonical-sources).

Callers import from the `txstore` façade, never from a submodule →
[STRUCTURE.md](STRUCTURE.md#layout). Failure classes →
[EXCEPTIONS.md](EXCEPTIONS.md#storage-exceptions).

## Storage ABI

Every backend implements the abstract methods; `put` and `transfer` are shared.

| Method | Contract |
| -- | -- |
| `get_type()` | Backend `StorageType` (`fs`, `s3`, `http`, `null`). |
| `list()` | Relative paths of regular objects; footmarks and hidden objects are excluded. |
| `get(path)` / `get_stream(path)` | Object bytes / binary stream; missing object → `StorageNotFoundException`. |
| `exists(path)` | Existence probe; backend errors other than "not found" → `StorageException`. |
| `get_metadata(path)` | Backend metadata dict (FS xattrs, S3 user metadata; HTTP: `http_status_code` plus `Content-Type`, `Content-Length`, `Content-Encoding`, `Last-Modified`, `ETag`, `Cache-Control` when sent). |
| `get_metadata` failures | Missing object → `StorageNotFoundException`. On `fs`, a filesystem without xattr support yields `{}`; any other xattr failure → `StorageException`. |
| `get_metadata` on `fs` | An xattr whose name or value is not UTF-8, set outside txstore, is left out. |
| Metadata keys on `fs` | Key `k` is the xattr `user.k`, on every OS; `get_metadata` returns only `user.` xattrs with the prefix dropped, and `delete_metadata` removes only those, so a key never reaches `security.`, `trusted.`, or `system.`. |
| `put(path, data, metadata=None, acl=None)` / `put_stream(...)` | Write bytes / stream with optional metadata and a backend ACL (`fs` octal mode → [VALIDATION.md](VALIDATION.md#filename-safety), `s3` canned ACL → [S3 ACL rules](#s3-acl-rules)); `put` is a `put_stream` of the bytes. |
| `put` metadata on `fs` | Best-effort: an xattr failure after the bytes are written is logged, not raised, and the object stays written. |
| `put_stream` from a failing stream | → `StorageException` chained from the stream's error, one write error counted, and no object or temp file left; a `transfer` from `http` meets this when the connection drops. |
| `add_metadata(path, metadata, acl=None)` | Merge metadata onto an existing object. |
| `add_metadata` failures | On a readable backend, a missing object or any backend failure → `StorageException`. On `fs`, a filesystem without xattr support skips the write with a warning. |
| `delete(path)` / `delete_metadata(path)` / `delete_metadata_fields(path, keys)` | Remove the object / all metadata / named keys; on a readable backend, `delete` of a missing object → `StorageNotFoundException`. |
| `delete_metadata` / `delete_metadata_fields` on `fs` | A filesystem without xattr support removes nothing, with a warning; any other xattr failure, a missing object included, → `StorageException`. |
| `delete_metadata_fields` keys | Absent keys are ignored; an empty `keys` list is a no-op that returns before any path, reserved, missing, or staged-delete check. |
| `copy(path_src, path_dst)` / `move(path_src, path_dst)` | Same-backend copy / move; a missing parent of `path_dst` is handled as by `put`, so on `fs` the instance `auto_mkdir` decides. A missing parent with `auto_mkdir=False` → `StorageException`. |
| `copy` / `move` source | A missing source, as an active transaction sees it, → `StorageNotFoundException`, whatever the destination. Checked after the path refusals of [VALIDATION.md](VALIDATION.md#filename-safety) and before the destination's parent is created, so a refused call or a missing source creates no directory; a copy or rename that fails later keeps the parent it created. |
| `move` onto itself | Paths that name one object (same key, or on `fs` a `./` segment, symlink, hard link, or case or Unicode alias the filesystem folds) make `move` a no-op, in or out of a transaction, so a case-only rename on a case-insensitive filesystem does nothing. |
| `move` onto an alias in a transaction | The no-op holds only when the paths name one object as the transaction sees it; a write staged on only one of them, or a staged delete of the destination, makes the move a real move. |
| `mkdir(path)` / `rmdir(path)` | Directory lifecycle; no-ops where the backend has no directories. A file at the `mkdir` path → `StorageException`. |
| `format()` | Destructive reset of the whole storage root; see per-backend rows below. |
| `transfer(path_src, storage_dst, path_dst=None, acl=None)` | Streams one object into another storage in a single pass and returns its digests → [Digests](#digests). |

Streaming: `CHUNK_SIZE` is 1 MiB and HTTP requests use `REQUEST_TIMEOUT` of 30 s. `StorageMetrics`
counts read/write/delete objects, bytes, and errors per instance; each backend counts its own
written bytes in `put_stream`, so a `transfer` counts the source's read and the destination's write
once each. Only the public verbs count: history and lock I/O and the commit, discard, and revert
primitives do not.

## Backends

| Scheme | Backend | Reads | Writes | Transactions | Metadata | Notes |
| -- | -- | -- | -- | -- | -- | -- |
| `fs` | `StorageFS` | yes | yes | yes | xattr | Root is the URL path, `fs:///<absolute path>`; authority and relative path → [VALIDATION.md](VALIDATION.md#storage-urls). Key layout and `format()` → [Backend specifics](#backend-specifics). |
| `s3` | `StorageS3` | yes | yes | yes | user metadata + canned ACL | URL `s3://<endpoint>/<bucket>[/<prefix>]` → [VALIDATION.md](VALIDATION.md#s3-url-path-split). Listings, directories, and `format()` → [Backend specifics](#backend-specifics). |
| `http`, `https` | `StorageHTTP` | yes | no | no | none | `ReadOnlyStorageMixin` + `TransactionNotSupportedMixin`; `list` and every write, metadata, copy, move, directory, and transaction method → `NotImplementedError`, except `in_transaction()`, which returns `False`. Redirects → [VALIDATION.md](VALIDATION.md#http-url-join-safety); URL redaction → [CONFIG.md](CONFIG.md#secrets). |
| `null` | `StorageNull` | no | yes | no | none | `WriteOnlyStorageMixin` + `TransactionNotSupportedMixin`; write-only sink for digest-only transfers; operations → [StorageNull operations](#storagenull-operations). Not URL-routable → [VALIDATION.md](VALIDATION.md#storage-urls). |

An unsupported operation → `NotImplementedError`, never `AttributeError`; the capability mixins in
`txstore/_capabilities.py` provide the stubs.

### Backend specifics

| Backend | Rule |
| -- | -- |
| `fs` key layout | Key `k` lives at `join_path(root, k)`, the real path of `root/k`; paths and an octal `acl` → [VALIDATION.md](VALIDATION.md#filename-safety). |
| `fs` `format()` | → `StorageException`; remove the directory out of band. |
| `s3` listings | Listings, and so `list` and reclamation, see only keys under `<prefix>/`. |
| `s3` directories | `mkdir` and `rmdir` are no-ops. |
| `s3` `format()` | Deletes and recreates the bucket; with a prefix → `StorageException`, since the bucket holds keys outside the root. |

### StorageNull operations

| Operation | Behavior |
| -- | -- |
| Writes, metadata methods, `delete`, `format()`, `rmdir` | No-ops; a write drains its stream and counts the bytes. |
| Every read, `list`, `copy`, `move`, `mkdir`, and transaction method except `in_transaction()`, which returns `False` | → `NotImplementedError`. |

## Construction

`make_storage(url, *, create_path=False, ...)` in `txstore/factory.py` instantiates every URL-routed
backend; `StorageNull` is constructed directly. Resolution order and which callers read variables →
[CONFIG.md](CONFIG.md#resolution); settings → [CONFIG.md](CONFIG.md#storage-construction).

| Input | Rule |
| -- | -- |
| Scheme | Routed by `parse_storage_url()`; accepted URLs and their refusals → [VALIDATION.md](VALIDATION.md#storage-urls). |
| Empty URL | → `StorageException`; class choice → [EXCEPTIONS.md](EXCEPTIONS.md#rules). |
| HTTP URL without host | No hostname (a port or userinfo alone names none) → `StorageException` from the `StorageHTTP` constructor; class choice → [EXCEPTIONS.md](EXCEPTIONS.md#rules). |
| S3 endpoint without host | No hostname (`s3:///bucket`, `s3://:9000/bucket`) → `StorageException` from the `StorageS3` constructor; class choice → [EXCEPTIONS.md](EXCEPTIONS.md#rules). |
| `create_path` | Unset → nothing is created; meaning → [CONFIG.md](CONFIG.md#storage-construction). |
| FS directories | `auto_mkdir=True` creates written objects' parents; `create_base` creates the root itself. |
| FS root | A directly constructed `StorageFS` takes a relative `base_dir`, resolved against the working directory; a relative or empty `fs` URL path → [VALIDATION.md](VALIDATION.md#storage-urls). |
| `verify_ssl` | Meaning and default → [CONFIG.md](CONFIG.md#storage-construction). |
| S3 credentials | Missing or empty access or secret key → `StorageException` naming the missing settings; class choice → [EXCEPTIONS.md](EXCEPTIONS.md#rules). |
| S3 endpoint | `<scheme>://<host[:port]>` from the URL authority; `make_storage` refuses an `s3://` URL holding `@` → [VALIDATION.md](VALIDATION.md#storage-urls). A directly constructed `StorageS3` drops any userinfo from its endpoint, since the keys come from `s3_access` / `s3_secret`. |
| S3 bucket and prefix | Split from the URL path by `parse_s3_url_path()` in `txstore/url.py` → [VALIDATION.md](VALIDATION.md#s3-url-path-split). |
| S3 `default_acl` | Applied to writes that pass no ACL → [S3 ACL rules](#s3-acl-rules); default → [CONFIG.md](CONFIG.md#storage-construction); values → [VALIDATION.md](VALIDATION.md#s3-canned-acls). |
| S3 `bucket_acl` | Applied at bucket creation and never derived from `default_acl`; default → [CONFIG.md](CONFIG.md#storage-construction). |
| S3 bucket ACL scope | A canned ACL grants more on a bucket than on an object: `public-read` lets anyone list every key, and `public-read-write` lets anyone write, overwrite, and delete objects. |

## Transactions

`StorageFS` and `StorageS3` mix in `FootmarkMixin`; `is_transactional(storage)` is the capability
test, an `isinstance` check against `FootmarkMixin`: the `TransactionNotSupportedMixin` stubs carry
the same method names, so a structural (`hasattr`) check would match them. Pending changes are
parked on hidden sibling objects (`<dir>/.<file>.<token>`) until commit. One token per
`ObjectOperation` member (`NOOP`, `BACKUP`, `PUT`, `DELETE`) is a pure function of
`(transaction_id, begin timestamp)`, the frozen `TransactionModel`; only the latter three are ever
written, and `NOOP` keeps the tokens aligned with the wire schema → [Footmarks](#footmarks).

### Lifecycle

| Method | Behavior |
| -- | -- |
| `in_transaction()` | `True` iff a transaction is active on this instance. |
| `transaction_begin(transaction_id=0)` | Materializes the tokens; `0` auto-increments the instance id, which may repeat a recorded id. Returns the frozen `TransactionModel`. Active transaction → `StorageException`; an explicit or auto-incremented id outside the [Record](#record) `id` range → `ValueError`, nothing begun. |
| Transaction identity | The tokens encode the id and the begin timestamp to the microsecond, so two transactions that share an id never share a footmark. |
| `transaction_commit()` | Applies staged PUTs over originals and removes DELETE-marked originals; footmarks stay so revert can undo. Returns `True` iff any PUT, DELETE, or BACKUP footmark existed. No active transaction → `StorageException`. |
| Failed commit | A failing copy or delete → `StorageException`; the transaction stays active, so the commit can be retried. |
| Staged footmark gone at commit | A footmark this transaction staged and missing from a commit attempt's listing → `StorageException` before any write, with the transaction kept, so no attempt skips its PUT, DELETE, or BACKUP. |
| Staged footmarks | Kept on the instance, the only stager of its transaction, and empty at `transaction_begin`. |
| Staging per verb | A write or metadata change stages its PUT and BACKUP; a delete drops its PUT and, for an original, stages its DELETE and BACKUP. |
| Staging cut short | A call refused by a check stages nothing, since its checks precede its first write; one failing later keeps the steps it completed: its BACKUP, a delete's dropped PUT (losing that staged write), or a move's copy leg. |
| Staged footmark under an alias | A footmark staged under one spelling and listed under another that the backend folds to it (a case or Unicode alias on `fs` → [Storage ABI](#storage-abi)) counts as present; dropping it under either spelling unstages both. |
| Staged footmark gone at staging | A metadata change whose staged PUT is gone, or any staging call whose staged BACKUP is gone → `StorageException` before the original is copied, since that copy would replace the staged change. |
| Refused original | Commit checks the original of every listed PUT, DELETE, and BACKUP footmark → [VALIDATION.md](VALIDATION.md#filename-safety) before its first write; a refused original → `StorageException` with nothing applied, since no retry could get past it. |
| Discard after a failed commit | Deletes the BACKUPs of originals the commit already overwrote, so those changes can no longer be reverted. |
| `transaction_revert(transaction)` | Reverses a committed transaction: drops new objects, restores backups, removes its footmarks. Active transaction → `StorageException`. |
| Revert cleanup order | Revert and `transaction_forget` remove BACKUP footmarks after every other footmark of their original, and keep one whose sibling resisted, so a revert retried after a crash never takes a restored original for a created one. |
| Revert without footmarks | A transaction with no footmarks left in storage reverts as a no-op and logs a warning. |
| Failed revert | A failing restore → `StorageException` with the footmarks kept, so the revert can be retried. |
| Refused original on revert | Revert checks the original of every PUT and BACKUP footmark → [VALIDATION.md](VALIDATION.md#filename-safety) before its first write; a refused original → `StorageException` with nothing reverted and the footmarks kept. |
| Footmark gone on commit or revert | Commit's PUT footmark or revert's BACKUP footmark gone between the listing and its copy → `StorageException` with the transaction or its footmarks kept; on `fs` before the first write, elsewhere when copying it fails. |
| BACKUP gone on commit | On `fs`, a BACKUP footmark gone after the listing → `StorageException` before the first write; elsewhere commit goes on → [ROADMAP.md](ROADMAP.md#known-deviations). |
| BACKUP removed outside txstore | Revert loses the original instead of restoring it → [ROADMAP.md](ROADMAP.md#known-deviations). |
| Footmark gone on reclamation or discard | Counted as deleted. |
| `transaction_discard()` | Drops every staged footmark without touching originals. No active transaction → `StorageException`; a failing delete → `StorageException` with the transaction still active. |
| Discard deletes | One `_obj_reclaim_delete_many` call, then each path it failed on retried through `_reclaim_footmark`; every retry runs, the first failure is the one raised, and later ones are logged as warnings. |
| `hidden_objects()` | Returns every hidden object once, sorted: footmarks, reserved objects, and foreign dotfiles alike. The two `find_*` methods below accept it as `listing`, and the matching forget takes the paths they find; reuse rule → [STYLE.md](STYLE.md#performance). |
| `find_transaction_footmarks(models, *, listing=None)` | Footmark objects of committed transactions: hidden, not reserved, footmark path carrying one of the models' tokens; the split `_categorize` classifies with → [Footmarks](#footmarks), so reclaim and revert agree on what a token owns. Active transaction → `StorageException`. |
| `transaction_forget(models, *, paths=None)` | Deletes the models' footmarks; originals are untouched, the transactions stay committed and stop being revertable. Returns the paths deleted; a failed delete is logged and left out. Active transaction → `StorageException`. |
| `transaction_forget` `paths` | The paths `find_transaction_footmarks` returned: re-checked against the models' tokens without listing, and a path that is not theirs is spared. `None` finds them with one listing. |
| `find_orphaned_footmarks(known, *, listing=None)` | Footmark objects of no recorded transaction: hidden, not reserved, footmark path whose token passes acceptance → [Footmarks](#footmarks) and is in no `known` token list. Active transaction → `StorageException`. |
| `find_orphaned_footmarks` matching | A sweep has no token to match, so it decodes; the caller excludes concurrent writers. |
| `transaction_forget_orphans(known, *, orphans=None)` | Deletes the orphaned footmarks and returns the paths deleted. `orphans` are the paths `find_orphaned_footmarks` returned: they are re-checked without listing, and a recorded or foreign path is spared; `None` finds them with one listing. Active transaction → `StorageException`. |

### Footmarks

| Rule | Behavior |
| -- | -- |
| Token | Base64 of the wire encoding below, with `/` replaced by `@` so the token is a valid path segment; it holds no `.`. |
| Path | `<dir>/.<file>.<token>`, built by `footmark_path` and split by `split_footmark_path`, the one reading every classifier shares. A hidden name with an empty `<file>` (`.<token>`) or no trailing segment has no original and is not a footmark. |
| Wire format | Protocol-buffers encoding of `Footmark` in [footmark.proto](docs/reference/footmark.proto): `header` (field 1, `int32`), `timestamp` (field 2, `{seconds int64, nanos int32}`), `transaction_id` (field 3, `int32`), `operation` (field 4, enum). Zero scalars are omitted; `timestamp` is always written. |
| Wire compatibility | `txstore/_footmark_wire.py` encodes byte-identically to protobuf and decodes as proto3 does: last scalar wins, unknown fields and fields of an unexpected wire type are skipped; no protobuf runtime is needed. |
| Wire rejections | Groups, tags above 32 bits, field number 0, truncation, and varints over 10 bytes. |
| Token acceptance | `parse_footmark_token` accepts a segment iff it is strict base64 of a well-formed wire message with matching `header`, positive id, known operation, and valid timestamp, and re-serializing those fields yields the segment exactly; anything else → `None`. |
| Token strictness | Only a segment this build could have written is accepted, so an orphan sweep deletes nothing else. |
| Overlay | Reads, `exists`, and `list` see staged changes while a transaction is active. |
| Reading a staged delete | `get` / `get_stream` of a DELETE-marked object → `StorageException` and counts a read error. |
| Staged delete as a source | `get_metadata`, `copy`, or `move` of a DELETE-marked object → `StorageException`; no read error is counted. |
| Metadata change of a staged delete | `add_metadata`, `delete_metadata`, or `delete_metadata_fields` with non-empty `keys` on a DELETE-marked object → `StorageException`; no footmark is staged. |
| Staged delete | Deleting an object with neither an original nor a staged PUT → `StorageNotFoundException`. |
| DELETE then DELETE | A no-op that keeps the object deleted, unlike a second direct `delete`. |
| PUT then DELETE | Collapses to "no object". |
| DELETE then PUT | Collapses to "new object". |
| DELETE then failed write | A `put`, `copy`, or `move` onto a staged delete that fails keeps the delete staged. |
| DELETE then failed write and restore | If restoring the dropped marker fails too, a warning is logged, the write's own error is raised, and the delete is lost. |
| Metadata change | Stages a BACKUP of the original before mutating metadata. |
| Other dotfiles | `_categorize()` classifies only footmark paths → [Footmarks](#footmarks); `.transactions.json` and `.transactions.lock` are ignored. |
| Reclaim exclusions | Reserved objects and every other dotfile are never deleted. |
| Reclaim confinement | A reclaim or a discard deletes a listed path, not a validated argument, and anyone who can write into the root can create a validly named footmark; the `StorageFS` checks → [VALIDATION.md](VALIDATION.md#filename-safety). |
| Concurrency | One active transaction per instance; instances are not thread-safe. Cross-process serialization → [Locking](#locking). |

### Timestamps

`Timestamp(seconds, nanos)` is the begin instant every token and the history record share.

| Rule | Behavior |
| -- | -- |
| Range | `seconds` within `0001-01-01T00:00:00Z`..`9999-12-31T23:59:59Z`, `0 <= nanos < 10^9`; outside → `ValueError`. |
| `to_json()` | UTC RFC 3339 ending in `Z`, with 0, 3, 6, or 9 fraction digits: the fewest that represent `nanos` exactly. |
| `from_json(text)` | ASCII only: date and time as `%Y-%m-%dT%H:%M:%S`, an optional `.` and up to 9 fraction digits, then `Z` or `±HH:MM` with hours `00`–`23` and minutes `00`–`59`; a lowercase `t`, trailing data, or anything else → `ValueError`. |
| Frozen form | The text form is stored in history and the tokens are rebuilt from it on revert, so both functions match the protobuf `Timestamp` JSON mapping exactly and never change. |
| `now()` | Current UTC time at microsecond precision. |

### Backend primitives

`FootmarkMixin` owns the algorithm: the lifecycle, `_categorize`, `_backup`, and the overlay that a
backend's public methods go through (`_read_target`, `_source_target`, `_overlay_exists`,
`_overlay_list`, `_stage_write`, `_stage_delete`, `_metadata_target`, `_copy_object`,
`_move_object`), and `_reclaim_footmark(rel)`, the final per-footmark delete of reclamation and
discard: `_obj_delete(_obj_checked_footmark(rel))`; one already gone → [Lifecycle](#lifecycle). A
backend gains transactions by inheriting the mixin and implementing the primitives below; those
without a default are abstract, so a backend missing one cannot be constructed:

| Primitive | Purpose |
| -- | -- |
| `_obj_full(rel) -> str` | Relative path → backend full path or key. |
| `_obj_iter_rel_all() -> Iterable[str]` | Every relative path, regular and hidden alike. |
| `_obj_exists(full) -> bool` | "Not found" → `False`; any other backend error → `StorageException`. |
| `_obj_same(full_a, full_b) -> bool` | Distinct keys name one stored object (an alias or a hard link); a missing one → `False`. Defaults to `False`; FS uses `os.path.samefile`. |
| `_obj_copy(src_full, dst_full) -> None` | True-replace copy carrying metadata and canned ACL, so a staged `delete_metadata` propagates on commit → [S3 ACL rules](#s3-acl-rules). |
| `_obj_delete(full) -> None` | Hard delete; an absent object is no error, so commit and revert send no existence check first. |
| `_obj_rename(src_full, dst_full) -> bool` | Native rename for a `move` outside a transaction; `False` → copy then delete. Defaults to `False`; FS uses `os.replace`, which changes nothing when it fails, and returns `False` across devices. FS attributes → [VALIDATION.md](VALIDATION.md#filename-safety). |
| `_obj_checked_footmark(rel) -> str` | Full path of a footmark that came from a listing: commit and revert copy from or check it, reclamation and discard delete it. Defaults to `_obj_full(rel)`. |
| `_obj_checked_footmark` on FS | Refusals → [VALIDATION.md](VALIDATION.md#filename-safety); one gone since the listing → `StorageNotFoundException`. |
| `_obj_reclaim_delete_many(rels) -> (removed, failed)` | Best-effort delete of listed footmarks; `failed` holds `(rel, reason)`. Defaults to one `_reclaim_footmark` per path. |
| `_obj_reclaim_delete_many` overrides | Resolve and count every path as `_reclaim_footmark` does. |
| `_obj_reclaim_delete_many` on S3 | `DeleteObjects` in chunks of 1000 on the keys `_obj_checked_footmark` returns; a refused footmark, per-key `Errors`, and a key in neither list are failures; a chunk whose call fails falls back to per-key deletes. |
| `_obj_touch(full) -> None` | Create an empty marker object (DELETE markers). |
| `_obj_prepare_parent(full) -> None` | Create the parent directory of `full` before a write or a native rename where the backend has directories. Defaults to a no-op, which S3 keeps; FS obeys the instance `auto_mkdir`. |
| `_obj_prepare_parent` callers | Every staged write, `_copy_object` included, after the source check; the staged copy of a metadata change; `_move_object` also before a native rename. |
| `_obj_check_transfer(verb, src_rel, dst_rel) -> None` | Refuses a `copy` / `move` pair the backend cannot carry; `_copy_object` and `_move_object` call it after their reserved checks and before the source check. Defaults to a no-op; FS refuses directories → [VALIDATION.md](VALIDATION.md#filename-safety). |
| `_obj_lock() -> context manager` | The cross-process writer lock of the root; not re-entrant → [Locking](#locking). No default; abstract. |
| `_obj_lock_identity() -> Hashable` | Equal for every instance of one root's lock; `history_lock` claims it for the thread before `_obj_lock` → [Locking](#locking). No default; abstract. |
| `_obj_get_reserved(rel) -> bytes \| None` | Read a reserved root object outside the overlay, `None` when it is missing; `TransactionHistory.load` is its only caller. FS symlink rule → [VALIDATION.md](VALIDATION.md#filename-safety). |
| `_obj_put_reserved(rel, data) -> None` | Write a reserved root object outside the overlay; `TransactionHistory` is its only caller (`save`, `revert_last`, the prune rewrite) → [Files](#files). The backend picks the access: the reserved ACL on S3 → [S3 ACL rules](#s3-acl-rules), the umask on FS. |
| `_obj_check_reserved(rel) -> None` | The reserved-path guard → [VALIDATION.md](VALIDATION.md#filename-safety). Defaults to the lexical reserved-name and object-name checks; FS replaces the object-name check with the resolved-path checks. |
| `_obj_check_reserved` confinement | It is the only root-confinement check commit and revert apply to a footmark's original, so an override in a backend that resolves paths refuses the root and every path resolving outside it. |
| `_obj_check_reserved` callers | `_stage_write`, `_stage_delete`, `_metadata_target`, `_copy_object`, `_move_object`, and commit and revert through `_checked_full` call it; a backend calls it for any mutator that bypasses them (FS `mkdir`, `rmdir`); guarded operations → [VALIDATION.md](VALIDATION.md#filename-safety). |

A failing primitive other than `_obj_exists` may raise its library's error; the overlay helpers
raise it → `StorageException`, chained and naming the operation →
[EXCEPTIONS.md](EXCEPTIONS.md#rules).

`transaction_commit()` delegates to `_commit_listed()`, which also returns the hidden objects the
commit listed; `maybe_transaction` calls `_commit_listed()` to pass that listing to the prune, so a
subclass hooks commit there, not in `transaction_commit`. Commit creates and deletes no footmark, so
the listing stays exact for the footmark classifiers; a dotfile original it writes or deletes may
differ. `_transaction_abandon()` forgets the active transaction and keeps its footmarks; when
`maybe_transaction` calls it → [Failure semantics](#failure-semantics). Commit, discard, and revert
each list every key under the root, so a recorded write costs O(root size) under `history_lock` →
[ROADMAP.md](ROADMAP.md#ideas).

### Failure semantics

`maybe_transaction(storage, retention=None)` is the context manager callers wrap writes in.

| Situation | Result |
| -- | -- |
| Non-`FootmarkMixin` storage | Complete no-op; the body runs without a transaction. Capability test → [Transactions](#transactions). |
| Exception in the body | Staged footmarks are discarded and the exception propagates unchanged; a failing discard is logged and does not mask it, and the transaction is abandoned with its footmarks kept. |
| Failed commit | → `StorageException` with nothing recorded; the transaction is abandoned, not discarded, so the instance can begin again and the BACKUPs of originals already overwritten survive for an orphan sweep. |
| Successful body | Commit; a non-empty commit appends one history record → [Recording rules](#recording-rules), then prunes when a retention policy was passed → [Retention](#retention). |
| Backend unreachable mid-commit or mid-discard | Remote state may be partially updated; atomicity is best-effort → [ROADMAP.md](ROADMAP.md#known-deviations). |
| Malformed `.transactions.json` | Read before the transaction begins, so every write aborts → `StorageException` with no change staged. Move or delete the file out of band to resume; the `Storage` API refuses reserved paths → [VALIDATION.md](VALIDATION.md#filename-safety). |
| Recovered `.transactions.json` | Recorded transactions are lost and their objects become unrevertable; their footmarks are reclaimed by an orphan sweep → [Retention](#retention). |

## Transaction history

`TransactionHistory` lists, reverts, and prunes committed transactions. The array is rewritten in
full on every write and bounded by [Retention](#retention).

### Files

| Object | Purpose | S3 ACL |
| -- | -- | -- |
| `.transactions.json` | Compact JSON array of committed, non-empty transactions at the storage root; rewritten in full on every append, pop, or prune. | Reserved ACL → [S3 ACL rules](#s3-acl-rules). |
| `.transactions.lock` | Writer lock → [Locking](#locking). | As `.transactions.json`. |

Both names are reserved → [VALIDATION.md](VALIDATION.md#filename-safety), so an ordinary write
cannot replace the lock or the audit log. Only `TransactionHistory` writes `.transactions.json`
(through `_obj_put_reserved`); the lock object is written by the lock helpers alone, never through
the `Storage` ABI.

### Record

```json
{ "id": 7, "timestamp": "2026-06-08T10:15:30.123456789Z" }
```

| Field | Rule |
| -- | -- |
| `id` | Integer in `1..2^31-1` (`int32`, matching the wire schema). |
| `bool` id | Passed to `TransactionModel` or `transaction_begin` → `ValueError`; read by `load` → `StorageException`. |
| `timestamp` | Strict RFC 3339 string with up to nine fractional digits; control characters rejected; parsed by `Timestamp.from_json` → [Timestamps](#timestamps). |
| Malformed file or record | → `StorageException`: unparseable JSON, non-array root, non-object entry, bad id, or a timestamp that fails the grammar or is no real instant. |
| Tokens | Not stored; `load` parses each record once through `record_to_model()`, which regenerates them from `(id, timestamp)`, bit-identical to the model returned at begin. |

### Operations

Records are `TransactionModel`s; a list passed in is never modified.

| Method | Behavior |
| -- | -- |
| `load() -> list[TransactionModel]` | One read: `_obj_get_reserved` on a transactional storage, else `get`; missing file → empty list; parses every entry. |
| `next_id(records=None) -> int` | `max(id) + 1`; id space exhausted → `StorageException`. |
| `append(model, records=None)` | Writes the records plus `{id, timestamp}` through `save`; the optional list avoids a re-read. Lock → [Locking](#locking). |
| `record_to_model(record) -> TransactionModel` | Parses one stored record; malformed record → `StorageException`. |
| `revert_last() -> TransactionModel \| None` | Under the lock: reload, revert the latest transaction, save without it. Non-transactional backend → `StorageException`. |
| `pop_last() -> TransactionModel \| None` | Under the lock: drop the last record only, no storage revert. Non-transactional backend → `StorageException`. |
| `prune(retention, *, now=None, orphans=False, dry_run=False)` | Takes the lock, loads, drops expired records and their footmarks → [Retention](#retention); returns `PruneResult(records, footmark_paths, orphans)`. Lists hidden objects once for every step. Non-transactional backend → `StorageException`. |
| Auto-prune | `maybe_transaction` runs the same reclaim and rewrite → [Retention](#retention). |
| `save(records)` | Writes the full array of `{id, timestamp}` through `_obj_put_reserved`; a `TransactionModel` with an id `load` would reject cannot be built → `ValueError`. Non-transactional backend → `StorageException`. |

### Retention

`HistoryRetention(keep, max_age_days)` is the policy; `HistoryRetention.from_env()` builds it from
arguments or variables → [CONFIG.md](CONFIG.md#history-retention); values →
[VALIDATION.md](VALIDATION.md#history-retention). Age decides what expires; `keep` is a floor.

| Rule | Behavior |
| -- | -- |
| Expiry | A record is pruned iff it is older than `max_age_days` and its id is not among the `keep` highest ids. |
| `select_expired(records, now=None)` | `HistoryRetention` method; pure, no I/O. Returns the records the policy expires, in file order; disabled retention or `len(records) <= keep` → empty list. |
| Floor | `keep` is compared by id, not by position, so the record holding `max(id)` always survives and `next_id` stays monotonic. Ids are uniqued before the floor is taken, so a repeated id spends one slot. |
| Duplicate ids | `load` does not require unique ids. Expiry is a function of `(id, timestamp)` and the rewrite drops exactly the records selected, so a young record sharing an expired record's id survives. |
| Disabled | A `max_age_days` of `0` disables expiry: `select_expired` returns an empty list and nothing is listed, deleted, or rewritten. `prune()` still takes the lock and loads the history, and still sweeps when `orphans=True`; `maybe_transaction` skips the prune entirely. |
| Effect order | Footmarks of the expired records are deleted first, then the array is rewritten without them. The reverse order would strand footmarks whose tokens no surviving record can regenerate. |
| Interrupted prune | A crash between the two leaves a record whose footmarks are gone; the next prune expires it again, and reverting it is a no-op with a warning → [ROADMAP.md](ROADMAP.md#known-deviations). |
| Partial reclaim | A footmark delete that fails is logged and left for an orphan sweep, in the cleanup order → [Lifecycle](#lifecycle); the record is still dropped and the result lists only the paths removed. S3 reports per-key `Errors` from one batched call. |
| Not a revert | Committed objects are untouched; the transactions stay applied and stop being revertable. |
| Auto-prune | `maybe_transaction` prunes after a recorded commit when a policy is passed, without orphans, under the lock it already holds, reusing the commit's full `hidden_objects()` listing. |
| Auto-prune failure | Logged and swallowed: the commit is already durable. |
| Auto-prune no-op | Nothing is listed or rewritten when no record is expirable. |
| Orphan sweep | `prune(..., orphans=True)` also deletes footmark objects of no recorded transaction → `find_orphaned_footmarks` in [Lifecycle](#lifecycle). |
| Orphan origins | A writer that died between begin and commit, a discarded transaction whose cleanup failed, a commit that failed inside `maybe_transaction`, a raw `transaction_begin` / `transaction_commit`, which records nothing, or a history file moved away to recover from corruption. |
| Orphan scope | Only an explicit `prune(..., orphans=True)` sweeps. `maybe_transaction` never does: auto-prune already runs under `history_lock`, so this is a scope choice that keeps deletes of unrecorded objects explicit, not a locking limitation. |
| Nested roots | Roots do not nest: a sweep lists the whole subtree and `history_lock` excludes only its own root's writers, so a sweep of an outer root deletes an inner root's footmarks, committed and in flight. |
| Dry run | A prune decides everything before it deletes anything, and orphans are classified against every record, expiring ones included, so a dry run reports the same records, footmark paths, and orphan paths as a real run and changes nothing. |
| Dry-run lock | The lock is still taken, since orphan classification is only sound with writers excluded. |
| Malformed history | Every record is parsed on `load`, so a malformed one fails a real run where it fails the dry run: before anything is deleted. |

### Locking

`history_lock(storage)` holds the backend's `_obj_lock` and serializes the whole `maybe_transaction`
critical section (id allocation through history append), `revert_last` / `pop_last`, and a `prune`
run (listing through the last footmark delete). `append` and `save` take no lock; a caller outside
those holds `history_lock` around them, or a concurrent writer's record can be lost. The span
includes the body's transfers and sweeps, so a peer writer can exhaust the S3 acquire timeout →
[ROADMAP.md](ROADMAP.md#known-deviations). The S3 lock requires an endpoint that enforces
conditional writes: `IfNoneMatch` and `IfMatch` on `PutObject`, and `IfMatch` on `DeleteObject`.

| Backend | Mechanism | Bounds |
| -- | -- | -- |
| `fs` | Exclusive `flock` on `.transactions.lock` under the root; a lock file that cannot be created or locked → `StorageException`. | Blocks until acquired. |
| `s3` | Conditional `put_object` with `IfNoneMatch=*`, body `{owner, expires_at}`, reserved ACL → [S3 ACL rules](#s3-acl-rules). | TTL 12 h; acquire timeout 300 s → `StorageException`; retry every 0.25 s. |
| Non-transactional backends | No-op: they keep no history. | — |
| Transactional backend without `_obj_lock` or `_obj_lock_identity` | Cannot be constructed (abstract primitive), so never an unlocked section. | — |

| Re-entry | Behavior |
| -- | -- |
| Active transaction | `history_lock` on an instance with an active transaction → `StorageException`, before the lock is taken. |
| Holding thread | The lock is not re-entrant: a thread acquiring a root's lock it already holds, through any instance of the root (on `fs` also any spelling) → `StorageException`, instead of waiting on itself. |
| Lock identity | `fs` keys the root by the root directory's device and inode; `s3` by endpoint as written, bucket, and lock key, so another spelling of the endpoint waits out the acquire timeout. |
| Other threads and processes | Wait within the bounds above. |

| S3 lock step | Behavior |
| -- | -- |
| Refused write | A precondition failure (412) and a concurrent-write conflict (409) both mean "retry", until the acquire timeout. Any other error code, or a connection failure → `StorageException`. |
| Take-over | One `get_object` yields body and ETag together; a breakable body is overwritten by `put_object` with `IfMatch` on that ETag, so breaking and acquiring are one step and no delete is issued while breaking. |
| Vanished lock | An object deleted between the take-over read and write retries, as a refused write does. |
| Own body | Each acquire writes a fresh owner id; a body carrying it is held, not waited on, since a retried write whose first attempt committed is refused on replay. |
| Expiry | Only a successful read of a missing, corrupt, or past-TTL body is breakable. |
| Stamp cap | An `expires_at` more than two TTLs ahead of the breaker's clock is corrupt, so a far-future body blocks peers for at most that long. |
| Read refused | A 403 on the read aborts the acquire → `StorageException`, as for a refused write. |
| Read failure | Any other read failure keeps waiting, logged as a warning once per acquire; the acquire timeout names the last one and chains from it. |
| Clocks | The holder stamps `expires_at` from its UTC clock and the breaker judges it by its own → [ROADMAP.md](ROADMAP.md#known-deviations). |
| Write without ETag | → `StorageException`; the lock body is already written, so it is deleted best-effort before the failure surfaces rather than left to block peers. |
| Release | `delete_object` with `IfMatch` on the ETag the holder acquired; a changed version or an absent object is left alone. A failing delete is tried 3 times, 0.5 s apart, before the lock is left to its TTL. |

### Recording rules

- Only `maybe_transaction` records history; raw `transaction_begin` / `transaction_commit` leave
  footmarks but no record.
- An empty commit (no PUT, DELETE, or BACKUP footmark) writes nothing.
- `revert_last` is latest-only and removes the reverted record; the file lists the transactions
  still revertable by walking back, minus whatever [Retention](#retention) has expired.
- History append after commit, `save` after revert, and the forget-then-rewrite of a prune are not
  atomic with the footmark lifecycle → [ROADMAP.md](ROADMAP.md#known-deviations).

## S3 ACL rules

`S3Client.object_copy` copies with the canned ACL it is given, else with the source's group canned
ACL. `StorageS3` remembers the verbatim canned ACL written to each key in the active transaction
(its ACL sidecar), so `_obj_copy` hands it to the copy.

| Situation | ACL / behavior |
| -- | -- |
| `put`, `put_stream` with `acl=None` | `default_acl` from construction is applied; `None` sends no ACL. |
| `add_metadata` with `acl=None` | The object keeps its ACL, as on every metadata rewrite → [S3 metadata rewrites](#s3-metadata-rewrites); `default_acl` is not applied. |
| Bucket creation | `bucket_acl` from construction. |
| History and lock objects | `private` when a `default_acl` is set; no ACL under `NO_ACL`, and `bucket-owner-full-control` under that default, since a bucket that enforces bucket-owner ownership refuses every other ACL. |
| Copy in a transaction | The ACL the sidecar recorded for the source wins and is recorded for the destination; covers `_backup`, the staged metadata copy, commit of a PUT footmark, and `copy`. |
| Copy without a sidecar entry | Outside a transaction, or for a key not written in it, the copy carries the source's group grants as one canned ACL. |
| Group-grant mapping | The broadest canned ACL the grants make up, in any order: AllUsers WRITE → `public-read-write`, else AllUsers READ → `public-read`, else AuthenticatedUsers READ → `authenticated-read`. |
| Other grants | Not carried by a copy without a sidecar entry → [ROADMAP.md](ROADMAP.md#known-deviations). |
| Sidecar lifetime | Recorded only while a transaction is active; cleared when the next transaction begins. |
| Sidecar overwrite | A later content write of the same key without a canned ACL drops its entry, so commit copies it with the bucket default, as outside a transaction; a metadata rewrite with `acl` records it, and one without keeps the entry. |
| Public probe | After `put` / `put_stream` only (inside a transaction, on the staged footmark), when the effective ACL is `public-read` or `public-read-write`; a non-public grant → `StorageException`. The `StorageS3` argument `verify_public_acl=False` turns it off; `make_storage` keeps it on. |
| Probe retries | The probe tries 3 times, sleeping 0.5 s then 1 s; exhaustion → `StorageException`. |
| Unprobed writes | `add_metadata`, `copy`, `move`, and commit copies are not probed; `verify_public(path)` runs the probe ad hoc. |
| `fs` | No canned ACLs; how `acl` is applied → [VALIDATION.md](VALIDATION.md#filename-safety). |

## S3 metadata rewrites

S3 metadata is immutable, so `add_metadata`, `delete_metadata`, and `delete_metadata_fields` copy
the object onto itself with `MetadataDirective="REPLACE"` in `txstore/_s3_client.py`.

| Rule | Behavior |
| -- | -- |
| Merge | `object_put_metadata` merges the new entries over the existing user metadata. |
| Replace | `object_replace_metadata` writes exactly the given entries in one rewrite. |
| Removal | `object_delete_metadata` drops the named keys, ignoring absent ones; `None` drops every key and an empty list none. |
| System headers | `ContentType`, `ContentEncoding`, `ContentDisposition`, `ContentLanguage`, and `CacheControl` are carried over; `REPLACE` would otherwise reset them. |
| ACL | An explicit `acl` wins; otherwise the object's group canned ACL is carried over as on a copy. |
| Over the size limit | When the merge in `add_metadata` exceeds the S3 user-metadata limit (`MetadataTooLarge`), the existing keys are dropped and only the new entries are written in one rewrite, with a warning. |
| Rewrite failure | Any other error, including in that fallback rewrite, → `StorageException` with the metadata unchanged. |
| Large objects | At or above 5 GiB, the single-request copy limit, the rewrite is a multipart copy in 256 MiB ranges; any failure aborts the upload → `StorageException`. |
| Client | Connect and read timeout 60 s, pool of 10, 3 total attempts; uploads switch to a 10-thread multipart upload of 50 MiB parts above 100 MiB. |

## Digests

`txstore/hexdigest.py` owns the digest helpers and the algorithm lists.

| Item | Rule |
| -- | -- |
| Digest dict | Keys `MD5Sum`, `SHA1`, `SHA256`, `SHA512` (hex strings) and `Size` (decimal string), as `ChecksumOpen.get_digests`, `calc_digests`, and `transfer` return them. |
| Key spellings | Debian `Release` field names, so digest dicts interoperate with apt index tooling; `Size` is a decimal string for the same reason. Renaming is an API break. |
| `MD5Sum` | Kept because Debian indices still carry it; never decides a match on its own. |
| `SUPPORT_HASH_LIST`, `COMPARE_HASH_LIST` | The algorithm keys in order, and the same list without `MD5Sum`; `compare_digests` iterates the first and counts a match only on the second, `normalize_digests` keeps only the second. |
| `ChecksumOpen` | Wraps a stream and computes every digest and the size while it is read; reused by `transfer`. |
| `compare_digests(expected, incoming)` | `True` only when at least one algorithm other than `MD5Sum` is shared and every shared value, `MD5Sum` included, matches: MD5 can refuse a match but never make one. |
| Non-overlapping, unknown, or mismatched digests | `compare_digests` returns `False`. |
| `normalize_digests` | Canonicalizes algorithm names before comparison and drops unsupported ones and `MD5Sum`. |

## Maintenance

Add a backend by inheriting `Storage` (plus `FootmarkMixin` for transactions, or the capability
mixins for partial support), implementing the [backend primitives](#backend-primitives), and
registering it in `txstore/factory.py` and in the scheme table of `txstore/url.py`; export new
public names → [STRUCTURE.md](STRUCTURE.md#maintenance); update [Backends](#backends),
[VALIDATION.md](VALIDATION.md#storage-urls), and [TESTING.md](TESTING.md#file-layout) in the same
change. A change to the wire format or the timestamp text form breaks stored footmarks and is
refused. Keep this file and the module docstrings of `txstore/transaction.py`, `txstore/history.py`,
`txstore/_s3_acl.py`, and `txstore/_footmark_wire.py` in agreement.
