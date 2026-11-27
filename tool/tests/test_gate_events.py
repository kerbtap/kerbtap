"""GEL-001, GEL-005: the gate log records denies and bypasses, and the pack counts them."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kerbtap.assemble import build_bundle
from kerbtap.build import render_pack
from kerbtap.collect import github as github_mod
from tests.repo_factory import commit, git, init_repo

STARTER_HOOKS = Path(__file__).resolve().parents[2] / "starter" / ".claude" / "hooks"
GATE_LOG = Path(".kerbtap") / "gate-log" / "events.jsonl"
LIMIT_SENTENCE = (
    "The gate log is a local file. Any local user or agent can edit or delete it, "
    "so these counts are recorded, not verified."
)


def _event(gate: str, decision: str) -> str:
    return json.dumps({"gate": gate, "decision": decision, "reason_code": "x"})


def test_generate_counts_gate_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_mod, "gh_available", lambda: False)
    repo = init_repo(tmp_path / "r")
    commit(repo, "first")
    log = repo / GATE_LOG
    log.parent.mkdir(parents=True)
    lines = (
        [_event("precommit_review_guard", "deny")] * 3
        + [_event("precommit_review_guard", "ask")]
        + [_event("precommit_review_guard", "bypass")] * 2
        + [_event("precommit_doc_consistency_guard", "clear")]
        + [_event("precommit_review_guard", "shadow-deny")]
        + [_event("destructive_git_guard", "deny"), "{not json"]
    )
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    text = render_pack(build_bundle(repo))["exception-log.md"]

    assert "| precommit_review_guard | 3 | 1 | 2 | 0 | 1 |" in text
    assert "| destructive_git_guard | 1 | 0 | 0 | 0 | 0 |" in text
    assert "| precommit_doc_consistency_guard | 0 | 0 | 0 | 1 | 0 |" in text
    assert "9 event(s) parsed, 1 line(s) unparsed" in text
    assert LIMIT_SENTENCE in text
    from kerbtap.honesty import INTEGRITY_DISCLAIMER

    assert "tamper-evident" not in text.replace(INTEGRITY_DISCLAIMER, "")


def _hook(name: str, repo: Path, tool_input: dict, tool_name: str = "Bash") -> str:
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input, "cwd": str(repo)})
    return subprocess.run(
        [sys.executable, str(STARTER_HOOKS / name)], input=payload, text=True,
        capture_output=True, env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)},
    ).stdout


def _events(repo: Path) -> list[dict]:
    path = repo / GATE_LOG
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _staged(repo: Path, rel: str) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("x\n", encoding="utf-8")
    git(repo, "add", rel)


@pytest.mark.parametrize(
    ("hook", "staged", "bypass_env", "decisions", "marker"),
    [
        (
            "precommit_review_guard.py", "docs/deliverables/a.md", "PROJECT_REVIEWED",
            ["deny", "bypass"], '"permissionDecision": "deny"',
        ),
        # The doc gate warns, so each run logs a shadow-deny and a bypass is inert.
        (
            "precommit_doc_consistency_guard.py", "CLAUDE.md", "PROJECT_DOC_REVIEWED",
            ["shadow-deny", "shadow-deny"], "KERBTAP SHADOW",
        ),
    ],
)
def test_starter_copies_log_deny_and_bypass(
    tmp_path: Path, hook: str, staged: str, bypass_env: str, decisions: list[str], marker: str
) -> None:
    repo = init_repo(tmp_path / "r")
    _staged(repo, staged)

    assert marker in _hook(hook, repo, {"command": "git commit -m x"})
    bypass = f"{bypass_env}=1 {bypass_env}_REASON=why git commit -m x"
    out = _hook(hook, repo, {"command": bypass})

    gate = hook.removesuffix(".py")
    events = _events(repo)
    assert [(e["gate"], e["decision"]) for e in events] == [(gate, d) for d in decisions]
    if "bypass" in decisions:
        assert out == ""
        assert events[1]["bypass_form"] == "env-prefix"
        assert events[1]["bypass_reason"] == "why"
    else:
        assert marker in out


def test_refused_bypass_denies_and_log_head_is_a_sha(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    sha = commit(repo, "first")
    _staged(repo, "docs/deliverables/a.md")
    hook = "precommit_review_guard.py"

    blank = "PROJECT_REVIEWED=1 PROJECT_REVIEWED_REASON=' ' git commit -m x"
    out = _hook(hook, repo, {"command": blank})

    assert '"permissionDecision": "deny"' in out
    events = _events(repo)
    assert [e["reason_code"] for e in events] == ["bypass-no-reason"]
    assert events[0]["head"] == sha
    assert events[0]["branch"] == "main"


def test_read_gate_log_from_linked_worktree(tmp_path: Path) -> None:
    from kerbtap.collect.governance import read_gate_log

    repo = init_repo(tmp_path / "r")
    commit(repo, "first")
    (repo / GATE_LOG).parent.mkdir(parents=True)
    (repo / GATE_LOG).write_text(_event("destructive_git_guard", "deny") + "\n", encoding="utf-8")
    linked = tmp_path / "wt"
    git(repo, "worktree", "add", "-b", "feature", str(linked))

    log = read_gate_log(linked)

    assert [e["gate"] for e in log["events"]] == ["destructive_git_guard"]
