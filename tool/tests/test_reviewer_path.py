"""The reviewer-agent source row finds the installed reviewer path."""

from __future__ import annotations

import subprocess
from pathlib import Path

from kerbtap.collect.governance import detect_source_systems
from kerbtap.init import init

SOURCE_REPO = Path(__file__).resolve().parents[2]
REVIEWER = ".claude/agents/reviewer/SKILL.md"
ARCHITECT = ".claude/agents/architect/SKILL.md"


def _row(repo: Path):
    systems = detect_source_systems(repo, gh_available=False)
    return next(s for s in systems if s.kind == "reviewer-agent")


def _with_file(tmp_path: Path, rel: str | None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    if rel:
        f = repo / rel
        f.parent.mkdir(parents=True)
        f.write_text("x", encoding="utf-8")
    return repo


def test_reviewer_row_finds_installed_reviewer(tmp_path: Path) -> None:
    row = _row(_with_file(tmp_path, REVIEWER))
    assert row.present is True
    assert REVIEWER in row.evidence
    assert ARCHITECT not in row.evidence


def test_reviewer_row_finds_architect(tmp_path: Path) -> None:
    row = _row(_with_file(tmp_path, ARCHITECT))
    assert row.present is True
    assert ARCHITECT in row.evidence
    assert REVIEWER not in row.evidence


def test_reviewer_row_absent_names_both_paths(tmp_path: Path) -> None:
    row = _row(_with_file(tmp_path, None))
    assert row.present is False
    assert REVIEWER in row.evidence and ARCHITECT in row.evidence


def test_reviewer_row_present_after_init(tmp_path: Path) -> None:
    repo = tmp_path / "carbonLedger"
    repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    row = _row(repo)
    assert row.present is True
    assert REVIEWER in row.evidence


def test_reviewer_row_both_present_names_reviewer(tmp_path: Path) -> None:
    repo = _with_file(tmp_path, REVIEWER)
    (repo / ARCHITECT).parent.mkdir(parents=True)
    (repo / ARCHITECT).write_text("x", encoding="utf-8")
    row = _row(repo)
    assert row.present is True
    assert REVIEWER in row.evidence
    assert ARCHITECT not in row.evidence


def test_reviewer_row_unreadable_path_is_not_determinable(tmp_path: Path, monkeypatch) -> None:
    repo = _with_file(tmp_path, None)
    real_exists = Path.exists

    def fake_exists(self: Path, *a, **k) -> bool:
        if str(self).endswith(REVIEWER):
            raise PermissionError("denied")
        return real_exists(self, *a, **k)

    monkeypatch.setattr(Path, "exists", fake_exists)
    row = _row(repo)
    assert row.present is None
    assert REVIEWER in row.evidence
