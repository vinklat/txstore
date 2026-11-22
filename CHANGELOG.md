# Changelog

Released changes and their upgrade notes. Planned work → [ROADMAP.md](ROADMAP.md); entry format and
release steps → [SETUP.md](SETUP.md#release).

## Unreleased

## 0.1.3 - 2026-11-21

Bounded reads for untrusted sources: a size cap on `get`, `transfer`, and `get_digests`. Release →
[v0.1.3](https://github.com/vinklat/txstore/releases/tag/v0.1.3).

| Change | Migration |
| -- | -- |
| Exports | `StorageNotSupportedException` and `StorageTooLargeException` are exported from `txstore`. |
| Size cap | `get`, `transfer`, and the new `get_digests` take `max_size` → [STORAGE.md](STORAGE.md#storage-abi). |
| Unsupported operation | Breaking: an operation the backend lacks → `StorageNotSupportedException` instead of a bare `NotImplementedError` → [EXCEPTIONS.md](EXCEPTIONS.md#storage-exceptions). |
| `get_stream` | Breaking: returns a wrapper without backend attributes such as `iter_chunks` or `raw`; iterating it yields lines, not S3's 1 KiB chunks → [STORAGE.md](STORAGE.md#storage-abi). |
| `tell` and `fileno` | Breaking: `tell` on `s3` and `http`, and `fileno` on `http` → `io.UnsupportedOperation` → [STORAGE.md](STORAGE.md#storage-abi). |
| Stream reads | Breaking: body read failures and HTTP `get` streaming change → [STORAGE.md](STORAGE.md#storage-abi). |
| urllib3 | Requires urllib3 2.6 or later → [SETUP.md](SETUP.md#dependency-policy). |

## 0.1.2 - 2026-11-08

`redact_url` and stricter storage URLs. Release →
[v0.1.2](https://github.com/vinklat/txstore/releases/tag/v0.1.2).

| Change | Migration |
| -- | -- |
| Exports | `redact_url` is exported from `txstore`. |
| Storage URLs | A URL holding a control character, or `@` with no `//` authority, is refused → [VALIDATION.md](VALIDATION.md#storage-urls). |

## 0.1.1 - 2026-11-07

Renamed environment variable prefix. Release →
[v0.1.1](https://github.com/vinklat/txstore/releases/tag/v0.1.1).

| Change | Migration |
| -- | -- |
| Variables | `TX_STORE_*` variables are renamed to `TXSTORE_*`; the old names are no longer read → [CONFIG.md](CONFIG.md#environment-variables). |

## 0.1.0 - 2026-11-07

Initial public release: fs, S3, HTTP, and null backends; transactions, history, and retention;
configuration by arguments or `TX_STORE_*` variables; Debian testing floors. Release →
[v0.1.0](https://github.com/vinklat/txstore/releases/tag/v0.1.0).
