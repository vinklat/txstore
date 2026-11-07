"""`scripts/render_diagrams.py`: the stamp, each check on a synthetic tree, and the repository."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PATH = _REPO / "scripts" / "render_diagrams.py"
_SOURCE = "@startuml\nA -> B : hello\n@enduml\n"
_SVG = '<svg xmlns="http://www.w3.org/2000/svg"><text>hello</text></svg>'
_DIGEST = hashlib.sha256(_SOURCE.encode()).hexdigest()


def _load() -> types.ModuleType:
    name = "render_diagrams"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


render_diagrams = _load()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated repository root holding ``docs/dev/diagrams/``."""
    monkeypatch.setattr(render_diagrams, "REPO_ROOT", tmp_path)
    (tmp_path / "docs" / "dev" / "diagrams").mkdir(parents=True)
    return tmp_path


def _diagram(repo: Path, source: str = _SOURCE, rendered_from: str | None = None) -> Path:
    """Write a ``.puml`` and, unless ``rendered_from`` is empty, an SVG stamped from that text."""
    puml = repo / "docs" / "dev" / "diagrams" / "flow.puml"
    puml.write_text(source)
    if rendered_from != "":
        digest = hashlib.sha256((rendered_from or source).encode()).hexdigest()
        puml.with_suffix(".svg").write_text(render_diagrams.stamp(_SVG, digest))
    return puml


def test_repository_diagrams_current() -> None:
    assert render_diagrams.check() == []


# Stamp


def test_stamp_round_trips_and_keeps_xml_declaration_first() -> None:
    digest = "a" * 64
    stamped = render_diagrams.stamp(f'<?xml version="1.0"?>{_SVG}', digest)
    assert stamped.startswith('<?xml version="1.0"?>\n<!-- render_diagrams ')
    assert stamped.endswith(_SVG)
    assert render_diagrams.read_stamp(stamped) == (render_diagrams.PLANTUML_VERSION, digest)


def test_stamp_leads_an_svg_without_declaration() -> None:
    stamped = render_diagrams.stamp(_SVG, "b" * 64)
    assert stamped.startswith("<!-- render_diagrams ")
    assert stamped.endswith(_SVG)


def test_read_stamp_rejects_unstamped_svg() -> None:
    assert render_diagrams.read_stamp(_SVG) is None


# Check


def test_check_accepts_current_diagram(repo: Path) -> None:
    _diagram(repo)
    assert render_diagrams.check() == []


def test_check_reports_edited_source(repo: Path) -> None:
    _diagram(repo, source=_SOURCE.replace("hello", "bye"), rendered_from=_SOURCE)
    assert render_diagrams.check() == [
        "docs/dev/diagrams/flow.svg: stale; re-run scripts/render_diagrams.py"
    ]


@pytest.mark.parametrize(
    "svg",
    [
        pytest.param(_SVG, id="unstamped"),
        pytest.param(
            f"<!-- render_diagrams plantuml=0.0.1 sha256={_DIGEST} -->{_SVG}",
            id="other-plantuml-version",
        ),
    ],
)
def test_check_reports_svg_not_from_pinned_render(repo: Path, svg: str) -> None:
    puml = _diagram(repo)
    puml.with_suffix(".svg").write_text(svg)
    assert render_diagrams.check() == [
        "docs/dev/diagrams/flow.svg: stale; re-run scripts/render_diagrams.py"
    ]


def test_check_reports_missing_render(repo: Path) -> None:
    _diagram(repo, rendered_from="")
    assert render_diagrams.check() == [
        "docs/dev/diagrams/flow.puml: no rendered flow.svg; re-run scripts/render_diagrams.py"
    ]


def test_check_reports_orphan_render(repo: Path) -> None:
    _diagram(repo).unlink()
    assert render_diagrams.check() == ["docs/dev/diagrams/flow.svg: no source flow.puml"]


@pytest.mark.parametrize("fence", ["```plantuml", "~~~ puml", "````plantuml"])
def test_check_reports_inline_plantuml_fence(repo: Path, fence: str) -> None:
    (repo / "docs" / "dev" / "page.md").write_text(f"# Page\n\n{fence}\nA -> B\n```\n")
    (repo / "ROOT.md").write_text(f"{fence}\nA -> B\n```\n")
    hint = "inline plantuml fence; move it to docs/<dir>/diagrams/*.puml"
    assert render_diagrams.check() == [f"ROOT.md:1: {hint}", f"docs/dev/page.md:3: {hint}"]


def test_check_ignores_other_fences(repo: Path) -> None:
    (repo / "ROOT.md").write_text("```python\nx = 1\n```\n")
    assert render_diagrams.check() == []


# Render


def _fake_docker(
    monkeypatch: pytest.MonkeyPatch, returncode: int = 0, stdout: str = _SVG
) -> list[tuple[list[str], bytes]]:
    calls: list[tuple[list[str], bytes]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        stdin = kwargs["input"]
        assert isinstance(stdin, bytes)
        calls.append((args, stdin))
        return subprocess.CompletedProcess(args, returncode, stdout.encode(), b"syntax error")

    monkeypatch.setattr(render_diagrams.subprocess, "run", run)
    return calls


def test_render_writes_stamped_svg_from_stdin(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_docker(monkeypatch)
    puml = _diagram(repo, rendered_from="")
    assert render_diagrams.render() == 1
    [(args, stdin)] = calls
    assert args[:4] == ["docker", "run", "--rm", "-i"]
    assert render_diagrams.IMAGE in args
    assert stdin == _SOURCE.encode()
    assert render_diagrams.check() == []
    assert puml.with_suffix(".svg").read_text().endswith(_SVG)


def test_render_skips_current_unless_forced(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_docker(monkeypatch)
    _diagram(repo)
    assert render_diagrams.render() == 0
    assert calls == []
    assert render_diagrams.render(force=True) == 1


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [pytest.param(200, _SVG, id="nonzero-exit"), pytest.param(0, "", id="no-svg")],
)
def test_render_fails_without_svg(
    repo: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    _fake_docker(monkeypatch, returncode=returncode, stdout=stdout)
    puml = _diagram(repo, rendered_from="")
    with pytest.raises(SystemExit, match="flow.puml failed"):
        render_diagrams.render()
    assert not puml.with_suffix(".svg").exists()


def test_main_check_exit_codes(repo: Path) -> None:
    _diagram(repo)
    assert render_diagrams.main(["--check"]) == 0
    _diagram(repo, source="@startuml\n@enduml\n", rendered_from=_SOURCE)
    assert render_diagrams.main(["--check"]) == 1
