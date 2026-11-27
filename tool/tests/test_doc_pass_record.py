"""DPR-001 to DPR-005: `kerbtap doc-pass record` and the starter doc gate that reads it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kerbtap.cli import main
from tests.repo_factory import commit, git, init_repo

STARTER_HOOK = (
    Path(__file__).resolve().parents[2] / "starter" / ".claude" / "hooks"
    / "precommit_doc_consistency_guard.py"
)
RECORDS = Path(".kerbtap") / "doc-pass" / "records.jsonl"
GATE_LOG = Path(".kerbtap") / "gate-log" / "events.jsonl"
DOC = "CLAUDE.md"


def _blob(repo: Path, rel: str) -> str:
    return git(repo, "hash-object", "--", rel).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8")


def _staged(repo: Path, rel: str, text: str = "x\n") -> None:
    _write(repo, rel, text)
    git(repo, "add", rel)


def _record(repo: Path, *rels: str) -> None:
    assert main(["doc-pass", "record", "--repo", str(repo), *rels]) == 0


def _hook(repo: Path, command: str = "git commit -m x") -> str:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)})
    return subprocess.run(
        [sys.executable, str(STARTER_HOOK)], input=payload, text=True,
        capture_output=True, env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)},
    ).stdout


def _denied(out: str) -> bool:
    """The starter doc gate fired: it warns and never prints a deny decision."""
    return '"permissionDecision": "deny"' in out or "KERBTAP SHADOW" in out


def _decisions(repo: Path) -> list[str]:
    path = repo / GATE_LOG
    if not path.exists():
        return []
    return [json.loads(line)["decision"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_record_writes_blob_to_main_worktree(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    _write(repo, DOC, "one\n")
    git(repo, "add", DOC)
    commit(repo, "first")
    linked = tmp_path / "wt"
    git(repo, "worktree", "add", "-b", "feature", str(linked))
    _write(linked, DOC, "two\n")

    _record(linked, DOC)

    lines = [json.loads(x) for x in (repo / RECORDS).read_text(encoding="utf-8").splitlines()]
    assert [(x["path"], x["blob"]) for x in lines] == [(DOC, _blob(linked, DOC))]
    assert (repo / RECORDS).parent.joinpath(".gitignore").read_text(encoding="utf-8") == "*\n"
    assert not (linked / RECORDS).exists()


def test_record_refuses_bad_path(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    _write(repo, DOC, "one\n")
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x\n", encoding="utf-8")
    (repo / "link.md").symlink_to(outside)
    (repo / "linkdir").symlink_to(tmp_path, target_is_directory=True)
    _record(repo, DOC)
    before = (repo / RECORDS).read_text(encoding="utf-8")

    for bad in ("missing.md", "link.md", "linkdir/elsewhere.md", str(outside), "../elsewhere.md"):
        assert main(["doc-pass", "record", "--repo", str(repo), DOC, bad]) == 2, bad

    assert (repo / RECORDS).read_text(encoding="utf-8") == before


def test_starter_matching_record_clears(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    _staged(repo, DOC)
    _record(repo, DOC)

    assert _hook(repo) == ""
    assert _decisions(repo) == ["clear"]


def test_starter_unwritable_log_refuses_clear(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    _staged(repo, DOC)
    _record(repo, DOC)
    (repo / ".kerbtap" / "gate-log").write_text("not a directory", encoding="utf-8")

    assert _denied(_hook(repo))


def test_starter_no_record_denies(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    _staged(repo, DOC)

    assert _denied(_hook(repo))
    assert "clear" not in _decisions(repo)


def test_starter_stale_record_denies(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    _write(repo, DOC, "one\n")
    _record(repo, DOC)
    _staged(repo, DOC, "two\n")

    assert _denied(_hook(repo))
    assert "clear" not in _decisions(repo)


def test_starter_partial_record_denies(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    _staged(repo, DOC)
    _staged(repo, "AGENTS.md")
    _record(repo, DOC)

    out = _hook(repo)

    assert _denied(out)
    assert "AGENTS.md" in out
    assert "clear" not in _decisions(repo)


def test_starter_deny_names_record_first(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    _staged(repo, DOC)

    out = _hook(repo)

    assert _denied(out)
    assert "kerbtap doc-pass record" in out
    assert "_DOC_REVIEWED=1" not in out  # a bypass is inert, so the warning names none


def test_starter_staged_doc_worktree_differs_denies(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    _staged(repo, DOC, "one\n")
    _record(repo, DOC)
    _write(repo, DOC, "two\n")

    assert _denied(_hook(repo, "git commit -a -m x"))  # -a lands the working-tree blob, which has no record
    assert "clear" not in _decisions(repo)


def test_starter_unstaged_doc_uses_worktree_blob(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    _staged(repo, DOC, "one\n")
    commit(repo, "seed")
    _write(repo, DOC, "two\n")
    _write(repo, "other.txt", "x\n")
    command = "git commit -a -m x"
    _record(repo, DOC)
    _write(repo, DOC, "three\n")

    assert _denied(_hook(repo, command))
    assert "clear" not in _decisions(repo)

    _record(repo, DOC)

    assert _hook(repo, command) == ""
    assert _decisions(repo) == ["shadow-deny", "clear"]


@pytest.mark.parametrize("rel", ["a.md", "docs/a.md"])
def test_record_accepts_nested_and_flat_paths(tmp_path: Path, rel: str) -> None:
    repo = init_repo(tmp_path / "r")
    _write(repo, rel, "x\n")

    _record(repo, rel)

    assert json.loads((repo / RECORDS).read_text(encoding="utf-8"))["path"] == rel
