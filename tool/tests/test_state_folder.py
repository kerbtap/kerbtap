"""SF-001: one rule names the state folder, `.governor/` when it exists, `.kerbtap/` otherwise."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from kerbtap.cli import main
from kerbtap.review.record import record, verify
from kerbtap.state import NEW_NAME, OLD_NAME, state_name
from tests.repo_factory import git, init_repo

HOOKS = Path(__file__).resolve().parents[2] / "starter" / ".claude" / "hooks"
DOC = "CLAUDE.md"
GUARDED = "docs/deliverables/a.md"


def _write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8")


def _stage(repo: Path, rel: str) -> None:
    _write(repo, rel, "x\n")
    git(repo, "add", rel)


def _hook(name: str, repo: Path) -> str:
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": str(repo)}
    )
    return subprocess.run(
        [sys.executable, str(HOOKS / name)], input=payload, text=True, capture_output=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)},
    ).stdout


def _decisions(path: Path) -> list[str]:
    return [json.loads(x)["decision"] for x in path.read_text(encoding="utf-8").splitlines()]


def test_old_folder_still_clears_and_logs(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    _stage(repo, DOC)
    blob = git(repo, "hash-object", "--", DOC).stdout.strip()
    records = repo / OLD_NAME / "doc-pass" / "records.jsonl"
    records.parent.mkdir(parents=True)
    records.write_text(json.dumps({"ts": "t", "path": DOC, "blob": blob}) + "\n", encoding="utf-8")

    assert _hook("precommit_doc_consistency_guard.py", repo) == ""

    assert _decisions(repo / OLD_NAME / "gate-log" / "events.jsonl") == ["clear"]
    assert not (repo / NEW_NAME).exists()


def test_old_folder_review_record_and_gate_mode(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    (repo / OLD_NAME).mkdir()
    _write(repo, "v.json", "{}\n")
    verdict = repo / "verdict.json"
    verdict.write_text(
        json.dumps({"verdict": "PASS", "reviewer": "architect", "summary": "ok", "subject": ["v.json"]}),
        encoding="utf-8",
    )
    git(repo, "add", "v.json")
    artifact, trailers = record(verdict, "PASS", "architect", repo)
    assert artifact.parent == repo / OLD_NAME / "reviews"
    git(repo, "add", "-f", str(artifact.relative_to(repo)))
    git(repo, "commit", "-q", "-m", f"review\n\n{trailers}")
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()

    ok, message, _ = verify(repo, sha)
    assert ok, message
    assert not (repo / NEW_NAME).exists()

    (repo / OLD_NAME / "gate-mode.json").write_text('{"precommit_review_guard": "shadow"}\n')
    _stage(repo, GUARDED)
    out = _hook("precommit_review_guard.py", repo)
    assert "KERBTAP SHADOW (precommit_review_guard would deny)" in out
    assert '"permissionDecision": "deny"' not in out
    assert _decisions(repo / OLD_NAME / "gate-log" / "events.jsonl") == ["shadow-deny"]
    assert not (repo / NEW_NAME).exists()


def test_new_install_writes_kerbtap_folder(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    _write(repo, DOC, "one\n")

    assert main(["doc-pass", "record", "--repo", str(repo), DOC]) == 0

    assert (repo / NEW_NAME / "doc-pass" / "records.jsonl").is_file()
    assert not (repo / OLD_NAME).exists()


def test_old_folder_wins_when_both_exist(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    (repo / OLD_NAME).mkdir()
    (repo / NEW_NAME).mkdir()
    _write(repo, DOC, "one\n")

    assert state_name(repo) == OLD_NAME
    assert main(["doc-pass", "record", "--repo", str(repo), DOC]) == 0

    assert (repo / OLD_NAME / "doc-pass" / "records.jsonl").is_file()
    assert not (repo / NEW_NAME / "doc-pass").exists()


def test_old_commit_verifies_without_the_old_folder_in_the_checkout(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    (repo / OLD_NAME).mkdir()
    _write(repo, "v.json", "{}\n")
    verdict = repo / "verdict.json"
    verdict.write_text(
        json.dumps({"verdict": "PASS", "reviewer": "architect", "summary": "ok", "subject": ["v.json"]}),
        encoding="utf-8",
    )
    git(repo, "add", "v.json")
    artifact, trailers = record(verdict, "PASS", "architect", repo)
    git(repo, "add", "-f", str(artifact.relative_to(repo)))
    git(repo, "commit", "-q", "-m", f"review\n\n{trailers}")
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "rm", "-rq", "--cached", OLD_NAME)
    shutil.rmtree(repo / OLD_NAME)
    assert state_name(repo) == NEW_NAME

    ok, message, _ = verify(repo, sha)

    assert ok, message
