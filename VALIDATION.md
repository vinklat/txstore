# Validation contract

Owns: input constraints at the storage-URL, storage-path, configuration, and retention boundaries.
Routing → [AGENTS.md](AGENTS.md#canonical-sources).

## Principles

- Validate at the boundary that first receives the value: URL parser, path helper, `make_storage`,
  or a model constructor.
- Reject unknown schemes before any storage mutation; each backend refuses the paths its
  [filename-safety](#filename-safety) rows assign it, and `is_unsafe_path` is the caller-side key
  check.
- Failures are written `condition → ExceptionClass` → [EXCEPTIONS.md](EXCEPTIONS.md#rules).

## Storage URLs

Implemented by `parse_storage_url()` in `txstore/url.py`; result is
`(storage_type, scheme, netloc, path)`. `scheme` is the transport scheme `make_storage` dials; it is
empty for a backend with no transport.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Supported schemes | Exactly `fs`, `s3`, `http`, `https` from `get_supported_schemes()`. | Other scheme → `ValueError` |
| Authority | urllib parses the URL, and a port, when present, is non-empty, numeric, and in range. | Unparseable URL (a non-ASCII character NFKC folds into a delimiter, a malformed bracketed host) or a bad port → `ValueError`, without echoing the URL |
| Credential `/` | An unencoded `/` in `http(s)` credentials ends the authority before its `@`. A token-only userinfo, or a password whose characters before the `/` are digits, then parses as a host, port, and path without error; `%2F` keeps the userinfo whole. | Any other remainder: an empty, non-numeric, or out-of-range port → `ValueError`, without echoing the URL |
| Credential `?` or `#` | An unencoded `?` or `#` in credentials ends the authority before its `@`; the URL never routes, whatever the remainder. `%3F` and `%23` keep the userinfo whole. | `?` or `#` in credentials → `ValueError`, without echoing the URL |
| `s3://` credentials | S3 keys come from `s3_access` / `s3_secret`, so an `s3://` URL holds no `@`, wherever a split password left it. | `@` in an `s3://` URL → `ValueError`, without echoing the URL |
| `fs://` authority | The authority is empty: `fs:///<absolute path>`. | Non-empty `fs://` authority → `ValueError`, without echoing the URL |
| `fs://` path | The path starts with `/`, so a truncated URL never roots a store in the working directory. | Empty or relative `fs` path (`fs:`, `fs://`, `fs:a`) → `ValueError`, without echoing the URL |
| Query and fragment | Routing keeps only netloc and path, so the URL holds no `?` or `#`, even an empty one. | `?` or `#` in the URL → `ValueError`, without echoing the URL |
| Direct backend URLs | A `StorageHTTP` base URL or `StorageS3` endpoint holds no `?` or `#` either: a query would never be sent, and botocore echoes the endpoint in its errors. | `?` or `#` → `ValueError` from the constructor, without echoing the URL |
| `fs://` | `StorageType.fs`; no transport, so the scheme is empty. | — |
| `s3://` | `StorageType.s3`; transport scheme normalized to `https`. | — |
| `http://`, `https://` | `StorageType.http`; scheme preserved. | — |
| `null` | Not URL-routable; absent from `get_supported_schemes()`. Construction → [STORAGE.md](STORAGE.md#construction). | — |

## HTTP URL join safety

Implemented by `join_url()` in `txstore/url.py`. Prevents SSRF when a path comes from an untrusted
source, such as a key supplied by a caller or read back from storage.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Base URL | Has both scheme and netloc, and its authority parses → [Storage URLs](#storage-urls). | → `ValueError("Cannot join URLs: invalid base URL: ...")`; an authority that does not parse → `ValueError`, without echoing the URL |
| Resolved URL | urllib parses the joined URL. | → `ValueError("Rejected unparseable URL join ...")`, naming only the redacted base |
| Scheme | Resolved scheme equals the base scheme, case-insensitive. | → `ValueError("Rejected cross-scheme URL join: ...")` |
| Origin | Resolved netloc equals the base netloc, case-insensitive. | → `ValueError("Rejected cross-origin URL join: ...")` |
| Prefix | Resolved path, percent-decoded once and with its dot segments resolved, starts with the base path, so no reference can drop the prefix on the base origin; a backslash counts as a separator. | → `ValueError("Rejected out-of-prefix URL join: ...")` |
| Rooted or encoded reference | `urljoin` reads a rooted or scheme-carrying reference as replacing the prefix, and leaves `%2e%2e` encoded for an origin to decode and resolve itself; the prefix rule catches both. | → `ValueError("Rejected out-of-prefix URL join: ...")` |
| Double encoding | Resolved path holds no escape after one percent-decode pass. | → `ValueError("Rejected doubly encoded URL join: ...")` |
| Control characters | Resolved path holds no C0 control or DEL, raw or percent-decoded once. | → `ValueError("Rejected control character URL join: ...")` |
| Query and fragment | The key holds no raw `?` or `#`, so it names the whole path it fetches; `%3F` and `%23` name the characters literally. Checked on the key before it is resolved. | → `ValueError("Rejected query or fragment URL join ...")`, naming only the redacted base |
| Path parameters | The prefix rule drops each segment's `;params` before resolving dot segments, as a servlet-style origin does. | → `ValueError("Rejected out-of-prefix URL join: ...")` |
| Redirects | `StorageHTTP` never follows a redirect: the pins above hold for the first request only. | 3xx response → `StorageException` |
| `StorageHTTP` join | A join rejected by any rule above is re-raised by the backend, never as the raw `ValueError`. | Rejected join in `StorageHTTP` → `StorageException` |

## S3 URL path split

Implemented by `parse_s3_url_path()` in `txstore/url.py`; its prefix rules live in
`normalize_s3_prefix()`, which the `StorageS3` `prefix` argument also goes through.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Bucket root | `/<bucket>` and `/<bucket>/` → `(<bucket>, None)`. | — |
| Prefix | `/<bucket>/<prefix...>` → `(<bucket>, <prefix...>)`, taken verbatim, never percent-decoded. | — |
| Prefix holding `@` | An `s3://` URL holds no `@` → [Storage URLs](#storage-urls), and `%40` stays literal, so a prefix holding `@` is passed to `StorageS3(prefix=...)` directly. | — |
| Prefix trailing slash | Dropped, so `/<bucket>/dir/` → `(<bucket>, "dir")`; an empty prefix → `None`. | — |
| Empty prefix segment | After the trailing slashes are dropped, the prefix starts with `/` or holds `//` (`/<bucket>//x`, `/<bucket>/a//b`). | → `ValueError("S3 prefix has an empty segment: ...")` |
| Empty bucket | The path is empty or its first segment is (`""`, `/`, `//<prefix>`). | → `ValueError("S3 bucket name is not specified in url.")` |

## Filename safety

Object keys may be supplied by a caller or read back from storage, so a key names where a write
lands and which URL a read fetches. The checks below constrain both; the unsafe-key rows are a
caller-side check, the `StorageFS` rows are enforced by the backend, and the reserved-path and
object-name rows by every transactional backend. `StorageNull` checks no path: its writes are no-ops
→ [StorageNull operations](STORAGE.md#storagenull-operations).

| Rule | Constraint | Failure |
| -- | -- | -- |
| Unsafe key | Non-empty and relative. | `is_unsafe_path` returns `True` |
| Unsafe key check | The unsafe-key rows are one check, `is_unsafe_path()` in `txstore/url.py`, for callers to apply to untrusted keys. | — |
| Unsafe key backends | No backend applies it: `StorageHTTP` relies on `join_url` → [HTTP URL join safety](#http-url-join-safety), `StorageFS` on its paths row. | — |
| Unsafe key traversal | No `..` segment split on `/` or `\`, before and after one percent-decode pass; a `..;params` segment counts as `..`. | `is_unsafe_path` returns `True` |
| Unsafe key characters | No C0 control or DEL, before or after one percent-decode pass, so no `..%00` padding an origin strips reaches `join_url`. | `is_unsafe_path` returns `True` |
| Unsafe key encoding | A key still holding an escape after one percent-decode pass is doubly encoded and rejected. | `is_unsafe_path` returns `True` |
| Unsafe key URL form | The first segment holds no `:` and no later segment is empty, so no `scheme:` or protocol-relative form reaches `join_url`; no `?` or `#`, before or after one percent-decode pass, so the key is the whole URL path it fetches. | `is_unsafe_path` returns `True` |
| `StorageFS` paths | Relative and resolving inside the storage root. | → `StorageException` |
| `StorageFS` path encoding | No NUL byte and no character the filesystem encoding cannot take, checked before any other path rule on every verb. | → `StorageException` |
| Reserved root paths | Normalized, case-folded path, with leading `/` dropped, is not `.transactions.json` / `.transactions.lock`; enforced on every mutator: `put`, `put_stream`, `delete`, `copy` (destination), `move` (source and destination, before either writes), `add_metadata`, `delete_metadata`, `delete_metadata_fields` (non-empty `keys`), and `StorageFS` `mkdir` / `rmdir`. | → `StorageException` |
| `StorageFS` resolved paths | The reserved-path rule also applies to the path after symlink resolution, and no mutator targets the root itself (`""`, `.`, `pool/..`) or a path that resolves outside it. | → `StorageException` |
| Footmark originals | Commit and revert name the original of a listed footmark by the listed name, never through a symlink. | — |
| Footmark sources | On `fs` a PUT footmark commit copies, and a BACKUP footmark revert restores, is a regular file inside the root, checked before the first write; commit also refuses a BACKUP footmark that is already a symlink or non-regular file, as its revert would. | Symlink or non-regular footmark → `StorageException`; nothing is applied; one gone since the listing → [STORAGE.md](STORAGE.md#lifecycle) |
| Object name | On `s3` and every other transactional backend that does not resolve paths, no mutator targets a path whose last segment is empty, `.`, or `..` (`""`, `pool/`, `pool/..`): it names a prefix, not an object, and its footmark would have no original. | → `StorageException` |
| Footmark original paths | The original passes the reserved-path and object-name rows above, and on `fs` the `StorageFS` resolved-paths row, so commit and revert never act outside the root. | → `StorageException` |
| `StorageFS` directory destination | `copy` and `move` never write into a directory at the destination. | → `StorageException` |
| `StorageFS` directory source | `move` never takes a directory as its source, in or out of a transaction. | → `StorageException` |
| `StorageFS` read | `get` and `get_stream` open the resolved path without following a symlink, so a link swapped in after resolution is never read through; a link already in place resolves to its in-root target, which is read. | Link swapped in after resolution → `StorageException` |
| `StorageFS` non-regular files | `get`, `get_stream`, a copy source, a DELETE marker, `.transactions.json`, and `.transactions.lock` open without blocking and are refused unless the opened descriptor names a regular file, so a FIFO or device there, even one swapped in after a path check, fails the call instead of hanging it. | FIFO or device → `StorageException` |
| `StorageFS` device opens | The refusal follows the open, so a device at one of those paths is opened once, non-blocking and never as a controlling terminal, and its driver sees that open and close; mount the root `nodev` to rule it out. | — |
| `StorageFS` copy | A copy, backup, commit, or restore reads its source without following a symlink and lands through an exclusive temp file renamed over the destination, so a link at either end is never written through. | Source that is a link when opened (a user path is resolved first) → `StorageException` |
| `StorageFS` file mode | An `acl` of three or four octal digits sets the mode of the written file, masked to the permission bits (`0o777`); any other `acl` is ignored and the umask applies. `add_metadata` ignores `acl`. | — |
| `StorageFS` copied attributes | A copy, backup, commit, or restore carries the source's bytes, its mode masked to the permission bits (`0o777`), and only its `user.` xattrs, so a planted footmark lends no setuid, setgid, or sticky bit, ACL, or label. | — |
| `StorageFS` native move | A `move` outside a transaction on one device is an `os.replace` of the live object, which keeps every attribute it already had; across devices or in a transaction it is a copy, bound by the row above. | — |
| `StorageFS` other modes | Directories, `.transactions.json`, and `.transactions.lock` take the process umask; a root other users must not read or lock needs umask `077` (or `027` for a group). | — |
| `StorageFS` temp file | A write goes through a dotfile temp file with an unpredictable name, `.<name>.tmp.<16 hex>`, created exclusively and never through a symlink. | Planted file or link → `StorageException` |
| `StorageFS` metadata | Xattrs are set and removed without following a symlink. | — |
| `StorageFS` lock file | `.transactions.lock` is never opened through a symlink. | Planted link → `StorageException` |
| `StorageFS` history file | `.transactions.json` is never read through a symlink. | Planted link → `StorageException` |
| `StorageFS` footmark reclaim or discard | Listed path resolves inside the root and names a regular file; the unlink does not follow a symlink; one gone since the listing → [STORAGE.md](STORAGE.md#lifecycle). | Discard → `StorageException`; reclamation logs the refusal → [STORAGE.md](STORAGE.md#retention) |

## History retention

Implemented by `HistoryRetention.__post_init__` in `txstore/history.py`; variable parsing →
[Environment values](#environment-values).

| Rule | Constraint | Failure |
| -- | -- | -- |
| `keep` | Integer, 1 or greater; `bool` is not an integer here. | → `ValueError` |
| `max_age_days` | Integer, 0 to 36500, in days; meaning of `0` → [STORAGE.md](STORAGE.md#retention). | → `ValueError` |

## S3 canned ACLs

Implemented by `parse_bucket_acl()` and `parse_object_acl()` in `txstore/storage_s3.py`; the allowed
values are the `BucketCannedACL` and `ObjectCannedACL` literals in `txstore/_s3_client.py`.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Object ACL | `authenticated-read`, `aws-exec-read`, `bucket-owner-full-control`, `bucket-owner-read`, `private`, `public-read`, `public-read-write`. | → `ValueError("invalid S3 object ACL: ...")` |
| Bucket ACL | `authenticated-read`, `private`, `public-read`, `public-read-write`. | → `ValueError("invalid S3 bucket ACL: ...")` |
| `make_storage` default ACL | An object ACL or `NO_ACL` (`"none"`), from the argument or variable. | Other → `ValueError` |
| `StorageS3` per-call ACL | A non-`None` `acl` of `put`, `put_stream`, or `add_metadata` is an object ACL, checked before any request. | Other → `ValueError` |
| `make_storage` bucket ACL | A bucket ACL from the argument or variable; default → [CONFIG.md](CONFIG.md#storage-construction). | Other → `ValueError` |

## Environment values

Implemented by `txstore/_env.py`; names → [CONFIG.md](CONFIG.md#environment-variables); defaults →
[CONFIG.md](CONFIG.md#storage-construction) and [CONFIG.md](CONFIG.md#history-retention).

| Rule | Constraint | Failure |
| -- | -- | -- |
| Unset or empty | Treated alike: the setting falls back to its default. | — |
| Boolean | `1/0`, `true/false`, `yes/no`, `on/off`, any case, surrounding space ignored. | → `ValueError` naming the variable |
| Integer | Decimal, optional sign, surrounding space ignored. | → `ValueError` naming the variable |
| Name | Only `TX_STORE_*` names are read; an unprefixed name such as `S3_ACCESS` is never a fallback. | — |

## Maintenance

A new rule adds a row here, its implementation, and a success and a failure test in the same change
→ [TESTING.md](TESTING.md#harness-and-gates). A new URL scheme updates the scheme table in
`txstore/url.py` and [Storage URLs](#storage-urls) together.
