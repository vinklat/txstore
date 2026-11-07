# Configuration contract

Owns: `make_storage` and `HistoryRetention.from_env` settings, their `TX_STORE_*` environment
variables, resolution order, defaults, and secrets. Routing →
[AGENTS.md](AGENTS.md#canonical-sources).

txstore's own configuration is keyword arguments and `TX_STORE_*` variables only; an application
that exposes its own flags maps them to the keyword arguments below. The HTTP and S3 clients also
honour their libraries' standard environment → [Secrets](#secrets).

## Resolution

| Rule | Behavior |
| -- | -- |
| Order | Explicit keyword argument, then the `TX_STORE_*` variable, then the default. A `None` argument counts as not given. |
| Lazy read | A variable is read only when its argument is `None`, so an invalid value fails only a call that falls back to it. |
| Readers | Only `make_storage` and `HistoryRetention.from_env` read variables; backend constructors and `HistoryRetention(...)` take explicit values only. |
| Timing | Variables are read on each call, never at import. |
| Values | Grammar and failures → [VALIDATION.md](VALIDATION.md#environment-values). |

## Storage construction

```python
make_storage(url, *, create_path=False, verify_ssl=None, s3_access=None, s3_secret=None,
             s3_default_acl=None, s3_bucket_acl=None)
```

→ [STORAGE.md](STORAGE.md#construction).

| Argument | Variable | Default | Notes |
| -- | -- | -- | -- |
| `create_path` | none | `False` | Create the S3 bucket or FS root when missing. |
| `verify_ssl` | `TX_STORE_VERIFY_SSL` | `True` | TLS verification for HTTP and S3; resolving to `False` logs a warning. |
| `s3_access` | `TX_STORE_S3_ACCESS` | none | Secret; required for `s3://`. |
| `s3_secret` | `TX_STORE_S3_SECRET` | none | Secret; required for `s3://`. |
| `s3_default_acl` | `TX_STORE_S3_DEFAULT_ACL` | `none` (`NO_ACL`) | Object ACL for writes that pass none → [STORAGE.md](STORAGE.md#s3-acl-rules); values → [VALIDATION.md](VALIDATION.md#s3-canned-acls). |
| `s3_bucket_acl` | `TX_STORE_S3_BUCKET_ACL` | `None` | Bucket ACL at creation → [STORAGE.md](STORAGE.md#construction). |

## History retention

`HistoryRetention.from_env(*, keep=None, max_age_days=None)` → [STORAGE.md](STORAGE.md#retention);
values → [VALIDATION.md](VALIDATION.md#history-retention).

| Argument | Variable | Default | Notes |
| -- | -- | -- | -- |
| `keep` | `TX_STORE_HISTORY_KEEP` | `20` (`HISTORY_KEEP_DEFAULT`) | Floor of protected ids → [STORAGE.md](STORAGE.md#retention). |
| `max_age_days` | `TX_STORE_HISTORY_MAX_AGE_DAYS` | `90` (`HISTORY_MAX_AGE_DAYS_DEFAULT`) | Expiry age → [STORAGE.md](STORAGE.md#retention). |

## Environment variables

| Variable | Read by | Type |
| -- | -- | -- |
| `TX_STORE_VERIFY_SSL` | `make_storage` | Boolean |
| `TX_STORE_S3_ACCESS` | `make_storage` | String, secret |
| `TX_STORE_S3_SECRET` | `make_storage` | String, secret |
| `TX_STORE_S3_DEFAULT_ACL` | `make_storage` | Object canned ACL or `none` |
| `TX_STORE_S3_BUCKET_ACL` | `make_storage` | Bucket canned ACL |
| `TX_STORE_HISTORY_KEEP` | `HistoryRetention.from_env` | Integer |
| `TX_STORE_HISTORY_MAX_AGE_DAYS` | `HistoryRetention.from_env` | Integer |

## Secrets

- `TX_STORE_S3_ACCESS` and `TX_STORE_S3_SECRET`, and the matching arguments, are secrets: never
  logged, never part of `repr`, never written to storage.
- Credentials in a URL's userinfo (`user:password@host`) and its query string are replaced by `***`
  in logs, `repr`, and exception messages; which URLs are accepted →
  [VALIDATION.md](VALIDATION.md#storage-urls).
- Percent-encode non-ASCII characters and `/`, `?`, `#`, and `@` in URL credentials; how an
  unencoded one parses → [VALIDATION.md](VALIDATION.md#storage-urls). A URL whose authority does not
  parse appears as `<unparseable URL>`; a password that parses as a host, port, and path is kept out
  of logs only by the encoding.
- Object keys are names, not secrets: backends name them in logs and exception messages, `;params`
  included; HTTP join refusals that name only the base →
  [VALIDATION.md](VALIDATION.md#http-url-join-safety). Carry credentials in URL userinfo, netrc, or
  the S3 settings, never in a key.
- Prefer the variables, or a caller's secret store, to literals in code.
- The HTTP backend sends URL userinfo as Basic auth on URLs stripped of it, so no `requests` error
  or chained cause quotes the credentials.
- The HTTP backend sends `~/.netrc` (or `$NETRC`) credentials for the URL's host, overriding URL
  userinfo, and routes through `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`; `REQUESTS_CA_BUNDLE`
  replaces the CA store but never turns verification off. boto3 reads its own variables for S3.
- Over plain `http://`, URL userinfo and netrc credentials travel as cleartext Basic auth; the HTTP
  backend warns at construction when such a URL carries userinfo, and once per instance when it
  sends netrc credentials. Use `https://` for an authenticated server.

## Maintenance

A new setting adds its keyword argument, its `_env` read with a `TX_STORE_` literal, and its row
here in the same change; `scripts/check_docs.py` fails when a read variable has no
[Environment variables](#environment-variables) row or a row names no read variable.
