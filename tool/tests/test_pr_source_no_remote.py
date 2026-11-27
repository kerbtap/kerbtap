"""The pull-request source is present only when a GitHub repository resolves (EPS-001, EPS-002)."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from kerbtap.assemble import build_bundle
from kerbtap.build import render_pack
from kerbtap.collect import github as github_mod
from kerbtap.collect.governance import detect_source_systems
from tests.repo_factory import commit, init_repo

_ROW = "GitHub pull requests"


def _pr_row(repo: Path, **kwargs):
    systems = detect_source_systems(repo, **kwargs)
    return next(s for s in systems if s.name == _ROW)


@pytest.mark.skipif(shutil.which("gh") is None, reason="gh CLI not installed")
def test_repo_resolves_false_without_remote(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    commit(repo, "change")
    assert github_mod.repo_resolves(repo) is False


def test_assembled_bundle_no_repo_has_no_pr_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_mod, "gh_available", lambda: True)
    monkeypatch.setattr(github_mod, "repo_resolves", lambda *a, **k: False)
    monkeypatch.setattr(github_mod, "collect_pull_requests", lambda *a, **k: [])
    monkeypatch.setattr(github_mod, "collect_pr_details", lambda *a, **k: {})
    monkeypatch.setattr(
        github_mod, "collect_branch_protection", lambda *a, **k: {"state": "not-collected", "note": "n"}
    )
    repo = init_repo(tmp_path / "r")
    commit(repo, "change")
    bundle = build_bundle(repo)
    row = next(s for s in bundle.source_systems if s.name == _ROW)
    assert row.present is False
    assert bundle.gh_available is False
    assert "gh unavailable" not in "\n".join(render_pack(bundle).values())


def test_pr_row_present_when_repo_resolves(tmp_path: Path) -> None:
    row = _pr_row(init_repo(tmp_path / "r"), gh_available=True, github_repo=True)
    assert row.present is True
    assert row.evidence == "Pull-request records read via the `gh` CLI."


def test_pr_row_absent_when_no_repo_resolves(tmp_path: Path) -> None:
    row = _pr_row(init_repo(tmp_path / "r"), gh_available=True, github_repo=False)
    assert row.present is False
    assert "No GitHub repository resolves" in row.evidence
    assert "read via the `gh` CLI" not in row.evidence


def test_pr_row_absent_when_cli_unavailable(tmp_path: Path) -> None:
    row = _pr_row(init_repo(tmp_path / "r"), gh_available=False)
    assert row.present is False
    assert "gh CLI unavailable" in row.evidence


def _run_raising(exc: BaseException):
    def fake(*a, **k):
        raise exc

    return fake


def test_repo_resolves_true_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        github_mod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "{}", "")
    )
    assert github_mod.repo_resolves(tmp_path) is True


def test_repo_resolves_false_on_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        github_mod.subprocess, "run", _run_raising(subprocess.TimeoutExpired(["gh"], 30))
    )
    assert github_mod.repo_resolves(tmp_path) is False


def test_repo_resolves_false_when_gh_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_mod.subprocess, "run", _run_raising(FileNotFoundError("gh")))
    assert github_mod.repo_resolves(tmp_path) is False


def test_repo_resolves_log_omits_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "https://user:s3cr3t-token@github.com/org/repo.git"
    err = subprocess.CalledProcessError(1, ["gh"], output="", stderr=secret)
    monkeypatch.setattr(github_mod.subprocess, "run", _run_raising(err))
    with caplog.at_level(logging.DEBUG, logger=github_mod.logger.name):
        assert github_mod.repo_resolves(tmp_path) is False
    assert "s3cr3t-token" not in caplog.text
    assert caplog.text


def test_assembly_collects_pull_requests_when_repo_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(github_mod, "gh_available", lambda: True)
    monkeypatch.setattr(github_mod, "repo_resolves", lambda *a, **k: True)
    monkeypatch.setattr(github_mod, "collect_pull_requests", lambda p, *a, **k: calls.append(p) or [])
    monkeypatch.setattr(github_mod, "collect_pr_details", lambda *a, **k: {})
    monkeypatch.setattr(
        github_mod, "collect_branch_protection", lambda *a, **k: {"state": "not-collected", "note": "n"}
    )
    repo = init_repo(tmp_path / "r")
    commit(repo, "change")
    bundle = build_bundle(repo)
    assert calls
    assert bundle.gh_available is True
    assert next(s for s in bundle.source_systems if s.name == _ROW).present is True
