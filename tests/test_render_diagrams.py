"""`scripts/render_diagrams.py`: the stamp, each check, render with Docker faked, and the CLI."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PATH = _REPO / "scripts" / "render_diagrams.py"
# Non-ASCII on purpose: sources, renders, and PlantUML errors are UTF-8 text.
_SOURCE = "@startuml\nA -> B : café → hello\n@enduml\n"
_SVG = '<svg xmlns="http://www.w3.org/2000/svg"><text>café → 日本</text></svg>'
_STALE = "docs/dev/diagrams/flow.svg: stale; re-run scripts/render_diagrams.py"
_MISSING = "docs/dev/diagrams/flow.puml: no rendered flow.svg; re-run scripts/render_diagrams.py"


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


def _diagram(repo: Path, source: str = _SOURCE, name: str = "flow") -> Path:
    """Write ``docs/dev/diagrams/<name>.puml`` only."""
    puml = repo / "docs" / "dev" / "diagrams" / f"{name}.puml"
    puml.write_text(source, encoding="utf-8")
    return puml


def _render(puml: Path) -> Path:
    """Write the sibling SVG as a current render of the source as it is now."""
    svg = puml.with_suffix(".svg")
    svg.write_text(render_diagrams.stamp(_SVG, render_diagrams.digest(puml)), encoding="utf-8")
    return svg


def test_repository_diagrams_current() -> None:
    assert render_diagrams.check() == []


# Stamp


def test_stamp_is_the_first_line() -> None:
    recipe = "a" * 64
    assert render_diagrams.stamp(_SVG, recipe) == f"{render_diagrams.stamp_line(recipe)}\n{_SVG}"


# Check


def test_check_accepts_current_diagram(repo: Path) -> None:
    _render(_diagram(repo))
    assert render_diagrams.check() == []


def test_check_reports_edited_source(repo: Path) -> None:
    puml = _diagram(repo)
    _render(puml)
    puml.write_text(_SOURCE.replace("hello", "bye"), encoding="utf-8")
    assert render_diagrams.check() == [_STALE]


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        pytest.param("IMAGE", "plantuml/plantuml:1.2026.9@sha256:" + "0" * 64, id="image-digest"),
        pytest.param("RENDER_ARGS", ("-tsvg", "-pipe"), id="plantuml-arguments"),
    ],
)
def test_check_reports_changed_render_recipe(
    repo: Path, monkeypatch: pytest.MonkeyPatch, attribute: str, value: object
) -> None:
    _render(_diagram(repo))
    monkeypatch.setattr(render_diagrams, attribute, value)
    assert render_diagrams.check() == [_STALE]


@pytest.mark.parametrize("variant", ["unstamped", "other-plantuml-version", "stamp-not-first"])
def test_check_reports_svg_not_from_pinned_render(repo: Path, variant: str) -> None:
    puml = _diagram(repo)
    recipe = render_diagrams.digest(puml)
    svg = {
        "unstamped": _SVG,
        "other-plantuml-version": f"{render_diagrams.stamp_line(recipe, '0.0.1')}\n{_SVG}",
        "stamp-not-first": f"{_SVG}\n{render_diagrams.stamp_line(recipe)}\n",
    }[variant]
    puml.with_suffix(".svg").write_text(svg, encoding="utf-8")
    assert render_diagrams.check() == [_STALE]


def test_check_reports_missing_render(repo: Path) -> None:
    _diagram(repo)
    assert render_diagrams.check() == [_MISSING]


def test_check_reports_orphan_render(repo: Path) -> None:
    puml = _diagram(repo)
    _render(puml)
    puml.unlink()
    assert render_diagrams.check() == ["docs/dev/diagrams/flow.svg: no source flow.puml"]


@pytest.mark.parametrize("relative", ["docs/dev/flow.puml", "docs/dev/diagrams/sub/flow.puml"])
def test_check_reports_source_outside_diagrams(repo: Path, relative: str) -> None:
    (repo / relative).parent.mkdir(parents=True, exist_ok=True)
    (repo / relative).write_text(_SOURCE)
    assert render_diagrams.check() == [f"{relative}: diagram source outside a diagrams/ directory"]


def test_check_reports_url_in_source(repo: Path) -> None:
    _render(_diagram(repo, source="@startuml\n!include https://example.com/x.puml\n@enduml\n"))
    assert render_diagrams.check() == [
        "docs/dev/diagrams/flow.puml:2: URL in a diagram; renders are offline"
    ]


def test_check_ignores_svg_outside_diagrams(repo: Path) -> None:
    (repo / "docs" / "dev" / "logo.svg").write_text(_SVG, encoding="utf-8")
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
        return subprocess.CompletedProcess(args, returncode, stdout.encode(), b"syntax error \xff")

    monkeypatch.setattr(render_diagrams.subprocess, "run", run)
    return calls


def test_render_runs_pinned_image_offline(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_docker(monkeypatch)
    puml = _diagram(repo)
    assert render_diagrams.render() == 1
    [(args, stdin)] = calls
    assert args == [
        "docker",
        "run",
        "--rm",
        "-i",
        "--network=none",
        render_diagrams.IMAGE,
        *render_diagrams.RENDER_ARGS,
    ]
    assert stdin == _SOURCE.encode()
    assert render_diagrams.check() == []
    assert puml.with_suffix(".svg").read_text(encoding="utf-8").endswith(_SVG)


def test_render_skips_current_unless_forced(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_docker(monkeypatch)
    _render(_diagram(repo))
    assert render_diagrams.render() == 0
    assert calls == []
    assert render_diagrams.render(force=True) == 1


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        pytest.param(200, _SVG, id="nonzero-exit"),
        pytest.param(0, "", id="no-svg"),
        pytest.param(0, f'<?xml version="1.0"?>{_SVG}', id="xml-declaration"),
    ],
)
def test_render_fails_without_svg(
    repo: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    _fake_docker(monkeypatch, returncode=returncode, stdout=stdout)
    puml = _diagram(repo)
    with pytest.raises(SystemExit, match=r"flow\.puml failed(?s:.*)syntax error"):
        render_diagrams.render()
    assert not puml.with_suffix(".svg").exists()


def test_render_reports_missing_docker(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError(2, "No such file or directory", "docker")

    monkeypatch.setattr(render_diagrams.subprocess, "run", run)
    _diagram(repo)
    with pytest.raises(SystemExit, match="docker not found"):
        render_diagrams.render()


# Command line


@pytest.mark.parametrize(("argv", "renders"), [([], 0), (["--force"], 1)])
def test_main_renders(
    repo: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], renders: int
) -> None:
    calls = _fake_docker(monkeypatch)
    _render(_diagram(repo))
    assert render_diagrams.main(argv) == 0
    assert len(calls) == renders


def test_main_check_passes_current(repo: Path) -> None:
    _render(_diagram(repo))
    assert render_diagrams.main(["--check"]) == 0


def test_main_check_reports_problems(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _diagram(repo)
    assert render_diagrams.main(["--check"]) == 1
    assert _MISSING in capsys.readouterr().out.splitlines()
