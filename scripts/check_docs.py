#!/usr/bin/env python3
"""Fail when root contract documents drift from code or from each other.

Checks (SETUP.md quality gates; AGENTS.md contract writing rules):

- every ``[text](target)`` in root ``*.md`` and ``docs/**/*.md`` resolves relative to the linking
  document: a ``.md`` target must exist and its ``#anchor`` must be a heading slug; any other
  relative file target must exist. External links, ``]()``, and fenced code blocks are ignored;
- ``CONFIG.md`` environment-variable rows equal the ``TXSTORE_*`` names the source passes to the
  ``_env`` helpers, in both directions;
- the ``EXCEPTIONS.md`` exception table equals the ``class *Exception`` definitions in the source,
  and every backticked ``*Exception`` name in any document is a defined class;
- no document leaves a code fence open, which would hide every later line from every check above;
- no document opens a ``plantuml`` or ``puml`` fence, which GitHub does not render, and every
  ``docs/**/diagrams/*.svg`` is embedded as a Markdown image in a document (SETUP.md diagrams);
- prose lines stay within 100 columns (table rows, fenced blocks, lines carrying a URL, and
  single-token lines are exempt);
- every table row has as many cells as its header;
- root contracts carry no ``TODO``/``FIXME``/``XXX`` outside inline code (``ROADMAP.md`` exempt);
- every ``ROOT.md#anchor`` cited in ``src/`` or ``tests/`` names a root document and a heading;
- every ``docs/<dir>/`` holding pages has a ``README.md`` that links each sibling page;
- the ``README.md`` "Current version" line equals ``[project].version``;
- packages resolve from public PyPI only: ``pyproject.toml`` declares no custom index and every
  ``uv.lock`` registry is ``https://pypi.org/simple``.

Code facts are read from the source tree with ``ast``, never imported, so the checker runs with
only the standard library, as in CI.

Usage:
    uv run python scripts/check_docs.py
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections import Counter
from collections.abc import Container
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = Path("src") / "txstore"
LINE_LIMIT = 100
PYPI_REGISTRY = "https://pypi.org/simple"
ENV_HELPERS = frozenset({"env_str", "env_bool", "env_int"})

# Root documents allowed to carry TODO-style markers (deviations are recorded there by design).
TODO_EXEMPT: frozenset[str] = frozenset({"ROADMAP.md"})
# Fence info strings of diagrams GitHub does not render; they live as rendered SVG instead.
DIAGRAM_FENCES = frozenset({"plantuml", "puml"})

FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)")
HEADING_RE = re.compile(r"^(#+)\s+(.*?)\s*$")
LINK_RE = re.compile(r"\]\(([^)\s]*)\)")
# An unescaped inline ``![alt](dest)``, with an optional "title", 'title', or (title).
IMAGE_RE = re.compile(
    r"(?<!\\)!\[[^\]]*\]\(\s*([^)\s<>]+)(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# A code span: a backtick run closed by a run of the same length, within one paragraph.
CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(?:(?!\n\s*\n).)+?(?<!`)\1(?!`)", re.DOTALL)
EXTERNAL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)")
TABLE_SEP_RE = re.compile(r"^\|[\s:|-]+\|?\s*$")
CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
INLINE_CODE_RE = re.compile(r"`[^`]*`")
README_VERSION_RE = re.compile(r"Current version: \*\*([^*]+)\*\*")
TODO_RE = re.compile(r"\b(?:TODO|FIXME|XXX)\b")
URL_RE = re.compile(r"https?://")
CODE_REF_RE = re.compile(r"\b([A-Z][A-Z]+\.md)#([a-z0-9-]+)")
ENV_NAME_RE = re.compile(r"^`(TXSTORE_[A-Z0-9_]+)`$")
EXCEPTION_TOKEN_RE = re.compile(r"`([A-Z][A-Za-z0-9]*Exception)`")
EXCEPTION_CELL_RE = re.compile(r"^`([A-Z][A-Za-z0-9]*Exception)`$")

Lines = list[tuple[int, str]]
Table = tuple[int, list[str], list[tuple[int, list[str]]]]


class Fences(NamedTuple):
    """A document split by its fenced code blocks."""

    prose: Lines
    """``(lineno, line)`` outside fenced blocks."""
    openers: Lines
    """``(lineno, info string)`` of each outermost fence opener."""
    unclosed: int | None
    """Line where a fence opened and never closed."""


def _slug(heading: str) -> str:
    """GitHub-style heading slug: lowercase, drop punctuation, each space to one hyphen."""
    text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s", "-", text).strip("-")


def _root_markdown() -> dict[str, str]:
    """Root ``*.md`` documents keyed by file name."""
    return {p.name: p.read_text() for p in sorted(REPO_ROOT.glob("*.md"))}


def _docs_markdown() -> dict[str, str]:
    """Non-canonical ``docs/**/*.md``, keyed by repo-relative path."""
    return {
        p.relative_to(REPO_ROOT).as_posix(): p.read_text()
        for p in sorted((REPO_ROOT / "docs").rglob("*.md"))
    }


def _scan_fences(text: str) -> Fences:
    """Prose lines, outermost fence openers, and the line where an unclosed fence opened."""
    lines: Lines = []
    openers: Lines = []
    fence: str | None = None
    opened = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        match = FENCE_RE.match(line)
        if match is None:
            if fence is None:
                lines.append((lineno, line))
            continue
        token = match.group(1)
        if fence is None:
            fence, opened = token, lineno
            openers.append((lineno, match.group(2)))
        elif token[0] == fence[0] and len(token) >= len(fence):
            fence = None
    return Fences(lines, openers, opened if fence is not None else None)


def _prose_lines(text: str) -> Lines:
    """``(lineno, line)`` pairs outside fenced code blocks."""
    return _scan_fences(text).prose


def _target(source: str, target: str, known: Container[str] = ()) -> str | None:
    """Repo-relative path of a link target in ``source``; ``None`` when it does not exist."""
    path = (REPO_ROOT / source).parent / target
    try:
        name = path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:  # resolves outside the repository
        return None
    return name if name in known or (REPO_ROOT / name).exists() else None


def _link_targets(text: str) -> list[tuple[int, str]]:
    """``(lineno, target)`` of each relative link outside fenced blocks; ``]()`` is skipped."""
    return [
        (lineno, match.group(1))
        for lineno, line in _prose_lines(text)
        for match in LINK_RE.finditer(line)
        if match.group(1) and not EXTERNAL_RE.match(match.group(1))
    ]


def _image_targets(text: str) -> list[str]:
    """Target of each relative inline image shown: not in a fence, comment, or code span."""
    prose = "\n".join(line for _, line in _prose_lines(text))
    shown = CODE_SPAN_RE.sub("", HTML_COMMENT_RE.sub("", prose))
    return [
        target
        for match in IMAGE_RE.finditer(shown)
        if not EXTERNAL_RE.match(target := match.group(1))
    ]


def _anchors(text: str) -> set[str]:
    """Heading slugs of a document, ignoring fenced code blocks; duplicates get ``-N`` suffixes."""
    seen: Counter[str] = Counter()
    anchors: set[str] = set()
    for _, line in _prose_lines(text):
        match = HEADING_RE.match(line)
        if match is None:
            continue
        slug = _slug(match.group(2))
        anchors.add(slug if seen[slug] == 0 else f"{slug}-{seen[slug]}")
        seen[slug] += 1
    return anchors


def _cells(row: str) -> list[str]:
    r"""Cells of a ``| a | b |`` table row, stripped; ``\|`` does not split."""
    parts = [c.strip() for c in CELL_SPLIT_RE.split(row.strip())]
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


def _tables(text: str) -> list[Table]:
    """Markdown tables as ``(header lineno, header cells, [(lineno, cells), ...])``."""
    tables: list[Table] = []
    block: Lines = []

    def flush() -> None:
        if len(block) >= 2 and TABLE_SEP_RE.match(block[1][1].strip()):
            rows = [(n, _cells(row)) for n, row in block[2:]]
            tables.append((block[0][0], _cells(block[0][1]), rows))
        block.clear()

    for lineno, line in _prose_lines(text):
        if line.strip().startswith("|"):
            block.append((lineno, line))
        else:
            flush()
    flush()
    return tables


def _sources() -> list[Path]:
    return sorted((REPO_ROOT / PACKAGE).rglob("*.py"))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def check_fences(docs: dict[str, str]) -> list[str]:
    """No document leaves a code fence open: everything after one is invisible to every check."""
    errors: list[str] = []
    for name, text in docs.items():
        opened = _scan_fences(text).unclosed
        if opened is not None:
            errors.append(
                f"{name}:{opened}: unterminated code fence hides the rest of the document"
            )
    return errors


def check_anchors(docs: dict[str, str]) -> list[str]:
    """Every link target exists and every ``.md`` ``#anchor`` resolves to a heading."""
    anchors = {name: _anchors(text) for name, text in docs.items()}
    errors: list[str] = []
    for name, text in docs.items():
        for lineno, target in _link_targets(text):
            path, _, anchor = target.partition("#")
            resolved = name if path == "" else _target(name, path, anchors)
            if resolved is None:
                errors.append(f"{name}:{lineno}: link to missing file {path}")
                continue
            if resolved.endswith(".md") and resolved not in anchors:
                anchors[resolved] = _anchors((REPO_ROOT / resolved).read_text())
            if anchor and resolved.endswith(".md") and anchor not in anchors[resolved]:
                errors.append(f"{name}:{lineno}: broken anchor {resolved}#{anchor}")
    return errors


