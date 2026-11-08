# txstore

Unified object storage for Python: one `Storage` interface over a local filesystem, S3-compatible
buckets, and HTTP servers, selected by URL. Read, write, list, and stream objects between backends
with the same calls, and on the filesystem and S3, optionally group writes into a transaction you
commit, discard, or revert as one unit.

Current version: **0.1.2**. Planned work → [ROADMAP.md](ROADMAP.md).

## Capabilities

- One `Storage` interface over the filesystem (`fs://`), S3-compatible object stores (`s3://`),
  read-only HTTP (`http://`, `https://`), and a write-only digest sink; `make_storage(url)` picks
  the backend, so code switches storage by changing a URL.
- Get, put, list, copy, move, and metadata calls that behave the same on every backend that supports
  them.
- Transactions on filesystem and S3: writes are staged as hidden footmark objects and committed,
  discarded, or reverted together.
- A transaction history at the storage root with age and count retention, revert of the latest
  transaction, and cleanup of orphaned footmarks.
- A cross-process writer lock: `flock` on the filesystem, conditional writes on S3.
- Streamed transfers between storages that return MD5, SHA-1, SHA-256, and SHA-512 digests in one
  pass.
- S3 canned-ACL preservation across transactional copies and metadata rewrites.
- Configuration by keyword arguments or `TXSTORE_*` environment variables; no CLI coupling.

## Quick start

### Install

Released versions are published to [PyPI](https://pypi.org/project/txstore/):

```bash
pip install txstore
```

Settings → [CONFIG.md](CONFIG.md); behavior → [STORAGE.md](STORAGE.md).

### Stream between backends

Every backend is a `Storage` built from a URL, so moving data between technologies is one call.
Download a web page and save it to disk, streamed in chunks rather than loaded into memory:

```python
from txstore import make_storage

web = make_storage("https://docs.python.org/3")
disk = make_storage("fs:///srv/data", create_path=True)

digests = web.transfer("library/io.html", disk, "pages/io.html")
print(digests)
# {'MD5Sum': '...', 'SHA1': '...', 'SHA256': '...', 'SHA512': '...', 'Size': '180750'}
```

Swap either URL for `s3://...` and the same call uploads to or downloads from a bucket. HTTP storage
is read-only and does not follow redirects; point the base URL at the final location.

### Group writes into a transaction

Writes to the filesystem or S3 can be grouped into a transaction:

```python
from txstore import HistoryRetention, TransactionHistory, make_storage, maybe_transaction

storage = make_storage("fs:///srv/data", create_path=True)
retention = HistoryRetention.from_env()

# Write a data file and the manifest that points to it: both are applied together on commit.
with maybe_transaction(storage, retention):
    storage.put("reports/october.csv", b"day,orders\n1,42\n2,57\n")
    storage.put("manifest.json", b'{"latest": "reports/october.csv"}')

# A failure inside the block discards every staged write.
try:
    with maybe_transaction(storage, retention):
        storage.put("reports/november.csv", b"day,orders\n1,???\n")
        raise ValueError("validation failed")
except ValueError:
    assert not storage.exists("reports/november.csv")

# Changed your mind later? Revert the last transaction as one unit.
history = TransactionHistory(storage)
print(
    [(t.id, t.timestamp.to_json()) for t in history.load()]
)  # [(1, '2026-10-23T19:08:36.024119Z')]
history.revert_last()
assert not storage.exists("manifest.json")
```

### Connect to S3

On S3, pass the endpoint and bucket in the URL and the credentials as arguments or variables:

```bash
export TXSTORE_S3_ACCESS=... TXSTORE_S3_SECRET=...
```

```python
storage = make_storage("s3://s3.example.com/bucket/prefix", s3_default_acl="private")
```

## Documentation

| Location | Audience | Authority |
| -- | -- | -- |
| `/` root contract documents | Contributors and AI agents | Canonical |
| [docs/guides/](docs/guides/README.md) | Library users | Non-canonical guides and examples |
| [docs/development/](docs/development/README.md) | Contributors | Non-canonical code maps, flow diagrams, explanations |
| [docs/reference/](docs/reference/README.md) | Contributors and tooling | Non-canonical reference artifacts |

## Canonical ownership

Domain owners and agent navigation → [AGENTS.md](AGENTS.md#canonical-sources).

## Technology

Python 3.13+, boto3, requests, pyxattr; pytest, moto, Ruff, mypy, and uv for development.

## License

MIT → [LICENSE](LICENSE).
