#!/usr/bin/env python3
"""Render ``docs/**/*.puml`` to committed SVG and check that every SVG is current.

GitHub does not render PlantUML fences, so each diagram lives in ``docs/<dir>/diagrams/*.puml`` and
its page embeds the sibling ``.svg``. Rendering runs the pinned PlantUML Docker image in pipe mode
and stamps each SVG with the PlantUML version and the SHA-256 of its source, so ``--check`` detects
a stale or missing render offline, with only the standard library.

``--check`` reports:

- a ``.puml`` without a rendered ``.svg``;
- an ``.svg`` whose stamp is missing or names another source hash or PlantUML version;
- an ``.svg`` in a ``diagrams/`` directory without its ``.puml``;
- a ``plantuml`` or ``puml`` fence in a root ``*.md`` or ``docs/**/*.md``.

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
RENDER_ARGS = ("-tsvg", "-pipe", "-nometadata", "-failfast2", "-SbackgroundColor=white")

STAMP_RE = re.compile(r"<!-- render_diagrams plantuml=(\S+) sha256=([0-9a-f]{64}) -->")
XML_DECL_RE = re.compile(r"^<\?xml[^>]*\?>")
PLANTUML_FENCE_RE = re.compile(r"^\s{0,3}(?:`{3,}|~{3,})\s*(?:plantuml|puml)\b")
RERUN = "re-run scripts/render_diagrams.py"


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _sources() -> list[Path]:
    return sorted((REPO_ROOT / "docs").rglob("*.puml"))


def _digest(source: Path) -> str:
    return hashlib.sha256(source.read_bytes()).hexdigest()


def stamp(svg: str, digest: str) -> str:
    """``svg`` with the render stamp after its XML declaration, or first when it has none."""
    comment = f"<!-- render_diagrams plantuml={PLANTUML_VERSION} sha256={digest} -->"
    decl = XML_DECL_RE.match(svg)
    if decl is None:
        return f"{comment}\n{svg}"
    return f"{svg[: decl.end()]}\n{comment}{svg[decl.end() :]}"


def read_stamp(svg: str) -> tuple[str, str] | None:
    """``(plantuml version, source sha256)`` of a stamped SVG, or ``None`` when unstamped."""
    match = STAMP_RE.search(svg)
    return None if match is None else (match.group(1), match.group(2))


def is_current(source: Path) -> bool:
    """The sibling SVG exists and was rendered from this source by the pinned PlantUML."""
    svg = source.with_suffix(".svg")
    if not svg.is_file():
        return False
    return read_stamp(svg.read_text()) == (PLANTUML_VERSION, _digest(source))


def check() -> list[str]:
    """Every problem with the diagram sources, their renders, and the documents."""
    errors: list[str] = []
    for source in _sources():
        svg = source.with_suffix(".svg")
        if not svg.is_file():
            errors.append(f"{_relative(source)}: no rendered {svg.name}; {RERUN}")
        elif not is_current(source):
            errors.append(f"{_relative(svg)}: stale; {RERUN}")
    for svg in sorted((REPO_ROOT / "docs").rglob("diagrams/*.svg")):
        if not svg.with_suffix(".puml").is_file():
            errors.append(f"{_relative(svg)}: no source {svg.with_suffix('.puml').name}")
    documents = [*sorted(REPO_ROOT.glob("*.md")), *sorted((REPO_ROOT / "docs").rglob("*.md"))]
    for document in documents:
        for lineno, line in enumerate(document.read_text().splitlines(), 1):
            if PLANTUML_FENCE_RE.match(line):
                errors.append(
                    f"{_relative(document)}:{lineno}: inline plantuml fence; "
                    "move it to docs/<dir>/diagrams/*.puml"
                )
    return errors


def render(force: bool = False) -> int:
    """Render each stale source (every source with ``force``); return the number rendered."""
    rendered = 0
    for source in _sources():
        if not force and is_current(source):
            continue
        result = subprocess.run(
            ["docker", "run", "--rm", "-i", IMAGE, *RENDER_ARGS],
            input=source.read_bytes(),
            capture_output=True,
            check=False,
        )
        output = result.stdout.decode()
        if result.returncode != 0 or "<svg" not in output:
            raise SystemExit(
                f"render_diagrams: {_relative(source)} failed (exit {result.returncode})\n"
                f"{result.stderr.decode()}"
            )
        source.with_suffix(".svg").write_text(stamp(output, _digest(source)))
        print(f"rendered {_relative(source.with_suffix('.svg'))}")
        rendered += 1
    return rendered


def main(argv: list[str] | None = None) -> int:
    """Render stale diagrams, or with ``--check`` report problems and exit 1 on any."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail on stale diagrams")
    mode.add_argument("--force", action="store_true", help="render every diagram")
    args = parser.parse_args(argv)
    if not args.check:
        rendered = render(force=args.force)
        print(f"render_diagrams: rendered {rendered} of {len(_sources())} diagrams")
        return 0
    errors = check()
    for error in errors:
        print(error)
    if errors:
        print(f"render_diagrams: {len(errors)} problem(s)")
        return 1
    print(f"render_diagrams: OK ({len(_sources())} diagrams)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