def check_diagram_fences(docs: dict[str, str]) -> list[str]:
    """No document opens a PlantUML fence; a fence nested in another block is an example."""
    return [
        f"{name}:{lineno}: {info} fence; move it to docs/<dir>/diagrams/*.puml"
        for name, text in docs.items()
        for lineno, info in _scan_fences(text).openers
        if info.lower() in DIAGRAM_FENCES
    ]


def check_diagram_embeds(docs: dict[str, str]) -> list[str]:
    """Every rendered ``docs/**/diagrams/*.svg`` is embedded as a Markdown image in a document."""
    embedded = {
        _target(name, path, docs)
        for name, text in docs.items()
        for target in _image_targets(text)
        if (path := target.partition("#")[0])
    }
    return [
        f"{relative}: diagram is embedded in no document"
        for svg in sorted((REPO_ROOT / "docs").rglob("diagrams/*.svg"))
        if (relative := svg.relative_to(REPO_ROOT).as_posix()) not in embedded
    ]


def parse_env_names(sources: list[Path]) -> dict[str, str]:
    """``TXSTORE_*`` literals passed to an ``_env`` helper → first ``file:line`` reading them."""
    names: dict[str, str] = {}
    for path in sources:
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            arg = node.args[0]
            if (
                called in ENV_HELPERS
                and isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and arg.value.startswith("TXSTORE_")
            ):
                where = f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}"
                names.setdefault(arg.value, where)
    return names


