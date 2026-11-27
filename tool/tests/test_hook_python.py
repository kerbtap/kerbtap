"""The hook-interpreter check refuses an install that would leave its gates unable to start."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from kerbtap import hook_python
from kerbtap.hook_python import hook_python_problem
from kerbtap.init import InitError, init
from kerbtap.machine import MachineInstallError, install_machine
from tests.conftest import require_source

REPO_ROOT = Path(__file__).resolve().parents[2]


def _fake_python(tmp_path: Path, version: str) -> str:
    """A directory holding a ``python3`` that reports ``version``; returned as a PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "python3"
    exe.write_text(f"#!/bin/sh\necho {version}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(bin_dir)


def test_old_python_is_refused(tmp_path: Path) -> None:
    problem = hook_python_problem(_fake_python(tmp_path, "3.8"))
    assert problem is not None and "Python 3.8" in problem and "3.9+" in problem


def test_floor_python_is_accepted(tmp_path: Path) -> None:
    assert hook_python_problem(_fake_python(tmp_path, "3.9")) is None


def test_missing_python_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    problem = hook_python_problem(str(empty))
    assert problem is not None and "no python3 on PATH" in problem


def test_unreadable_version_is_refused(tmp_path: Path) -> None:
    assert hook_python_problem(_fake_python(tmp_path, "garbage")) is not None


def test_this_machine_passes() -> None:
    assert hook_python_problem() is None


@pytest.fixture
def old_python_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", _fake_python(tmp_path, "3.8") + os.pathsep + os.environ.get("PATH", ""))


def test_init_refuses_old_python_and_writes_nothing(tmp_path: Path, old_python_on_path: None) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(InitError, match="Python 3.8"):
        init(repo=repo, source_repo=REPO_ROOT, run_tests=False)
    assert not (repo / ".claude").exists()


def test_install_machine_refuses_old_python_and_writes_nothing(tmp_path: Path, old_python_on_path: None) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(MachineInstallError, match="Python 3.8"):
        install_machine(home=home)
    assert not (home / ".claude").exists()


def test_floor_is_the_one_ci_runs() -> None:
    require_source(".github/workflows/governance.yml")
    workflow = (REPO_ROOT / ".github/workflows/governance.yml").read_text(encoding="utf-8")
    floor = ".".join(map(str, hook_python.HOOK_PYTHON_FLOOR))
    assert f'python-version: "{floor}"' in workflow
