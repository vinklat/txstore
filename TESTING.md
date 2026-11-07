# Required test contract

Owns: required test coverage, harness rules, and gates. Routing →
[AGENTS.md](AGENTS.md#canonical-sources). Tests assert the owning contract directly; definitions are
not copied here.

## Harness and gates

- Run with `uv run pytest` → [SETUP.md](SETUP.md#test).
- Keep tests offline: no real network and no real cloud account. S3 tests use the session-scoped
  `moto_server` fixture; HTTP tests use `responses`.
- `tests/conftest.py` clears every `TX_STORE_*` variable before each test; a test sets one with
  `monkeypatch.setenv`.
- Shared stubs, the in-memory `MemoryTxnStorage` backend, and history helpers live in
  `tests/helpers/storage.py`; prefer them over ad-hoc setup.
- Build moto-backed storages with `s3_bucket` / `s3_storage` and botocore errors with `client_error`
  from `tests/helpers/s3.py`.
- Import only the private module a test covers; route every other import through the façade.
- Name test functions `test_<subject>_<scenario>`; parameterize cases that differ only in input,
  with readable ids.
- Keep every parameter set non-empty: `empty_parameter_set_mark = "fail_at_collect"` in
  `pyproject.toml` turns an empty set into a collection error, not a skip.
- Skip a test whose backend is missing (moto server, user xattrs, POSIX ACL xattrs, setgid bits, an
  S3 server that ignores `If-Match`) through `tests/helpers/backends.py`; `--strict-backends` turns
  those skips into failures; CI use → [SETUP.md](SETUP.md#quality-gates).

| Gate | Contract |
| -- | -- |
| Validation | Every rule in `VALIDATION.md` has a success and a failure case. |
| Coverage | `uv run pytest --cov` fails below the branch-coverage `fail_under` in `pyproject.toml`. |
| Configuration | Every `CONFIG.md` setting is asserted in the [resolution order](CONFIG.md#resolution). |
| Schemes | Every URL-routable storage scheme has direct backend tests. |
| Public methods | Every public function and method has a success case; every one that raises has a case per raising condition → [EXCEPTIONS.md](EXCEPTIONS.md#rules). |
| Contract docs | `tests/test_check_docs.py` runs `scripts/check_docs.py` against the repository. |
| Package contracts | `tests/test_package.py` → [Package contracts](#package-contracts). |

## Required coverage

| Area | Required assertion |
| -- | -- |
| Storage backends | Shared `Storage` contract, backend-specific behavior, the error mapping of every library failure with its metric, and the transaction lifecycle (begin, commit, discard, revert, staged metadata) where supported, including a primitive failing mid-commit, mid-revert, mid-discard, and mid-staging. |
| Capability stubs | Read-only and write-only backends prove unsupported operations raise `NotImplementedError`. |
| Construction | URL dispatch, missing-host and empty-URL refusal, `create_path`, credential resolution and refusal, a default ACL never becoming the bucket ACL, `NO_ACL`, TLS verification for S3 and HTTP, the TLS-off warning, and URL credential redaction, chained causes included. |
| HTTP backend | Redirects refused for every read method, header allowlist, credentials absent from `repr`, logs, and messages. |
| Transaction wiring | `maybe_transaction` commits on success, discards on failure, is a no-op on a non-transactional storage, and passes its retention policy to the prune. |
| History records | Appended only for non-empty commits and removed on revert; a malformed record, including one that is no real instant, rejected on load; an id outside the [Record](STORAGE.md#record) `id` range or a bool refused with `ValueError` by `TransactionModel` and `transaction_begin`. |
| History locking | Acquisition and expiry per backend; a transactional backend without a lock refused; a prune blocked by a peer's in-flight transaction; on `s3`, a second instance refused at once. |
| History lock nesting | Every lock taker refused, not left waiting, inside an active transaction, begun by hand or by `maybe_transaction`, and on an `fs` root the thread already locked through another instance, symlink, or case alias. |
| History retention | Age expiry with the keep-count floor by id; a record exactly at the age limit kept; a young record sharing an expired record's id kept; a malformed history fails before any delete; a committed write never fails. |
| History reclaim | Expired footmarks reclaimed from the commit's own listing with the removed paths reported; dry run reports the same paths; a footmark matched only on its exact `.<token>` tail. |
| History orphans and forget | Orphans swept only when asked; a symlinked footmark refused on reclaim as on discard; a forget handed found paths lists nothing and deletes only its own footmarks. |
| Footmark wire | Golden vectors captured from a protobuf runtime for encode and decode, including negative and boundary values; proto3 decoding of absent, repeated, unknown, and wrong-wire-type fields; every rejection rule → [STORAGE.md](STORAGE.md#footmarks). |
| Timestamps | Golden text forms for every fraction width and range edge; accepted and rejected parse inputs; range refusal; datetime round trip → [STORAGE.md](STORAGE.md#timestamps). |
| S3 client | Bucket and object operations against moto; group-ACL carry-over on copy; metadata merge, removal, header and ACL preservation; the multipart rewrite and its abort → [STORAGE.md](STORAGE.md#s3-metadata-rewrites). |
| Environment | Variable grammar, refusal messages naming the variable, unprefixed names ignored, secrets absent from logs. |
| Digests | Streamed and in-memory digests, normalization, and the comparison rules → [STORAGE.md](STORAGE.md#digests). |

## Package contracts

`tests/test_package.py` walks `src/txstore` and `tests/` with `ast` for the boundary checks.

| Area | Required assertion |
| -- | -- |
| Surface | `txstore.__all__` equals the expected list in the test, every name resolves, `__version__` is read from the distribution and not exported, and `py.typed` ships. |
| Runtime dependencies | Every runtime import is the standard library or a declared dependency, and the declared list is exact → [STRUCTURE.md](STRUCTURE.md#dependencies). |
| Environment | Only `_env` reads the environment. |
| Import directions | Every row of [STRUCTURE.md](STRUCTURE.md#dependencies) has an assertion, including the empty deferred-import set. |
| Test imports | A name in `txstore.__all__` is imported from `txstore`; a submodule only by the file that covers it (`test_<m>.py` covers `<m>` or `_<m>`), with named exceptions in the test → [Harness and gates](#harness-and-gates). |
| Exceptions | Every `EXCEPTIONS.md` class is, alone, the first argument of a `pytest.raises` under `tests/`; a tuple credits none. |

## File layout

One file per source module or contract surface, named `test_<module>.py`; the backends keep their
module names (`test_storage_fs.py`). `src/txstore/__init__.py` is covered by the package contracts
alone. Files that cover a contract surface rather than one module:

| File | Covers |
| -- | -- |
| `test_transactions.py` | Transaction lifecycle and reclamation shared by every `FootmarkMixin` backend, parametrized over `MemoryTxnStorage`, `StorageFS`, and `StorageS3`, with failure injection. |
| `test_fs_transactions.py`, `test_s3_transactions.py` | Backend-specific transaction behavior: symlink confinement and the per-path reclaim; ACL carry-over, the batched S3 reclaim, and a locked `prune`. |
| `test_package.py` | Package contracts → [Package contracts](#package-contracts). |
| `test_check_docs.py` | `scripts/check_docs.py` checks and a clean run on the repository. |
| `test_harness.py` | The `--strict-backends` switch of `tests/helpers/backends.py` and its `tests/conftest.py` wiring. |

## Maintenance

For each change: update the owning contract, add success and failure tests, then update only the
distinct assertion here. Prefer parameterization over repeated narratives; do not copy values from
the owning contracts.
