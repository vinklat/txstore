# Coding contract

Owns: coding conventions, logging rules, and performance rules; prevails on style conflicts. Routing
→ [AGENTS.md](AGENTS.md#canonical-sources).

## Python baseline

- Use the [SETUP.md prerequisites](SETUP.md#prerequisites-and-install) Python floor. Apply PEP 8
  where Ruff is silent, strict type hints, and Google-style docstrings for public symbols.
- Ruff owns formatting, lint, and import order at line length 100: standard library, third-party,
  then `txstore`. Commands → [SETUP.md](SETUP.md#quality-gates).
- mypy runs in strict mode over `src/txstore`, `tests`, and `scripts`; `# type: ignore` names its
  error code.
- A `# noqa` names an enabled Ruff rule the line breaks (`RUF100` flags any other); no pragmas for
  tools outside the gates.
- In `src/txstore`, import at module top. A function-level import there only breaks a named import
  cycle and carries `# noqa: PLC0415` beside a comment naming it →
  [STRUCTURE.md](STRUCTURE.md#dependencies); tests and scripts are exempt.
- Use snake-case modules; a leading underscore marks a module outside the public surface. A
  module-level `_name` stays in its module; a helper another module imports drops the underscore.
- No side effects at import time: no environment reads, network, or filesystem writes →
  [CONFIG.md](CONFIG.md#resolution).
- Prefer immutable inputs and results; do not mutate a caller's dict or list.

## Logging

- Use `%` formatting in `logging.*` calls, never f-strings.
- Every module logger installs `logging.NullHandler()`; the library never configures handlers.
- Keep messages factual and low-noise; secrets never appear → [CONFIG.md](CONFIG.md#secrets).
- Exception raising and chaining rules → [EXCEPTIONS.md](EXCEPTIONS.md#rules).

## Performance

| Concern | Rule |
| -- | -- |
| Large objects | Stream with `get_stream`, `put_stream`, and `transfer`; never load an object just to digest it → [STORAGE.md](STORAGE.md#storage-abi). |
| Digests | Reuse `ChecksumOpen` for in-flight calculation → [STORAGE.md](STORAGE.md#digests). |
| Listings | List hidden objects once, pass the `listing` to every `find_*` classifier, and hand the paths found to the forget call → [STORAGE.md](STORAGE.md#lifecycle). |
| Batching | Delete many S3 keys through `_obj_reclaim_delete_many`, never one `DeleteObject` per key → [STORAGE.md](STORAGE.md#backend-primitives). |

## Maintenance

Style rules change here and in `pyproject.toml` together.
