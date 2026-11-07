# AI coding agent guide

Root contracts decide behavior; the implementation shows current state. Keep one canonical owner per
fact; link instead of copying.

## Source priority

1. The root contract that owns the domain below; `STYLE.md` prevails on style.
2. Implementation under `src/txstore/` when checking current behavior.
3. `docs/guides/` for user how-tos, `docs/development/` for contributor explanations and diagrams,
   `docs/reference/` for reference artifacts, and `ROADMAP.md` for planned work and known
   deviations. All are non-canonical.

Resolve drift by updating the owning contract and implementation together, not by adding a second
definition.

## Canonical sources

| File | Owns |
| -- | -- |
| `STYLE.md` | Coding conventions, logging rules, performance rules; prevails on style |
| `STRUCTURE.md` | Layout, module ownership, public façade, internal import directions |
| `STORAGE.md` | Storage ABI, backends, construction, transactions, footmarks and their wire format, timestamps, history, S3 ACL and metadata rules, digests |
| `VALIDATION.md` | Input constraints at the URL, path, configuration, and retention boundaries |
| `EXCEPTIONS.md` | Exception classes, raising conditions, chaining rules |
| `CONFIG.md` | `make_storage` and retention settings, `TXSTORE_*` variables, defaults, secrets |
| `TESTING.md` | Required coverage, harness rules, gates |
| `SETUP.md` | Install, test, quality gates, diagrams, dependency policy, release, upgrade notes |

## Task routing

Read only the relevant sections; locate code from [STRUCTURE.md](STRUCTURE.md#layout).

| Task | Primary | Also check |
| -- | -- | -- |
| Backend / primitive | `STORAGE.md` backends and primitives | `STRUCTURE.md` layout, `VALIDATION.md` URLs, `TESTING.md` |
| Transaction / history / lock | `STORAGE.md` transactions and history | `EXCEPTIONS.md`, `TESTING.md` |
| Footmark wire / timestamp | `STORAGE.md` footmarks and timestamps | `docs/reference/footmark.proto`, `TESTING.md` golden vectors |
| S3 ACL / metadata | `STORAGE.md` S3 sections | `VALIDATION.md` canned ACLs, `CONFIG.md` |
| URL / path rule | `VALIDATION.md` | `EXCEPTIONS.md`, `TESTING.md` |
| Exception | `EXCEPTIONS.md` | Owning contract of the condition |
| Argument / `TXSTORE_*` variable | `CONFIG.md` | `VALIDATION.md` environment values, `src/txstore/_env.py` |
| Digest | `STORAGE.md` digests | `src/txstore/hexdigest.py` |
| Import boundary | `STRUCTURE.md` dependencies | `tests/test_package.py`, `STYLE.md` |
| Dependency change | `SETUP.md` dependency policy | `STRUCTURE.md` dependencies, `tests/test_package.py`, `.github/debian-test.sh` |
| Bug fix | Owning contract + `EXCEPTIONS.md` | `TESTING.md` |
| Tests | `TESTING.md` | Contract for the behavior under test |
| Contract / doc edit | Writing rules below → owning contract | `scripts/check_docs.py`, `SETUP.md` quality gates |
| Release | `SETUP.md` release | `.github/workflows/release.yml`, `ROADMAP.md` phases |
| Quality gate / CI change | `SETUP.md` quality gates | `.github/workflows/ci.yml` |
| Developer explanation / diagram | `docs/development/` | Owning root contract, `SETUP.md` diagrams |
| User how-to / future behavior | `docs/guides/` / `ROADMAP.md` | Owning root contract before implementing |

## Agent workflow

1. Read only the owning contract sections from the routing table; open the module named in
   [STRUCTURE.md](STRUCTURE.md#layout).
2. Implement within the import directions of [STRUCTURE.md](STRUCTURE.md#dependencies). Write errors
   as `condition → ExceptionClass` and link `EXCEPTIONS.md` instead of copying its table. A new
   setting → [CONFIG.md](CONFIG.md#maintenance); a new public name →
   [STRUCTURE.md](STRUCTURE.md#maintenance).
3. Add the narrowest test that falsifies the change per [TESTING.md](TESTING.md#maintenance).
4. Run the [SETUP.md quality gates](SETUP.md#quality-gates); contract and `docs/` edits also run
   `uv run python scripts/check_docs.py`. CI runs the gates again.

## Contract writing rules

- One owner per fact. Link it as `→ FILE.md#anchor`; never copy its table or values.
- Preamble opens with `Owns: <domain>. Routing → AGENTS.md.`, optionally followed by one short
  orienting paragraph. No cross-document pointer chains.
- Errors: `condition → ExceptionClass`; the class must be in `EXCEPTIONS.md` or be `ValueError` or
  `NotImplementedError`.
- Prefer tables; one fact per row. Narrative cells stay under ~300 characters: split rows rather
  than pad. Enumerations (method lists, value lists) are exempt.
- Tables use `| -- |` separator rows and unpadded cells; do not run a table aligner.
- Bare imperative rules; no RFC 2119 keywords. Headings are sentence case and unique per file.
- No TODOs or "current exception" notes in contracts; record them in
  [ROADMAP.md known deviations](ROADMAP.md#known-deviations) and link.
- Wrap prose at 100 columns; table rows are exempt. Prefer `→` shortcuts to sentences.
- After any contract or `docs/` edit run `uv run python scripts/check_docs.py`.
