#!/usr/bin/env python3
"""Render ``docs/**/diagrams/*.puml`` to committed SVG and check that every SVG is current.

GitHub does not render PlantUML fences, so each diagram lives in ``docs/<dir>/diagrams/*.puml`` and
its page embeds the sibling ``.svg`` (fences and embedding → ``check_docs.py``). Rendering runs the
pinned PlantUML Docker image in pipe mode with no network. Line 1 of each SVG is a stamp holding the
PlantUML version and a SHA-256 of the image, the PlantUML arguments, and the source, so ``--check``
detects a stale render offline, with only the standard library.

``--check`` reports:

- a ``.puml`` under ``docs/`` outside a ``diagrams/`` directory;
- a URL in a source, which an offline render cannot fetch;
- a source without a rendered ``.svg``, or with one whose stamp does not match;
- an ``.svg`` in a ``diagrams/`` directory without its ``.puml``.

Usage:
    uv run python scripts/render_diagrams.py            # render stale diagrams (needs Docker)
    uv run python scripts/render_diagrams.py --force    # render every diagram
    uv run python scripts/render_diagrams.py --check    # fail on stale diagrams
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLANTUML_VERSION = "1.2026.9"
IMAGE = (
    f"plantuml/plantuml:{PLANTUML_VERSION}"
    "@sha256:9b5071ab56a4192aac458ab4b0d42261c22c8109695894ae5be6013c3be6cac1"
)
# Container options; they do not change the output, so the stamp does not hash them.
DOCKER_ARGS = ("docker", "run", "--rm", "-i", "--network=none")
RENDER_ARGS = ("-tsvg", "-pipe", "-nometadata", "-failfast2", "-SbackgroundColor=white")

DIAGRAMS = "diagrams"
URL_RE = re.compile(r"https?://")
RERUN = "re-run scripts/render_diagrams.py"


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _sources() -> list[Path]:
    return sorted((REPO_ROOT / "docs").rglob(f"{DIAGRAMS}/*.puml"))


def _recipe(data: bytes) -> str:
    """SHA-256 of the render recipe: the image, the PlantUML arguments, and the source ``data``."""
    recipe = hashlib.sha256()
    for part in (IMAGE, *RENDER_ARGS):
        recipe.update(part.encode() + b"\0")
    recipe.update(data)
    return recipe.hexdigest()


def digest(source: Path) -> str:
    """SHA-256 of the render recipe of ``source`` as it is now."""
    return _recipe(source.read_bytes())


def stamp_line(recipe: str, version: str = PLANTUML_VERSION) -> str:
    """The stamp comment for a render of ``recipe`` by PlantUML ``version``."""
    return f"<!-- render_diagrams plantuml={version} sha256={recipe} -->"


def stamp(svg: str, recipe: str) -> str:
    """``svg`` with the stamp as its first line; PlantUML emits no XML declaration to precede."""
    return f"{stamp_line(recipe)}\n{svg}"


def problem(source: Path) -> str | None:
    """Why the render of ``source`` is not current, or ``None`` when it is."""
    svg = source.with_suffix(".svg")
    if not svg.is_file():
        return f"{_relative(source)}: no rendered {svg.name}; {RERUN}"
    if not svg.read_text(encoding="utf-8").startswith(stamp_line(digest(source)) + "\n"):
        return f"{_relative(svg)}: stale; {RERUN}"
    return None


def check(sources: list[Path] | None = None) -> list[str]:
    """Every problem with the diagram sources and their renders."""
    sources = _sources() if sources is None else sources
    errors = [
        f"{_relative(path)}: diagram source outside a {DIAGRAMS}/ directory"
        for path in sorted((REPO_ROOT / "docs").rglob("*.puml"))
        if path.parent.name != DIAGRAMS
    ]
    for source in sources:
        for lineno, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if URL_RE.search(line):
                errors.append(
                    f"{_relative(source)}:{lineno}: URL in a diagram; renders are offline"
                )
        found = problem(source)
        if found is not None:
            errors.append(found)
    for svg in sorted((REPO_ROOT / "docs").rglob(f"{DIAGRAMS}/*.svg")):
        if not svg.with_suffix(".puml").is_file():
            errors.append(f"{_relative(svg)}: no source {svg.with_suffix('.puml').name}")
    return errors


def _plantuml(source: Path, data: bytes) -> str:
    """SVG text of ``data``, read from ``source``, rendered by the pinned image; exit on failure."""
    try:
        result = subprocess.run(
            [*DOCKER_ARGS, IMAGE, *RENDER_ARGS],
            input=data,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        raise SystemExit(
            "render_diagrams: docker not found; rendering needs Docker → SETUP.md#diagrams"
        ) from None
    output = result.stdout.decode("utf-8")
    if result.returncode != 0 or not output.startswith("<svg"):
        raise SystemExit(
            f"render_diagrams: {_relative(source)} failed (exit {result.returncode})\n"
            f"{result.stderr.decode('utf-8', errors='replace')}"
        )
    return output


def render(force: bool = False, sources: list[Path] | None = None) -> int:
    """Render each stale source (every source with ``force``); return the number rendered."""
    sources = _sources() if sources is None else sources
    rendered = 0
    for source in sources:
        if not force and problem(source) is None:
            continue
        svg = source.with_suffix(".svg")
        # One read feeds both the render and its stamp, so a save mid-render leaves the SVG stale.
        data = source.read_bytes()
        svg.write_text(stamp(_plantuml(source, data), _recipe(data)), encoding="utf-8")
        print(f"rendered {_relative(svg)}")
        rendered += 1
    return rendered


def main(argv: list[str] | None = None) -> int:
    """Render stale diagrams, or with ``--check`` report problems and exit 1 on any."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail on stale diagrams")
    mode.add_argument("--force", action="store_true", help="render every diagram")
    args = parser.parse_args(argv)
    sources = _sources()
    if not args.check:
        rendered = render(force=args.force, sources=sources)
        print(f"render_diagrams: rendered {rendered} of {len(sources)} diagrams")
        return 0
    errors = check(sources)
    for error in errors:
        print(error)
    if errors:
        print(f"render_diagrams: {len(errors)} problem(s)")
        return 1
    print(f"render_diagrams: OK ({len(sources)} diagrams)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
