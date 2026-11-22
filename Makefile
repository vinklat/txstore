# Developer commands and quality gates → SETUP.md#quality-gates.

RUN := uv run --locked
MDFORMAT := $(RUN) mdformat --compact-tables --number --wrap 100
# The debian-testing job's container in .github/workflows/ci.yml → SETUP.md#dependency-policy.
DEBIAN_IMAGE := debian:forky

.PHONY: help install fmt lint test test-strict docs-check diagrams diagrams-check lock-check \
	build static check debian-test

help: ## List targets
	@grep -E '^[a-z][a-z-]*:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-15s %s\n", $$1, $$2}'

install: ## Sync the virtualenv from uv.lock
	uv sync --locked

fmt: ## Format Python and Markdown (root and docs/), auto-fix lint
	$(RUN) ruff format .
	$(RUN) ruff check --fix .
	$(MDFORMAT) *.md docs

lint: ## Format check (Python, Markdown), lint, strict type check
	$(RUN) ruff format --check .
	$(MDFORMAT) --check *.md docs
	$(RUN) ruff check .
	$(RUN) mypy

test: ## Tests with the coverage gate; missing backends skip
	$(RUN) pytest --cov

test-strict: ## Tests with the coverage gate; a missing backend fails
	$(RUN) pytest --cov --strict-backends

docs-check: ## Contract and docs parity
	$(RUN) python scripts/check_docs.py

diagrams: ## Render stale PlantUML diagrams (needs Docker)
	$(RUN) python scripts/render_diagrams.py

diagrams-check: ## Check diagram SVGs are fresh (offline)
	$(RUN) python scripts/render_diagrams.py --check

lock-check: ## Check uv.lock matches pyproject.toml
	uv lock --check

build: ## Build the sdist and wheel into dist/
	uv build

static: lint docs-check diagrams-check lock-check build ## Every gate except tests

check: static test ## Every quality gate

# COPYFILE_DISABLE keeps macOS tar from adding AppleDouble ._* files; GNU tar ignores it.
debian-test: ## The CI debian-testing job in a throwaway container (needs Docker)
	COPYFILE_DISABLE=1 tar -c --exclude=./.git --exclude=./.venv --exclude=./dist --exclude='*_cache' . \
		| docker run --rm -i $(DEBIAN_IMAGE) \
			sh -c 'mkdir /src && cd /src && tar -x && sh .github/debian-test.sh'