def check_config(docs: dict[str, str], code_names: dict[str, str]) -> list[str]:
    """``CONFIG.md`` variable rows equal the ``TXSTORE_*`` names the code reads."""
    documented: dict[str, int] = {}
    for _, header, rows in _tables(docs["CONFIG.md"]):
        if not header or header[0] != "Variable":
            continue
        for lineno, cells in rows:
            match = ENV_NAME_RE.match(cells[0]) if cells else None
            if match is None:
                documented[f"<unparsable row {lineno}>"] = lineno
                continue
            documented[match.group(1)] = lineno
    errors = [
        f"CONFIG.md:{lineno}: {name} is not read by the code"
        for name, lineno in sorted(documented.items())
        if name not in code_names
    ]
    errors += [
        f"CONFIG.md: {name} ({where}) has no Variable row"
        for name, where in sorted(code_names.items())
        if name not in documented
    ]
    return errors


def parse_exceptions(sources: list[Path]) -> set[str]:
    """Names of every ``class *Exception`` defined in the source."""
    names = {
        node.name
        for path in sources
        for node in ast.walk(_parse(path))
        if isinstance(node, ast.ClassDef) and node.name.endswith("Exception")
    }
    if not names:
        raise SystemExit(f"check_docs: parsed no exception classes under {PACKAGE}")
    return names


def check_exceptions(docs: dict[str, str], defined: set[str]) -> list[str]:
    """The ``EXCEPTIONS.md`` table lists every class; documents name only defined classes."""
    errors: list[str] = []
    rows: dict[str, int] = {}
    for _, header, table_rows in _tables(docs["EXCEPTIONS.md"]):
        if not header or header[0] != "Exception":
            continue
        for lineno, cells in table_rows:
            match = EXCEPTION_CELL_RE.match(cells[0]) if cells else None
            if match is not None:
                rows[match.group(1)] = lineno
    if not rows:
        errors.append("EXCEPTIONS.md: no table with header `Exception`")
    errors += [f"EXCEPTIONS.md: missing exception {n}" for n in sorted(defined - set(rows))]
    errors += [
        f"EXCEPTIONS.md:{lineno}: {name} is not defined in {PACKAGE.as_posix()}"
        for name, lineno in sorted(rows.items())
        if name not in defined
    ]
    for name, doc in docs.items():
        for lineno, line in _prose_lines(doc):
            for token in EXCEPTION_TOKEN_RE.findall(line):
                if token not in defined:
                    errors.append(f"{name}:{lineno}: unknown exception name {token}")
    return errors


def check_tables(docs: dict[str, str]) -> list[str]:
    """Every table row has as many cells as its header; an escaped ``\\|`` stays in its cell."""
    return [
        f"{name}:{lineno}: row has {len(cells)} cells, header at line {header_lineno} has "
        f"{len(header)}"
        for name, text in docs.items()
        for header_lineno, header, rows in _tables(text)
        for lineno, cells in rows
        if len(cells) != len(header)
    ]


