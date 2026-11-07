# Backends

The four storage backends, how a URL picks one, and how to configure them with arguments or
`TX_STORE_*` environment variables.

## What each backend does

| URL | Backend | Reads | Writes | Transactions |
| -- | -- | -- | -- | -- |
| `fs:///path` | `StorageFS` | yes | yes | yes |
| `s3://endpoint/bucket[/prefix]` | `StorageS3` | yes | yes | yes |
| `http://...`, `https://...` | `StorageHTTP` | yes | no | no |
| none | `StorageNull` | no | yes (discarded) | no |

`StorageNull` swallows writes; pair it with `transfer` to compute digests without keeping a copy.
Construct it directly: `StorageNull()`.

## Building a storage

`make_storage(url, ...)` picks the backend from the URL scheme. An omitted or `None` setting falls
back to its `TX_STORE_*` variable, where one exists, then to its default:

```python
from txstore import make_storage

fs = make_storage("fs:///srv/data", create_path=True)
s3 = make_storage(
    "s3://s3.example.com/releases/stable",  # keys from TX_STORE_S3_ACCESS / TX_STORE_S3_SECRET
    s3_default_acl="private",
)
web = make_storage("https://downloads.example.com/releases", verify_ssl=True)
```

Arguments, `TX_STORE_*` variables, and defaults → [CONFIG.md](../../CONFIG.md#storage-construction).

Percent-encode `/`, `?`, `#`, `@`, and non-ASCII characters in credentials in an `http(s)` URL: an
unencoded `/` can parse part of a password as the host and path instead of raising →
[CONFIG.md](../../CONFIG.md#secrets).

## S3 specifics

- The URL names the endpoint host, then the bucket, then an optional key prefix. Every key you pass
  is placed under the prefix. A trailing slash on the prefix is ignored, so `.../releases/stable/`
  and `.../releases/stable` reach the same keys, and an empty segment (`//`) is refused →
  [VALIDATION.md](../../VALIDATION.md#s3-url-path-split).
- `put`, `put_stream`, and `transfer` apply `s3_default_acl` unless the call passes `acl=`;
  `add_metadata` keeps the object's ACL unless it passes one. Inside a transaction, `copy` and
  `move` reuse the ACL the source was written with in that transaction; otherwise they carry the
  source's group ACL → [STORAGE.md](../../STORAGE.md#s3-acl-rules). By default no ACL is sent, so
  the bucket's policy and ownership settings decide access; a public mirror passes
  `s3_default_acl="public-read"`. A bucket created with `create_path=True` gets no ACL unless
  `s3_bucket_acl` names one; a canned bucket ACL grants more than the same object ACL →
  [STORAGE.md](../../STORAGE.md#construction).
- A `put`, `put_stream`, or `transfer` whose effective ACL is public (`public-read` or
  `public-read-write`, from `acl=` or the default) re-reads the object's ACL and fails if it is not
  public; `add_metadata`, `copy`, and `move` are not checked →
  [STORAGE.md](../../STORAGE.md#s3-acl-rules).
- `mkdir` and `rmdir` do nothing; `format()` deletes and recreates the bucket, and is refused when
  the URL has a prefix, since the bucket holds keys outside it.
- The writer lock needs an endpoint that enforces conditional writes (`If-None-Match` and `If-Match`
  on writes, `If-Match` on deletes) → [STORAGE.md](../../STORAGE.md#locking).

## Filesystem specifics

- Paths are confined to the root; `..` and symlinks out of the root are refused.
- Metadata is stored in extended attributes, key `k` as `user.k`, so the keys you pass are the same
  as on S3. On a filesystem without xattr support, `get_metadata` returns `{}` and metadata writes
  and removals are skipped with a warning, not raised → [STORAGE.md](../../STORAGE.md#storage-abi).
- `format()` is refused: remove the directory yourself.

## Errors you will see

| Error | When |
| -- | -- |
| `StorageException` | Empty URL, HTTP or S3 URL without a host, missing S3 credentials, or any backend failure. |
| `ValueError` | Unsupported scheme, a URL authority that does not parse, a URL query or fragment, an `s3://` URL holding `@`, an `fs://` URL naming a host or a relative path, a missing bucket name or an empty S3 prefix segment (`//`), an invalid ACL, or an invalid environment value → [EXCEPTIONS.md](../../EXCEPTIONS.md#other-errors). |
| `NotImplementedError` | `list`, a write, a metadata change, `copy`, `move`, `format()`, or a directory call on HTTP; a read, `list`, `copy`, `move`, or `mkdir` on the null sink; a transaction call on either, except `in_transaction()`, which returns `False` → [STORAGE.md](../../STORAGE.md#backends). |

## FAQ

**Can I construct a backend class directly?** Yes; backend constructors take explicit arguments and
never read environment variables. `make_storage` is the one place variables apply.

**Why does an HTTP read refuse some paths?** Keys are joined onto the base URL with SSRF guards: a
path that would leave the base origin or prefix is rejected, and a redirect is refused rather than
followed → [VALIDATION.md](../../VALIDATION.md#http-url-join-safety).

## Further reading

- [STORAGE.md](../../STORAGE.md#backends) and [STORAGE.md](../../STORAGE.md#construction).
- [CONFIG.md](../../CONFIG.md) and [VALIDATION.md](../../VALIDATION.md#storage-urls).
