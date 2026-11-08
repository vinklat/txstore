"""`scripts/check_docs.py`: each check on a synthetic tree, and a clean run on this repository."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PATH = _REPO / "scripts" / "check_docs.py"


def _load() -> types.ModuleType:
    name = "check_docs"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


check_docs = _load()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated repository root: every path the checker reads resolves under ``tmp_path``."""
    monkeypatch.setattr(check_docs, "REPO_ROOT", tmp_path)
    return tmp_path


def _write(repo: Path, relative: str, text: str) -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_repository_passes() -> None:
    assert check_docs.main() == 0


# Links and anchors


def test_anchors_accept_existing_heading(repo: Path) -> None:
    _write(repo, "B.md", "# B\n\n## Some rule\n")
    docs = {"A.md": "[x](B.md#some-rule)\n"}
    assert check_docs.check_anchors(docs) == []


def test_anchors_report_missing_heading_and_file(repo: Path) -> None:
    _write(repo, "B.md", "# B\n")
    docs = {"A.md": "[x](B.md#nope)\n[y](C.md)\n"}
    assert check_docs.check_anchors(docs) == [
        "A.md:1: broken anchor B.md#nope",
        "A.md:2: link to missing file C.md",
    ]


def test_anchors_resolve_in_page_links_and_documents_under_check(repo: Path) -> None:
    docs = {
        "A.md": "# A\n\n## Rule\n[ok](#rule)\n[bad](#nope)\n[k](K.md#h)\n",
        "K.md": "# K\n\n## H\n",
    }
    assert check_docs.check_anchors(docs) == ["A.md:5: broken anchor A.md#nope"]


def test_anchors_ignore_fences_and_external_links(repo: Path) -> None:
    docs = {"A.md": "```\n[x](MISSING.md)\n```\n[w](https://example.com/x.md)\n"}
    assert check_docs.check_anchors(docs) == []


def test_unterminated_fence_is_reported() -> None:
    assert check_docs.check_fences({"A.md": "text\n```\ncode\n"}) == [
        "A.md:2: unterminated code fence hides the rest of the document"
    ]


# Diagrams


@pytest.mark.parametrize(
    ("opener", "closer", "info"),
    [
        ("```plantuml", "```", "plantuml"),
        ("~~~ puml", "~~~", "puml"),
        ("````PlantUML {x}", "````", "PlantUML"),
    ],
)
def test_diagram_fence_is_reported(opener: str, closer: str, info: str) -> None:
    docs = {"A.md": f"# A\n\n{opener}\nA -> B\n{closer}\n"}
    assert check_docs.check_diagram_fences(docs) == [
        f"A.md:3: {info} fence; move it to docs/<dir>/diagrams/*.puml"
    ]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "````markdown\n```plantuml\nA\n```\n````\n```python\nx = 1\n```\n", id="longer"
        ),
        pytest.param("~~~\n```\n```plantuml\nA\n```\n~~~\n", id="other-character"),
        pytest.param("````\n```\n```plantuml\nA\n```\n````\n", id="shorter-closer"),
    ],
)
def test_diagram_fence_nested_in_another_block_is_an_example(text: str) -> None:
    assert check_docs.check_diagram_fences({"A.md": text}) == []


def test_diagram_embeds_report_unlinked_svg(repo: Path) -> None:
    _write(repo, "docs/dev/diagrams/shown.svg", "<svg/>")
    _write(repo, "docs/dev/diagrams/hidden.svg", "<svg/>")
    _write(repo, "docs/dev/logo.svg", "<svg/>")
    docs = {
        "docs/dev/page.md": "![Shown](diagrams/shown.svg#x)\n```\n![x](diagrams/hidden.svg)\n```\n"
    }
    assert check_docs.check_diagram_embeds(docs) == [
        "docs/dev/diagrams/hidden.svg: diagram is embedded in no document"
    ]


@pytest.mark.parametrize(
    ("text", "embedded"),
    [
        pytest.param('![Flow](diagrams/flow.svg "Flow diagram")\n', True, id="double-title"),
        pytest.param("![Flow](diagrams/flow.svg 'Flow diagram')\n", True, id="single-title"),
        pytest.param("![Flow](diagrams/flow.svg (Flow diagram))\n", True, id="paren-title"),
        pytest.param("<!-- x -->\n![Flow](diagrams/flow.svg)\n", True, id="after-comment"),
        pytest.param("Run `x`, then:\n\n![Flow](diagrams/flow.svg)\n", True, id="after-code"),
        pytest.param("[Flow](diagrams/flow.svg)\n", False, id="plain-link"),
        pytest.param("![Flow](<diagrams/flow.svg>)\n", False, id="angle-brackets"),
        pytest.param("![Flow][flow]\n\n[flow]: diagrams/flow.svg\n", False, id="reference-style"),
        pytest.param("\\![Flow](diagrams/flow.svg)\n", False, id="escaped-bang"),
        pytest.param("<!-- ![Flow](diagrams/flow.svg) -->\n", False, id="html-comment"),
        pytest.param("<!--\n![Flow](diagrams/flow.svg)\n-->\n", False, id="html-comment-multiline"),
        pytest.param("Write `![Flow](diagrams/flow.svg)` to embed.\n", False, id="inline-code"),
        pytest.param("Write `` ![Flow](diagrams/flow.svg) `` here.\n", False, id="double-backtick"),
        pytest.param("Write `see\n![Flow](diagrams/flow.svg)` here.\n", False, id="multiline-code"),
    ],
)
def test_diagram_embeds_require_an_image(repo: Path, text: str, embedded: bool) -> None:
    _write(repo, "docs/dev/diagrams/flow.svg", "<svg/>")
    unembedded = ["docs/dev/diagrams/flow.svg: diagram is embedded in no document"]
    assert check_docs.check_diagram_embeds({"docs/dev/page.md": text}) == (
        [] if embedded else unembedded
    )


