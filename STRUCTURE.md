# Structure and dependency contract

Owns: directories, module ownership, the public façade, and internal import directions. Routing →
[AGENTS.md](AGENTS.md#canonical-sources).

## Layout

| Path | Owns |
| -- | -- |
| `src/txstore/__init__.py` | Public façade: `__all__` is the supported surface; submodules are not. |
| `src/txstore/base.py` | `Storage` ABI, `StorageType`, storage exceptions, `CHUNK_SIZE`, `REQUEST_TIMEOUT`. |
| `src/txstore/storage_fs.py`, `storage_s3.py`, `storage_http.py`, `storage_null.py` | One backend each → [STORAGE.md](STORAGE.md#backends). |
| `src/txstore/factory.py` | `make_storage()` and `NO_ACL`. |
| `src/txstore/transaction.py` | `FootmarkMixin` (lifecycle, overlay, primitives), `TransactionModel`, `is_transactional`. |
| `src/txstore/_reclaim.py` | `ReclaimMixin`: footmark classification and reclamation, inherited by `FootmarkMixin` → [STORAGE.md](STORAGE.md#lifecycle). |
| `src/txstore/history.py` | `TransactionHistory`, `HistoryRetention`, `history_lock`, `maybe_transaction`. |
| `src/txstore/footmark.py` | `ObjectFootmark`, `ObjectOperation`, token build and parse, footmark path build and split. |
| `src/txstore/hexdigest.py`, `metrics.py`, `url.py` | Digests, per-instance counters, URL and path helpers: the scheme table behind `get_supported_schemes()` and `parse_storage_url()`, `parse_s3_url_path()`, `redact_url`. |
| `src/txstore/_footmark_wire.py` | Footmark wire codec → [STORAGE.md](STORAGE.md#footmarks). |
| `src/txstore/_timestamp.py` | `Timestamp` → [STORAGE.md](STORAGE.md#timestamps). |
| `src/txstore/_env.py` | The only reader of `os.environ` → [CONFIG.md](CONFIG.md#resolution). |
| `src/txstore/_s3_client.py` | boto3 wrapper and canned-ACL literals → [STORAGE.md](STORAGE.md#s3-metadata-rewrites). |
| `src/txstore/_s3_acl.py`, `_s3_errors.py` | S3 public-ACL probe; `ClientError` classifiers and the `s3_errors` translation to `StorageException`. |
| `src/txstore/_s3_lock.py` | S3 history lock on conditional writes → [STORAGE.md](STORAGE.md#locking). |
| `src/txstore/_capabilities.py`, `_fs_paths.py`, `_reserved.py` | Capability stubs, FS path confinement and xattrs, reserved root names. |
| `tests/` | Tests, `conftest.py` fixtures, and `helpers/` → [TESTING.md](TESTING.md#file-layout). |
| `scripts/check_docs.py` | Contract and docs parity checker → [SETUP.md](SETUP.md#quality-gates). |
| `docs/guides/` | Non-canonical user guides. |
| `docs/development/` | Non-canonical contributor explanations and diagrams. |
| `docs/reference/` | Non-canonical reference artifacts, including `footmark.proto`. |
| `.github/` | `workflows/ci.yml` → [SETUP.md](SETUP.md#quality-gates); `debian-test.sh` → [SETUP.md](SETUP.md#dependency-policy); `workflows/release.yml` and `build-constraints.txt` → [SETUP.md](SETUP.md#release). |

`pyproject.toml` and `uv.lock` define the package and its development environment.

## Dependencies

Modules import each other in the directions below; `tests/test_package.py` asserts each row with
`ast` → [TESTING.md](TESTING.md#package-contracts).

| Rule | Detail |
| -- | -- |
| Façade | Callers import `txstore`; no submodule imports the façade. |
| Leaves | `_capabilities`, `_env`, `_footmark_wire`, `_s3_client`, `_timestamp`, `hexdigest`, and `metrics` import no `txstore` module. |
| Core | `base` imports only `hexdigest` and `metrics`; `url`, `_reserved`, and `_fs_paths` build on `base`; `_s3_acl` and `_s3_errors` on `base` and `_s3_client`; `_s3_lock` on `base`, `_s3_client`, and `_s3_errors`; `footmark` on the wire codec and `Timestamp`. |
| Transactions | `_reclaim` imports `footmark` and `_reserved`, and `transaction` for types only; `transaction` imports `base`, `footmark`, `_reserved`, and `_reclaim`; `history` imports `transaction` and no backend or `_s3_*` module. |
| Backends | No backend imports another. Only the façade and `factory` import a backend module; a lock is the backend's `_obj_lock` primitive. |
| Load-time cycles | None: no import is deferred → [STYLE.md](STYLE.md#python-baseline). |
| Type-only imports | `if TYPE_CHECKING:` imports, including the `mypy_boto3_s3` stubs, are exempt from these rules and from the runtime dependency list. |
| Runtime dependencies | Standard library, plus the modules `boto3`, `botocore`, `requests`, and `xattr` (the `pyxattr` distribution) → [SETUP.md](SETUP.md#dependency-policy). |

## Maintenance

A new module adds a [Layout](#layout) row; a new import direction updates
[Dependencies](#dependencies) and the matching assertion in `tests/test_package.py` together. A new
public name is exported from `src/txstore/__init__.py` and added to the surface assertion →
[TESTING.md](TESTING.md#package-contracts).
