"""Tests that the pack narrative never asserts controls it did not detect.

Three AI states must be distinguished precisely:
  1. governed             — governance scaffold and/or dogfood log present.
  2. tooling-ungoverned   — a .claude/ dir exists (e.g. skills) but no governance/dogfood.
  3. none                 — no .claude/ dir, no dogfood, no AI co-author trailers.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from kerbtap.assemble import build_bundle
from kerbtap.build import render_pack


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def _init(path: Path, name: str = "Dev", email: str = "dev@example.com") -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)
    _git(path, "config", "user.name", name)
    _git(path, "config", "user.email", email)


def _commit(path: Path, msg: str) -> None:
    _git(path, "add", "-A")
    _git(path, "commit", "-m", msg)


def _plain_repo(path: Path) -> None:
    """State 3: no .claude, no logs, no AI trailers."""
    _init(path, "Human Dev", "human@example.com")
    (path / "src.py").write_text("print('hi')\n", encoding="utf-8")
    _commit(path, "Add feature")
    (path / "src.py").write_text("print('hello')\n", encoding="utf-8")
    _commit(path, "Improve greeting")


def _tooling_repo(path: Path) -> None:
    """State 2: a .claude/skills/ config only — no governance scaffold, no logs."""
    _init(path)
    (path / ".claude" / "skills" / "enricher").mkdir(parents=True)
    (path / ".claude" / "skills" / "enricher" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    _commit(path, "Add app")


def _governed_repo(path: Path) -> None:
    """State 1: governance scaffold + decision log + dogfood log."""
    _init(path, "Owner", "owner@example.com")
    (path / ".claude" / "hooks").mkdir(parents=True)
    (path / ".claude" / "hooks" / "precommit_review_guard.py").write_text("# gate\n", encoding="utf-8")
    (path / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    (path / "docs").mkdir()
    (path / "docs" / "decision-log.md").write_text("# Decision log\n", encoding="utf-8")
    (path / "docs" / "replication").mkdir(parents=True)
    (path / "docs" / "replication" / "dogfood-log.md").write_text(
        "# Dogfood Log\n\n**O4 — The self-attestation bypass leaves no evidence artifact.** More.\n",
        encoding="utf-8",
    )
    _commit(path, "Founding commit: scaffold")


# --- State 3: no AI evidence --------------------------------------------------------


class TestState3PlainRepo:
    def test_manifest_states_no_ai_tooling_or_orchestration(self, tmp_path: Path) -> None:
        repo = tmp_path / "plain"
        repo.mkdir()
        _plain_repo(repo)
        manifest = json.loads(render_pack(build_bundle(repo))["manifest.json"])
        att = manifest["repo_level_attestation"]
        assert att["ai_state"] == "none"
        assert att["ai_orchestrated"] is False
        assert "No evidence of AI tooling or orchestration found" in att["basis"]
        for change in manifest["changes"]:
            assert change["ai_assisted"]["value"] is False
            assert "No evidence of AI tooling or orchestration found" in change["ai_assisted"]["basis"]

    def test_report_asserts_no_absent_controls(self, tmp_path: Path) -> None:
        repo = tmp_path / "plain"
        repo.mkdir()
        _plain_repo(repo)
        rendered = render_pack(build_bundle(repo))
        report = rendered["report.md"]
        assert "a deny-mode pre-commit hook gates deliverable commits" not in report
        assert "no deny-mode pre-commit review gate found in this repository" in report
        assert "a git-versioned decision log records governance decisions" not in report
        assert "no git-versioned decision log or dogfood log found" in report
        assert "no AI tooling or orchestration evidence detected" in report
        assert "GOVERNOR_REVIEWED" not in rendered["remediation-plan.md"]
        assert "No dogfood or process-observation log was found" in rendered["exception-log.md"]


# --- State 2: AI tooling configured but ungoverned ----------------------------------


class TestState2ToolingUngoverned:
    def test_precise_state2_wording_not_state1_or_state3(self, tmp_path: Path) -> None:
        repo = tmp_path / "tooling"
        repo.mkdir()
        _tooling_repo(repo)
        rendered = render_pack(build_bundle(repo))
        manifest = json.loads(rendered["manifest.json"])
        att = manifest["repo_level_attestation"]

        assert att["ai_state"] == "ai-assisted-ungoverned"
        assert att["ai_orchestrated"] is False
        # Names the .claude/ tooling signal precisely.
        assert "`.claude/` Claude Code configuration (e.g. skills)" in att["basis"]
        assert "governed AI-orchestration of delivery is not evidenced" in att["basis"]
        # NOT a governed-orchestration claim, and NOT a "no .claude/" claim.
        assert "governed AI-orchestrated delivery is evidenced" not in att["basis"]
        assert "No evidence of AI tooling or orchestration found" not in att["basis"]
        # This tooling repo has no trailers, so the basis must NOT mention trailers.
        assert "AI co-author trailer" not in att["basis"]
        for change in manifest["changes"]:
            assert change["ai_assisted"]["value"] is False
            assert "AI coding tooling" in change["ai_assisted"]["basis"]

    def test_report_prints_the_state2_distinction(self, tmp_path: Path) -> None:
        repo = tmp_path / "tooling"
        repo.mkdir()
        _tooling_repo(repo)
        report = render_pack(build_bundle(repo))["report.md"]
        assert "AI coding tooling configured (`.claude/`)" in report
        assert "governed AI-orchestration of delivery is not evidenced" in report
        assert "no AI tooling or orchestration evidence detected" not in report


# --- State 1: governed AI-orchestration ---------------------------------------------


class TestState1Governed:
    def test_asserts_governed_orchestration_and_hook(self, tmp_path: Path) -> None:
        repo = tmp_path / "governed"
        repo.mkdir()
        _governed_repo(repo)
        rendered = render_pack(build_bundle(repo))
        manifest = json.loads(rendered["manifest.json"])
        att = manifest["repo_level_attestation"]
        report = rendered["report.md"]

        assert att["ai_state"] == "governed"
        assert att["ai_orchestrated"] is True
        assert "kerbtap governance scaffold" in att["basis"]
        assert "a deny-mode pre-commit hook gates deliverable commits" in report
        assert "a git-versioned decision log records governance decisions" in report
        assert all(c["ai_assisted"]["value"] is True for c in manifest["changes"])