def test_code_references_resolve(repo: Path) -> None:
    # Citations are assembled so this file does not cite them itself.
    good, bad, missing = "STORAGE.md" + "#good", "STORAGE.md" + "#bad", "CONFIG.md" + "#any"
    _write(repo, "src/txstore/mod.py", f'"""See {good} and {bad}."""\n')
    _write(repo, "tests/test_x.py", f"# {missing}\n")
    docs = {"STORAGE.md": "# S\n\n## Good\n"}
    assert check_docs.check_code_references(docs) == [
        f"src/txstore/mod.py:1: broken anchor {bad}",
        "tests/test_x.py:1: cites missing document CONFIG.md",
    ]


# CONFIG.md parity


_CONFIG = """# C

| Variable | Read by | Type |
| -- | -- | -- |
| `TXSTORE_A` | x | y |
| `TXSTORE_DOC_ONLY` | x | y |
"""


def test_config_parity_both_directions(repo: Path) -> None:
    src = _write(
        repo,
        "src/txstore/f.py",
        'env_str("TXSTORE_A")\n_env.env_bool("TXSTORE_CODE_ONLY")\nenv_int(OTHER)\n',
    )
    names = check_docs.parse_env_names([src])
    assert names == {"TXSTORE_A": "src/txstore/f.py:1", "TXSTORE_CODE_ONLY": "src/txstore/f.py:2"}
    assert check_docs.check_config({"CONFIG.md": _CONFIG}, names) == [
        "CONFIG.md:6: TXSTORE_DOC_ONLY is not read by the code",
        "CONFIG.md: TXSTORE_CODE_ONLY (src/txstore/f.py:2) has no Variable row",
    ]


# EXCEPTIONS.md parity


_EXCEPTIONS = """# E

| Exception | Module | Raised when |
| -- | -- | -- |
| `AException` | m | w |
| `GoneException` | m | w |
"""


def test_exceptions_parity_and_unknown_names(repo: Path) -> None:
    src = _write(
        repo, "src/txstore/e.py", "class AException(Exception): ...\nclass BException: ...\n"
    )
    defined = check_docs.parse_exceptions([src])
    docs = {"EXCEPTIONS.md": _EXCEPTIONS, "X.md": "raises `CException`\n"}
    assert check_docs.check_exceptions(docs, defined) == [
        "EXCEPTIONS.md: missing exception BException",
        "EXCEPTIONS.md:6: GoneException is not defined in src/txstore",
        "EXCEPTIONS.md:6: unknown exception name GoneException",
        "X.md:1: unknown exception name CException",
    ]


# Writing rules


def test_line_length_exempts_tables_urls_and_tokens() -> None:
    long = "word " * 25
    docs = {
        "A.md": f"{long}\n| {long} |\n{long} https://example.com\n{'x' * 120}\n",
    }
    assert check_docs.check_line_length(docs) == ["A.md:1: line is 125 columns (limit 100)"]


def test_tables_rows_match_header_width() -> None:
    table = "| A | B | C |\n| -- | -- | -- |\n| `x \\| y` | b | c |\n| a | b | |\n| a | b |\n"
    assert check_docs.check_tables({"A.md": table}) == [
        "A.md:5: row has 2 cells, header at line 1 has 3"
    ]


def test_todos_only_in_roadmap() -> None:
    docs = {"A.md": "TODO fix\n`TODO` in code\n", "ROADMAP.md": "TODO allowed\n"}
    assert check_docs.check_todos(docs) == ["A.md:1: TODO marker in a contract"]


def test_docs_index_links_every_page() -> None:
    docs = {"docs/guides/README.md": "[a](a.md)\n", "docs/guides/a.md": "", "docs/guides/b.md": ""}
    assert check_docs.check_docs_index(docs) == [
        "docs/guides/README.md: b.md is not linked from the index"
    ]


# Release and packaging


def test_version_matches_pyproject(repo: Path) -> None:
    _write(repo, "pyproject.toml", '[project]\nversion = "1.2.3"\n')
    assert check_docs.check_version({"README.md": "Current version: **1.2.3**."}) == []
    assert check_docs.check_version({"README.md": "Current version: **1.2.2**."}) == [
        "README.md: version 1.2.2, pyproject.toml has 1.2.3"
    ]


def test_upgrade_note_required_for_current_version(repo: Path) -> None:
    _write(repo, "pyproject.toml", '[project]\nversion = "1.2.3"\n')
    setup = "# Setup\n\n## Upgrade notes\n\n- {note}: x.\n\n## Other\n\n- 1.2.3: elsewhere.\n"
    assert check_docs.check_upgrade_note({"SETUP.md": setup.format(note="1.2.3")}) == []
    assert check_docs.check_upgrade_note({"SETUP.md": setup.format(note="1.2.2")}) == [
        "SETUP.md: no upgrade note for 1.2.3"
    ]


def test_package_sources_refuse_custom_indexes(repo: Path) -> None:
    _write(
        repo,
        "pyproject.toml",
        '[project]\nversion = "1"\n[[tool.uv.index]]\nname = "x"\nurl = "https://x"\n',
    )
    _write(
        repo,
        "uv.lock",
        '[[package]]\nname = "a"\nsource = { registry = "https://pypi.org/simple" }\n'
        '[[package]]\nname = "b"\nsource = { registry = "https://mirror.example/simple" }\n',
    )
    assert check_docs.check_package_sources() == [
        "pyproject.toml: declares [[tool.uv.index]]; packages resolve from PyPI",
        "uv.lock: b resolves from https://mirror.example/simple",
    ]
