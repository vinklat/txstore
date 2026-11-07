# txstore guides

These guides explain what txstore does and how to use it from Python. They assume Python knowledge
but no knowledge of the code or of the contract documents.

The guides are explanatory and non-canonical. When a guide and a contract document disagree, the
contract is right and the guide needs a fix. Each guide ends with a "Further reading" section that
names the contracts behind it.

## Conventions

- **Imports.** Every example imports from `txstore`; submodules are not a supported surface.
- **Paths.** Object paths are relative to the storage root, with `/` separators, on every backend.
- **Digests.** A digest dict has the keys `MD5Sum`, `SHA1`, `SHA256`, `SHA512`, and `Size`.
- **Examples.** Examples share one story: a release directory `/srv/data` on disk and the bucket
  `releases` on `s3.example.com`. Credentials come from `TX_STORE_S3_ACCESS` and
  `TX_STORE_S3_SECRET`.

## Reading order

| Guide | What you will learn |
| -- | -- |
| [Introduction](introduction.md) | What txstore is, the ideas behind it, and one end-to-end example. |
| [Backends](backends.md) | The four backends, storage URLs, `make_storage`, and configuration. |
| [Transactions](transactions.md) | Staging, committing, discarding, and reverting writes. |
| [History and retention](history.md) | The transaction log, reverting, pruning, and the writer lock. |

## The contract documents

The repository root holds the contract documents: the single source of truth for every behavior,
written for implementers and automation. You do not need them to use the library. Which contract
owns what → [AGENTS.md](../../AGENTS.md#canonical-sources).

## For contributors

Explanations of the implementation, flow diagrams, and code maps live in
[docs/development/](../development/README.md).
