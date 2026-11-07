# Setup and release contract

Owns: install, test, quality gates, diagrams, dependency policy, release, and upgrade notes. Routing
→ [AGENTS.md](AGENTS.md#canonical-sources).

## Prerequisites and install

- Python 3.13 or newer. `fs://` metadata needs a filesystem with extended attributes.
- Library users: `pip install txstore`.
- Contributors: [uv](https://docs.astral.sh/uv/); dependencies resolve from public PyPI only.

```bash
uv sync
```

## Test

Harness rules → [TESTING.md](TESTING.md#harness-and-gates); required coverage →
[TESTING.md](TESTING.md#required-coverage).

```bash
uv run pytest
uv run pytest --cov
```

## Quality gates

Run every row before submitting. `.github/workflows/ci.yml` runs the check rows on pushes to `main`,
on pull requests, weekly, and before each release; it checks the lockfile through
`uv sync --locked`.

| Check | Command |
| -- | -- |
| Format | `uv run ruff format` |
| Format check | `uv run ruff format --check .` |
| Lint | `uv run ruff check .` (locally `--fix`) |
| Type check | `uv run mypy`; mode and scope → [STYLE.md](STYLE.md#python-baseline) |
| Tests | `uv run pytest --cov`; the CI `test` job adds `--strict-backends`, the Debian job does not |
| Lockfile | `uv lock --check` |
| Contract docs | `uv run python scripts/check_docs.py` |
| Diagrams | `uv run python scripts/render_diagrams.py --check` |
| Build | `uv build` |

`check_docs.py` verifies links and anchors in root contracts and `docs/`, contract anchors cited in
code, `CONFIG.md` parity with the `TX_STORE_*` variables read, `EXCEPTIONS.md` parity with the
exception classes, 100-column prose, table rows with as many cells as their header, no `TODO`
markers in contracts, the `docs/*/README.md` indexes, the README version, PyPI-only package sources,
and the [diagram](#diagrams) fence and embed rules.

## Diagrams

GitHub does not render PlantUML, so diagrams are committed as SVG next to their source.

| Rule | Detail |
| -- | -- |
| Source | One diagram per `docs/<dir>/diagrams/*.puml`, wrapped in a bare `@startuml`/`@enduml`. |
| No URL | A source contains no URL: renders run offline. |
| Embed | A document embeds each rendered `.svg` as an inline Markdown image with a bare relative path and an optional title; reference-style images and `<path>` destinations do not count. `check_docs.py` reports an SVG that no document displays that way. |
| Fences | No document opens a `plantuml` or `puml` fence; `check_docs.py` reports one outside another fenced block. |
| Render | `uv run python scripts/render_diagrams.py` renders each stale source through the pinned `plantuml/plantuml` Docker image with no network. |
| Force | `render_diagrams.py --force` renders every source, stale or not. |
| Docker | Rendering needs Docker; `--check` does not. |
| Stamp | Line 1 of each SVG carries the PlantUML version and a SHA-256 of the image, the PlantUML arguments, and the source. |
| Check | `render_diagrams.py --check` reports, offline, a `.puml` under `docs/` outside a `diagrams/` directory, a URL in a source, a missing or stale SVG, and an SVG without its source. |
| Upgrade | Changing `IMAGE` or `RENDER_ARGS` in the script marks every SVG stale; re-render and commit them together. |
| Diff | `.gitattributes` marks rendered SVGs `linguist-generated`, so GitHub collapses their diffs. |

## Dependency policy

txstore is packaged for Debian, so its dependencies stay installable from Debian testing.

| Rule | Detail |
| -- | -- |
| Floors | Every runtime dependency's lower bound is available in Debian testing (forky until its release, then the next testing). |
| Python | Every Python version Debian testing ships is supported and in CI. |
| Typing | Stub and typing-only packages are imported only under `TYPE_CHECKING` and stay in the `dev` group. |
| Generated code | No module is generated code pinned to a runtime version. |
| Tests | Run offline from the source distribution, with `pytest`, `moto`, and `responses` as the only test-only packages and no dependence on `addopts` or a coverage plugin: the Debian job installs only those. |
| Source distribution | Holds `src`, `tests`, `docs`, `scripts`, the root documents, `LICENSE`, and `uv.lock`, so a distribution build and its tests need no git checkout. |
| CI | The `debian-testing` job runs `.github/debian-test.sh`: the suite on Debian's packaged dependencies, pip installing only txstore with no index; triggers → [quality gates](#quality-gates). |

A floor raise names the Debian testing version that satisfies it in the change description.

## Release

`.github/workflows/release.yml` publishes to PyPI with Trusted Publishing; no API token is stored.

| Item | Rule |
| -- | -- |
| Version | `pyproject.toml` `[project].version` and the `README.md` "Current version" line. |
| Version parity | `check_docs.py` fails when the two differ. |
| `__version__` | Read from the installed distribution. |
| Version bump | `uv version --bump patch` (or `minor`, `major`), the README line to match, and every `Unreleased:` upgrade note renamed to `<version>:`. |
| Steps | Version bump → [quality gates](#quality-gates) → merge to `main` → `git tag vX.Y.Z` on `main` → `git push origin vX.Y.Z`. |
| Trigger | Pushing a tag `vX.Y.Z`, with or without a suffix (e.g. `v0.2.0rc1`). |
| Tag format | `vX.Y.Z`, the pattern the planned `debian/watch` tracks → [ROADMAP.md](ROADMAP.md#phases). |
| Gates | `ci.yml` runs in full before the build. |
| Tag checks | The tagged commit is on `main`, and the tag without its `v` equals the `pyproject.toml` version, or the release stops. |
| Build | sdist and wheel, without the uv cache. |
| Build backend | hatchling only from the hash-pinned `.github/build-constraints.txt`; its header holds the refresh command. |
| Build backend floor | `[build-system]` keeps a floor, so a Debian build uses Debian's hatchling. |
| PyPI description | `README.md`, with its relative links rewritten to the tag on GitHub; the repo copy is unchanged. |
| Checks | Run after the artifact upload, so no tool they fetch alters the published files; a failure stops the release. |
| Link check | No relative link is left in the wheel's description. |
| Metadata check | `twine check --strict` passes. |
| Smoke test | The wheel, installed alone, imports and its `txstore.__version__` equals the tag version. |
| Stages | TestPyPI, then PyPI, then a GitHub release with the sdist and wheel and a link to the [upgrade notes](#upgrade-notes). |
| GitHub environments | `testpypi` and `pypi`; `pypi` has a required reviewer, so the PyPI upload waits for approval. |
| Tag ruleset | A repository ruleset limits who creates `v*` tags. |
| Trusted publisher identity | Owner `vinklat`, repo `txstore`, workflow `release.yml`. |
| Trusted publisher on TestPyPI | That identity with environment `testpypi`. |
| Trusted publisher on PyPI | That identity with environment `pypi`. |

## Upgrade notes

- 0.1.0: initial public release.
