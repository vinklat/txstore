# txstore

Transactional object storage for Python: stage writes on a local filesystem or an S3-compatible
bucket, then commit, discard, or revert them as one transaction.

## Planned capabilities

- One `Storage` interface over the filesystem (`fs://`), S3-compatible object stores (`s3://`),
  read-only HTTP (`http://`, `https://`), and a write-only digest sink.
- Transactions on filesystem and S3: writes are staged as hidden footmark objects and committed,
  discarded, or reverted together.
- A transaction history at the storage root with age and count retention, revert of the latest
  transaction, and cleanup of orphaned footmarks.
- A cross-process writer lock: `flock` on the filesystem, conditional writes on S3.
- Streamed transfers between storages that return MD5, SHA-1, SHA-256, and SHA-512 digests in one
  pass.
- S3 canned-ACL preservation across transactional copies and metadata rewrites.
- Configuration by keyword arguments or `TX_STORE_*` environment variables; no CLI coupling.

## Technology

Python 3.13+, boto3, pydantic, requests, pyxattr; pytest, moto, Ruff, mypy, and uv for development.

## License

MIT → [LICENSE](LICENSE).
