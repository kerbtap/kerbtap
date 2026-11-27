"""``governor init`` installs a git pre-commit hook that runs the commit gates (GHK-001..005)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from kerbtap import git_hook
from tests.repo_factory import git, init_repo

TOOL_DIR = Path(__file__).resolve().parents[1]
SOURCE_REPO = TOOL_DIR.parent


def _governor(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(TOOL_DIR)}
    return subprocess.run(
        [sys.executable, "-m", "kerbtap", *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _installed_repo(tmp_path: Path) -> Path:
    repo = init_repo(tmp_path / "carbonLedger")
    (repo / "docs").mkdir()
    (repo / "docs/readme.md").write_text("docs\n")
    done = _governor("init", str(repo), "--no-tests", "--source", str(SOURCE_REPO))
    assert done.returncode == 0, done.stderr
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "install", env_extra={"CARBONLEDGER_REVIEWED": "1", "CARBONLEDGER_REVIEWED_REASON": "t", "CARBONLEDGER_DOC_REVIEWED": "1", "CARBONLEDGER_DOC_REVIEWED_REASON": "t"})
    return repo


def _stage_market(repo: Path) -> None:
    (repo / "docs/market.md").write_text("market\n")
    git(repo, "add", "docs/market.md")


def test_plain_commit_of_guarded_file_is_refused(tmp_path: Path) -> None:
    repo = _installed_repo(tmp_path)
    _stage_market(repo)
    head = git(repo, "rev-parse", "HEAD").stdout
    done = git(repo, "commit", "-m", "wip", check=False)
    assert done.returncode != 0
    assert "CARBONLEDGER_REVIEWED" in done.stderr
    assert git(repo, "rev-parse", "HEAD").stdout == head


def test_env_bypass_clears_its_gate(tmp_path: Path) -> None:
    repo = _installed_repo(tmp_path)
    _stage_market(repo)
    head = git(repo, "rev-parse", "HEAD").stdout
    done = git(repo, "commit", "-m", "wip", check=False, env_extra={"CARBONLEDGER_REVIEWED": "1", "CARBONLEDGER_REVIEWED_REASON": "t", "CARBONLEDGER_DOC_REVIEWED": "1", "CARBONLEDGER_DOC_REVIEWED_REASON": "t"})
    assert done.returncode == 0, done.stderr
    assert git(repo, "rev-parse", "HEAD").stdout != head


def test_bypass_without_reason_is_refused(tmp_path: Path) -> None:
    repo = _installed_repo(tmp_path)
    _stage_market(repo)
    head = git(repo, "rev-parse", "HEAD").stdout
    done = git(repo, "commit", "-m", "wip", check=False, env_extra={"CARBONLEDGER_REVIEWED": "1"})
    assert done.returncode != 0
    assert "bypass-no-reason" in done.stderr
    assert git(repo, "rev-parse", "HEAD").stdout == head


def _user_hook(path: Path, sentinel: Path) -> None:
    path.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    path.chmod(0o755)


def test_existing_hook_still_runs(tmp_path: Path) -> None:
    for hooks_path in (None, ".githooks"):
        repo = init_repo(tmp_path / f"r{hooks_path or 'default'}" / "carbonLedger")
        (repo / "docs").mkdir()
        (repo / "docs/readme.md").write_text("docs\n")
        sentinel = tmp_path / f"sentinel{hooks_path or 'default'}"
        if hooks_path:
            git(repo, "config", "core.hooksPath", hooks_path)
            (repo / hooks_path).mkdir()
        _user_hook(repo / (hooks_path or ".git/hooks") / "pre-commit", sentinel)
        assert _governor("init", str(repo), "--no-tests", "--source", str(SOURCE_REPO)).returncode == 0
        (repo / "notes.txt").write_text("n\n")
        git(repo, "add", "notes.txt")
        done = git(repo, "commit", "-m", "notes", check=False)
        assert done.returncode == 0, done.stderr
        assert sentinel.exists()


def test_taken_chained_name_is_refused(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "carbonLedger")
    hooks = repo / ".git/hooks"
    _user_hook(hooks / "pre-commit", tmp_path / "s")
    (hooks / "pre-commit.governor-chained").write_text("taken\n")
    before = [(hooks / n).read_bytes() for n in ("pre-commit", "pre-commit.governor-chained")]
    done = _governor("git-hook", "install", str(repo))
    assert done.returncode == 2
    assert "pre-commit.governor-chained" in done.stderr and "pre-commit" in done.stderr
    assert [(hooks / n).read_bytes() for n in ("pre-commit", "pre-commit.governor-chained")] == before


def test_status_and_remove_restore_the_chained_hook(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "carbonLedger")
    hooks = repo / ".git/hooks"
    _user_hook(hooks / "pre-commit", tmp_path / "s")
    original = (hooks / "pre-commit").read_bytes()
    mode = (hooks / "pre-commit").stat().st_mode
    assert _governor("git-hook", "install", str(repo)).returncode == 0
    first = _governor("git-hook", "status", str(repo))
    assert first.returncode == 0 and "installed" in first.stdout
    assert _governor("git-hook", "remove", str(repo)).returncode == 0
    assert (hooks / "pre-commit").read_bytes() == original
    assert (hooks / "pre-commit").stat().st_mode == mode
    assert not (hooks / "pre-commit.governor-chained").exists()
    second = _governor("git-hook", "status", str(repo))
    assert second.returncode == 1 and "absent" in second.stdout


def test_doc_gate_warns_and_does_not_refuse(tmp_path: Path) -> None:
    repo = _installed_repo(tmp_path)
    (repo / "CLAUDE.md").write_text("changed\n")
    git(repo, "add", "CLAUDE.md")
    done = git(repo, "commit", "-m", "doc", check=False)
    assert done.returncode == 0, done.stderr
    assert "KERBTAP SHADOW (precommit_doc_consistency_guard would deny)" in done.stderr


def test_adapter_passes_shadow_warning(tmp_path: Path) -> None:
    repo = _installed_repo(tmp_path)
    _stage_market(repo)
    (repo / ".governor").mkdir(exist_ok=True)
    (repo / ".governor/gate-mode.json").write_text('{"precommit_review_guard": "shadow"}\n')
    head = git(repo, "rev-parse", "HEAD").stdout
    done = git(repo, "commit", "-m", "wip", check=False)
    assert done.returncode == 0, done.stderr
    assert "KERBTAP SHADOW (precommit_review_guard would deny)" in done.stderr
    assert git(repo, "rev-parse", "HEAD").stdout != head
    log = (repo / ".governor/gate-log/events.jsonl").read_text()
    assert '"decision": "shadow-deny"' in log and '"decision": "deny"' not in log


def test_symlinked_hook_chains_and_still_runs(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "carbonLedger")
    sentinel = tmp_path / "sentinel"
    target = tmp_path / "shared-hook"
    _user_hook(target, sentinel)
    (repo / ".git/hooks/pre-commit").symlink_to(target)
    assert _governor("git-hook", "install", str(repo)).returncode == 0
    (repo / "notes.txt").write_text("n\n")
    git(repo, "add", "notes.txt")
    assert git(repo, "commit", "-m", "notes", check=False).returncode == 0
    assert sentinel.exists()


def test_failed_write_leaves_the_old_hook_active(tmp_path: Path, monkeypatch) -> None:
    repo = init_repo(tmp_path / "carbonLedger")
    hook = repo / ".git/hooks/pre-commit"
    _user_hook(hook, tmp_path / "s")
    original = hook.read_bytes()

    def fail(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", fail)
    try:
        git_hook.install(repo)
        raised = False
    except git_hook.GitHookError as exc:
        raised = str(hook) in str(exc)
    monkeypatch.undo()
    assert raised
    assert hook.read_bytes() == original
    assert not (repo / ".git/hooks/pre-commit.governor-chained").exists()
