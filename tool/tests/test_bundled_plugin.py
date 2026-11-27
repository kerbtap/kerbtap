"""The wheel carries the plugin marketplace, and ``init`` installs the machine tier from it."""

from __future__ import annotations

import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest

from kerbtap import cli, machine

TOOL_DIR = Path(__file__).resolve().parents[1]


def _bundle(build_lib: Path) -> Path:
    spec = importlib.util.spec_from_file_location("governor_bundler", TOOL_DIR / "bundle_artifact.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.bundle_artifact(build_lib)


def test_the_bundle_carries_the_marketplace_and_its_plugins(tmp_path: Path) -> None:
    artifact = _bundle(tmp_path)
    market = json.loads((artifact / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    sources = {p["source"] for p in market["plugins"]}
    assert sources == {"./global", "./research"}
    for source in sources:
        assert (artifact / source).is_dir(), f"{source} is not in the bundle"


def test_the_default_marketplace_is_the_bundled_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = _bundle(tmp_path)
    monkeypatch.setattr(machine, "artifact_root", lambda _dir: artifact)
    assert machine.default_marketplace() == str(artifact)


def test_the_default_marketplace_falls_back_without_a_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine, "artifact_root", lambda _dir: None)
    assert machine.default_marketplace() == machine.DEFAULT_MARKETPLACE


def test_a_bundle_without_a_marketplace_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine, "artifact_root", lambda _dir: tmp_path)
    assert machine.default_marketplace() == machine.DEFAULT_MARKETPLACE


@pytest.fixture
def init_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Namespace]:
    """``init`` and the machine step replaced; the real ``claude`` command is never reached."""
    result = Namespace(
        repo=tmp_path, prefix="X", surfaces=Namespace(docs=True, code=False), installed=[], overwritten=[],
        unchanged=[], settings_backup=None, dropped=[], tests_failed=[], tests_run=0, git_hook="none",
        claude_md_current=True, claude_md_pending=None,
    )
    monkeypatch.setattr(cli, "init", lambda **_kw: result)
    calls: list[Namespace] = []
    monkeypatch.setattr(cli, "_cmd_install_machine", lambda args: calls.append(args) or 0)
    return calls


def _init_args(tmp_path: Path) -> Namespace:
    return Namespace(source=tmp_path, repo_path=tmp_path, prefix=None, force=False, no_tests=True,
                     with_code_review_graph=False, review_code=False)


def test_init_installs_the_machine_tier_when_claude_is_there(
    init_stub: list[Namespace], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/bin/claude")
    monkeypatch.setattr(cli, "_plugin_installed", lambda: False)
    assert cli._cmd_init(_init_args(tmp_path)) == 0
    assert len(init_stub) == 1 and init_stub[0].marketplace is None


def test_init_skips_the_machine_tier_when_the_plugin_is_installed(
    init_stub: list[Namespace], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/bin/claude")
    monkeypatch.setattr(cli, "_plugin_installed", lambda: True)
    assert cli._cmd_init(_init_args(tmp_path)) == 0
    assert init_stub == []


def test_init_says_what_to_run_when_claude_is_missing(
    init_stub: list[Namespace], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    assert cli._cmd_init(_init_args(tmp_path)) == 0
    assert init_stub == []
    assert "kerbtap install-machine" in capsys.readouterr().out
