# Exception contract

Owns: exception classes, the conditions that raise them, and raising and chaining rules. Routing →
[AGENTS.md](AGENTS.md#canonical-sources).

## Rules

- Contracts write a failure as `condition → ExceptionClass`.
- Backend and storage-format failures raise `StorageException` or its subclass; a caller handling
  `StorageException` handles every storage failure.
- Wrap a library error (`ClientError`, `BotoCoreError`, `OSError`, `requests` errors) in
  `StorageException` with `raise ... from exc`; never let it escape a public `Storage` method.
  Errors from a `put_stream` source stream → [STORAGE.md](STORAGE.md#storage-abi).
- Invalid arguments and configuration raise `ValueError`, except a URL that parses but cannot name a
  storage (empty, HTTP or S3 without host, `s3://` without resolved keys): `make_storage` or the
  backend constructor raises `StorageException` → [STORAGE.md](STORAGE.md#construction); a malformed
  `s3://` path stays `ValueError` → [Other errors](#other-errors).
- Unsupported backend operations → [Other errors](#other-errors).
- Messages state the condition and the object; secrets never appear →
  [CONFIG.md](CONFIG.md#secrets).

## Storage exceptions

| Exception | Module | Raised when |
| -- | -- | -- |
| `StorageException` | `txstore/base.py` | A backend operation, transaction, history, or lock step fails → [STORAGE.md](STORAGE.md#failure-semantics). |
| `StorageNotFoundException` | `txstore/base.py` | The named object is missing → [STORAGE.md](STORAGE.md#storage-abi); subclass of `StorageException`. |

## Other errors

| Error | Raised when |
| -- | -- |
| `ValueError` | Unsupported URL scheme, an authority that does not parse, an `s3://` URL holding `@`, an `fs://` URL with an authority or a path that is not absolute, or a storage URL holding `?` or `#` → [VALIDATION.md](VALIDATION.md#storage-urls). |
| `ValueError` | Empty S3 bucket, or an S3 prefix with an empty segment → [VALIDATION.md](VALIDATION.md#s3-url-path-split). |
| `ValueError` | Unsafe HTTP join passed to `join_url`; `StorageHTTP` raises `StorageException` instead → [VALIDATION.md](VALIDATION.md#http-url-join-safety). |
| `ValueError` | Invalid canned ACL → [VALIDATION.md](VALIDATION.md#s3-canned-acls). |
| `ValueError` | Invalid environment value → [VALIDATION.md](VALIDATION.md#environment-values). |
| `ValueError` | Out-of-range retention → [VALIDATION.md](VALIDATION.md#history-retention). |
| `ValueError` | Out-of-range timestamp passed to `Timestamp`; one in a history record raises `StorageException` instead → [STORAGE.md](STORAGE.md#timestamps). |
| `ValueError` | Out-of-range transaction id → [STORAGE.md](STORAGE.md#lifecycle). |
| `NotImplementedError` | A read-only, write-only, or non-transactional backend is asked for an operation it lacks → [STORAGE.md](STORAGE.md#backends); not a `StorageException` → [ROADMAP.md](ROADMAP.md#known-deviations). |

## Maintenance

A new exception class adds a row to [Storage exceptions](#storage-exceptions) in the same change;
`scripts/check_docs.py` fails when the table and the `class *Exception` definitions differ.
