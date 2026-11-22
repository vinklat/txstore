# Developer commands and quality gates → SETUP.md#quality-gates. CI runs these targets.

UV := uv run
MDFORMAT := $(UV) mdformat --compact-tables --number --wrap 100
# Follows the debian-testing job's container in .github/workflows/ci.yml.
DEBIAN_IMAGE := debian:forky

.PHONY: help install fmt lint test test-strict docs-check diagrams diagrams-check lock-check \
	build static check debian-test

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-15s %s\n", $$1, $$2}'

install: ## Sync the virtualenv from uv.lock
	uv sync --locked

fmt: ## Format Python and Markdown (root and docs/), auto-fix lint
	$(UV) ruff format .
	$(UV) ruff check --fix .
	$(MDFORMAT) *.md docs

lint: ## Format check (Python, Markdown), lint, strict type check
	$(UV) ruff format --check .
	$(MDFORMAT) --check *.md docs
	$(UV) ruff check .
	$(UV) mypy

test: ## Tests with the coverage gate; missing backends skip
	$(UV) pytest --cov

test-strict: ## Tests with the coverage gate; a missing backend fails (CI, dev container)
	$(UV) pytest --cov --strict-backends

docs-check: ## Contract and docs parity
	$(UV) python scripts/check_docs.py

diagrams: ## Render stale PlantUML diagrams (needs Docker)
	$(UV) python scripts/render_diagrams.py

diagrams-check: ## Check diagram SVGs are fresh (offline)
	$(UV) python scripts/render_diagrams.py --check

lock-check: ## Check uv.lock matches pyproject.toml
	uv lock --check

build: ## Build the sdist and wheel into dist/
	uv build

static: lint docs-check diagrams-check lock-check build ## Every gate except tests (CI lint job)

check: static test ## Every quality gate (run before each PR)

# COPYFILE_DISABLE keeps macOS tar from adding AppleDouble ._* files; GNU tar ignores it.
debian-test: ## The CI debian-testing job in a throwaway container (needs Docker)
	COPYFILE_DISABLE=1 tar -c --exclude=./.git --exclude=./.venv --exclude=./dist --exclude='*_cache' . \
		| docker run --rm -i $(DEBIAN_IMAGE) \
			sh -c 'mkdir /src && cd /src && tar -x && sh .github/debian-test.sh'