def check_line_length(docs: dict[str, str]) -> list[str]:
    """Prose stays within ``LINE_LIMIT`` columns; tables, fences, URLs, bare tokens are exempt."""
    errors: list[str] = []
    for name, text in docs.items():
        for lineno, line in _prose_lines(text):
            stripped = line.strip()
            if len(line) <= LINE_LIMIT or stripped.startswith("|") or URL_RE.search(line):
                continue
            if " " not in stripped:
                continue
            errors.append(f"{name}:{lineno}: line is {len(line)} columns (limit {LINE_LIMIT})")
    return errors


def check_todos(docs: dict[str, str]) -> list[str]:
    """Root contracts carry no TODO-style markers outside inline code."""
    return [
        f"{name}:{lineno}: TODO marker in a contract"
        for name, text in docs.items()
        if name not in TODO_EXEMPT
        for lineno, line in _prose_lines(text)
        if TODO_RE.search(INLINE_CODE_RE.sub("", line))
    ]


def check_code_references(docs: dict[str, str]) -> list[str]:
    """Every ``ROOT.md#anchor`` cited in source or test code resolves to a root heading."""
    anchors = {name: _anchors(text) for name, text in docs.items()}
    errors: list[str] = []
    for root in (REPO_ROOT / PACKAGE, REPO_ROOT / "tests"):
        for path in sorted(root.rglob("*.py")):
            where = path.relative_to(REPO_ROOT).as_posix()
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                for doc, anchor in CODE_REF_RE.findall(line):
                    if doc not in anchors:
                        errors.append(f"{where}:{lineno}: cites missing document {doc}")
                    elif anchor not in anchors[doc]:
                        errors.append(f"{where}:{lineno}: broken anchor {doc}#{anchor}")
    return errors


def check_docs_index(docs: dict[str, str]) -> list[str]:
    """Every ``docs/<dir>/`` with pages has a ``README.md`` linking each sibling page."""
    errors: list[str] = []
    by_dir: dict[str, list[str]] = {}
    for name in docs:
        parts = name.split("/")
        if len(parts) == 3 and parts[0] == "docs":
            by_dir.setdefault(parts[1], []).append(parts[2])
    for directory, pages in sorted(by_dir.items()):
        index = f"docs/{directory}/README.md"
        if index not in docs:
            errors.append(f"docs/{directory}/: no README.md index")
            continue
        linked = {
            m.group(1).partition("#")[0]
            for _, line in _prose_lines(docs[index])
            for m in LINK_RE.finditer(line)
        }
        errors += [
            f"{index}: {page} is not linked from the index"
            for page in sorted(pages)
            if page != "README.md" and page not in linked
        ]
    return errors


def check_version(docs: dict[str, str]) -> list[str]:
    """The ``README.md`` "Current version" line carries ``[project].version``."""
    expected = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    readme = README_VERSION_RE.search(docs.get("README.md", ""))
    found = readme.group(1) if readme else None
    if found == expected:
        return []
    return [f"README.md: version {found or 'missing'}, pyproject.toml has {expected}"]


def check_package_sources() -> list[str]:
    """Dependencies resolve from public PyPI only."""
    errors: list[str] = []
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    tool = pyproject.get("tool", {})
    if tool.get("uv", {}).get("index"):
        errors.append("pyproject.toml: declares [[tool.uv.index]]; packages resolve from PyPI")
    if tool.get("poetry", {}).get("source"):
        errors.append("pyproject.toml: declares [[tool.poetry.source]]; packages resolve from PyPI")
    lock = REPO_ROOT / "uv.lock"
    if lock.is_file():
        for package in tomllib.loads(lock.read_text()).get("package", []):
            registry = package.get("source", {}).get("registry")
            if registry is not None and registry != PYPI_REGISTRY:
                errors.append(f"uv.lock: {package.get('name')} resolves from {registry}")
    return errors


def main() -> int:
    """Run every check; print failures; exit 1 on any failure."""
    docs = _root_markdown()
    for required in ("CONFIG.md", "EXCEPTIONS.md"):
        if required not in docs:
            raise SystemExit(f"check_docs: {required} not found in {REPO_ROOT}")
    all_docs = {**docs, **_docs_markdown()}
    sources = _sources()
    errors = [
        *check_fences(all_docs),
        *check_anchors(all_docs),
        *check_diagram_fences(all_docs),
        *check_diagram_embeds(all_docs),
        *check_config(docs, parse_env_names(sources)),
        *check_exceptions(all_docs, parse_exceptions(sources)),
        *check_line_length(all_docs),
        *check_tables(all_docs),
        *check_todos(docs),
        *check_code_references(docs),
        *check_docs_index(all_docs),
        *check_version(docs),
        *check_package_sources(),
    ]
    for error in errors:
        print(error)
    if errors:
        print(f"check_docs: {len(errors)} problem(s)")
        return 1
    print(f"check_docs: OK ({len(all_docs)} documents)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
