# Setup and release contract

Owns: install, dev container, test, quality gates, diagrams, dependency policy, Debian package, and
release. Routing → [AGENTS.md](AGENTS.md#canonical-sources).

## Prerequisites and install

- Python 3.13 or newer. `fs://` metadata needs a filesystem with extended attributes.
- Library users: `pip install txstore`.
- Contributors: [uv](https://docs.astral.sh/uv/) and GNU make; dependencies resolve from public PyPI
  only. Every command runs from the repository root.

```bash
make install
```

A Linux environment with xattrs and POSIX ACLs → [dev container](#dev-container).

## Dev container

`.devcontainer/` runs every [quality gate](#quality-gates) on Debian testing's `python3`.

| Item | Rule |
| -- | -- |
| Base | The `debian-testing` job's image, renamed with it; a floating tag, so it tracks testing. |
| Dependencies | From `uv.lock`, not apt; the Debian floors stay the `debian-testing` job's → [dependency policy](#dependency-policy). |
| Python | Debian's `python3`, preferred over uv-managed Pythons. |
| Other Python | `UV_PYTHON=<version> uv sync` rebuilds the venv on that version, downloading it if Debian lacks it; the venv keeps it until the next sync. |
| Environment | `UV_PROJECT_ENVIRONMENT` puts the venv outside the workspace; the host `.venv` on the bind mount stays untouched. |
| Setup | `uv sync --locked` on create; the image installs `make`. |
| User | Non-root `dev` without sudo, so the tests that skip as root run; for root, `docker exec -u root`. |
| Tests | `make test-strict` passes: every backend leg → [TESTING.md](TESTING.md#harness-and-gates) is available. |
| Diagrams | No Docker inside: render on the host → [diagrams](#diagrams). |
| uv | Pinned by tag and digest in the `Dockerfile`, as a third-party binary; a bump updates both. |
| Check | Not in CI: after a change to `.devcontainer/`, rebuild it and run every quality gate inside. |

## Test

Harness rules → [TESTING.md](TESTING.md#harness-and-gates); required coverage →
[TESTING.md](TESTING.md#required-coverage).

| Command | Effect |
| -- | -- |
| `make test` | Every test with the coverage gate |
| `make test-strict` | The same, with backend skips as failures → [TESTING.md](TESTING.md#harness-and-gates) |
| `uv run pytest tests/test_x.py -k name` | One file or test |

## Quality gates

Run `make check` before submitting.

| Item | Rule |
| -- | -- |
| Commands | The `Makefile` holds each command line; `make help` lists the targets. |
| Lock | Tool targets run with `uv run --locked`: after editing `pyproject.toml`, run `uv lock`. |
| Formatting | `make fmt` formats Python, the root `*.md`, and `docs/`, and applies lint fixes; it is not a gate. |
| Backend legs | Skips → [TESTING.md](TESTING.md#harness-and-gates); every leg → [dev container](#dev-container). |
| CI jobs | `.github/workflows/ci.yml` runs `make install`, then `make static` in its `lint` job and `make test-strict` in its `test` job; its `debian-testing` job → [dependency policy](#dependency-policy). |
| CI triggers | Pushes to `main`, pull requests, weekly, and before each release. |

| Gate | Checks |
| -- | -- |
| `make lint` | Python, the root `*.md`, and `docs/` are formatted; lint passes; strict types → [STYLE.md](STYLE.md#python-baseline) |
| `make docs-check` | `check_docs.py`, below |
| `make diagrams-check` | → [diagrams](#diagrams) |
| `make lock-check` | `uv.lock` matches `pyproject.toml` |
| `make build` | The sdist and wheel build |
| `make static` | Every gate above |
| `make check` | `make static`, then `make test` → [test](#test) |

`check_docs.py` verifies links and anchors in root contracts and `docs/`, contract anchors cited in
code, `CONFIG.md` parity with the `TXSTORE_*` variables read, `EXCEPTIONS.md` parity with the
exception classes, 100-column prose, table rows with as many cells as their header, no `TODO`
markers in contracts, the `docs/*/README.md` indexes, the README version, a dated `CHANGELOG.md`
section for the `pyproject.toml` version, PyPI-only package sources, the [diagram](#diagrams) fence
and embed rules, and `make` targets: each one inline code outside fences cites has `## ` help, and
this file cites each one.

## Diagrams

GitHub does not render PlantUML, so diagrams are committed as SVG next to their source.

| Rule | Detail |
| -- | -- |
| Source | One diagram per `docs/<dir>/diagrams/*.puml`, wrapped in a bare `@startuml`/`@enduml`. |
| No URL | A source contains no URL: renders run offline. |
| Embed | A document embeds each rendered `.svg` as an inline Markdown image with a bare relative path and an optional title; reference-style images and `<path>` destinations do not count. `check_docs.py` reports an SVG that no document displays that way. |
| Fences | No document opens a `plantuml` or `puml` fence; `check_docs.py` reports one outside another fenced block. |
| Render | `make diagrams` renders each stale source through the pinned `plantuml/plantuml` Docker image with no network. |
| Force | `render_diagrams.py --force` renders every source, stale or not. |
| Docker | Rendering needs Docker; `--check` does not. |
| Stamp | Line 1 of each SVG carries the PlantUML version and a SHA-256 of the image, the PlantUML arguments, and the source. |
| Check | `make diagrams-check` (`render_diagrams.py --check`) reports, offline, a `.puml` under `docs/` outside a `diagrams/` directory, a URL in a source, a missing or stale SVG, and an SVG without its source. |
| Upgrade | Changing `IMAGE` or `RENDER_ARGS` in the script marks every SVG stale; re-render and commit them together. |
| Diff | `.gitattributes` marks rendered SVGs `linguist-generated`, so GitHub collapses their diffs. |

## Dependency policy

txstore is packaged for Debian, so its dependencies stay installable from Debian testing.

| Rule | Detail |
| -- | -- |
| Floors | Every runtime dependency's lower bound is available in Debian testing (forky until its release, then the next testing). |
| Floor-only dependencies | `urllib3` 2.6 or later, declared only for that floor. |
| Python | Every Python version Debian testing ships is supported and in CI. |
| Typing | Stub and typing-only packages are imported only under `TYPE_CHECKING` and stay in the `dev` group. |
| Generated code | No module is generated code pinned to a runtime version. |
| Tests | Run offline from the source distribution, with `pytest`, `moto`, and `responses` as the only test-only packages and no dependence on `addopts` or a coverage plugin: the Debian job installs only those. The Debian package's test set → [Debian package](#debian-package). |
| Source distribution | Holds `src`, `tests`, `docs`, `scripts`, the root documents, `LICENSE`, `Makefile`, and `uv.lock`, so a distribution build and its tests need no git checkout. |
| CI | The `debian-testing` job runs `.github/debian-test.sh`: the suite on Debian's packaged dependencies, pip installing only txstore with no index, without `make` or `--strict-backends`; triggers → [quality gates](#quality-gates). |
| Local Debian run | `make debian-test` runs `.github/debian-test.sh` in a throwaway container of the `debian-testing` job's image (`DEBIAN_IMAGE`), renamed with it, on the files git tracks or would track, uncommitted edits included; needs git and Docker. |

A floor raise names the Debian testing version that satisfies it in the change description.

## Debian package

txstore ships in Debian as `python3-txstore`, maintained by the Debian Python Team. The packaging
lives on salsa, not in this repository; planned release → [ROADMAP.md](ROADMAP.md#releases).

| Item | Rule |
| -- | -- |
| Names | Source `python-txstore`, binary `python3-txstore`, `Architecture: all`. |
| Maintainer | `Debian Python Team <team+python@tracker.debian.org>`; the upstream author in `Uploaders`; a team Debian Developer sponsors the uploads. |
| Repository | `salsa.debian.org/python-team/packages/python-txstore`, under the [team policy](https://salsa.debian.org/python-team/tools/python-modules/-/blob/master/policy.rst); this repository never carries `debian/`. |
| Branches | `debian/main`, `upstream`, and `pristine-tar`, the team's DEP-14 names; Debian tags are `debian/<version>`. |
| Upstream tarball | The GitHub tarball of tag `vX.Y.Z`, found by `debian/watch`; the `upstream` branch holds imported tarballs only, never upstream git history. |
| Versions | A PEP 440 pre-release suffix maps to `~` (`0.2.0rc1` → `0.2.0~rc1`), so it sorts before its final; the Debian revision starts at `-1`. |
| Build | `dh-python` with `pybuild-plugin-pyproject` and Debian's hatchling; the tests run on every Python 3 that unstable ships. |
| Package tests | The suite without `test_check_docs.py`, `test_render_diagrams.py`, and `test_package.py`, which read `scripts/`, `src/`, and the root documents; the `ci.yml` jobs run them. |
| Autopkgtest | `autopkgtest-pkg-pybuild`: the package tests against the installed package. |
| CI | Salsa CI on the team repository builds the package and runs lintian, autopkgtest, reprotest, and piuparts; here, the `debian-testing` job keeps the [dependency floors](#dependency-policy). |
| Lintian | No error or warning at `--pedantic`, except `unreleased-changes` while the changelog distribution is `UNRELEASED`. |
| Upload path | Join the team → ITP → team repository → team sponsor upload with binaries to NEW → buildd rebuild → testing migration. |
| Deadline | In testing before the forky soft freeze: a package missing from testing then is not in the release. |

## Release

`.github/workflows/release.yml` publishes to PyPI with Trusted Publishing; no API token is stored.

| Item | Rule |
| -- | -- |
| Version | `pyproject.toml` `[project].version` and the `README.md` "Current version" line. |
| Version parity | `check_docs.py` fails when the two differ. |
| `__version__` | Read from the installed distribution. |
| Changelog | Add each new public name, breaking change, and renamed or removed setting as a `Change` and `Migration` row under `## Unreleased`, in the same change → [CHANGELOG.md](CHANGELOG.md). |
| Version bump | `uv version --bump patch` (or `minor`, `major`), the README line to match, and the `CHANGELOG.md` heading `## Unreleased` renamed to `## <version> - <YYYY-MM-DD>`, given a one-line summary and a `Release →` link to its GitHub release, below a new empty `## Unreleased`. |
| Pre-1.0 versions | A minor bump starts a [ROADMAP.md](ROADMAP.md#releases) release; every other release is a patch, new public names and breaking changes included. Semantic versioning applies from 1.0.0. |
| Steps | Version bump → [quality gates](#quality-gates) → merge to `main` → `git tag vX.Y.Z` on `main` → `git push origin vX.Y.Z`. |
| Trigger | Pushing a tag `vX.Y.Z`, with or without a suffix (e.g. `v0.2.0rc1`). |
| Tag format | `vX.Y.Z`, the pattern `debian/watch` tracks → [Debian package](#debian-package). |
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
| Stages | TestPyPI, then PyPI, then a GitHub release with the sdist and wheel and a link to the tag's [CHANGELOG.md](CHANGELOG.md). |
| GitHub environments | `testpypi` and `pypi`; `pypi` has a required reviewer, so the PyPI upload waits for approval. |
| Tag ruleset | A repository ruleset limits who creates `v*` tags. |
| Trusted publisher identity | Owner `vinklat`, repo `txstore`, workflow `release.yml`. |
| Trusted publisher on TestPyPI | That identity with environment `testpypi`. |
| Trusted publisher on PyPI | That identity with environment `pypi`. |
