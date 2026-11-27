"""CR-001 to CR-004 and CR-006: `governor change-record` and the scoped path it reads the commits with."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from kerbtap import change_record
from kerbtap.assemble import build_bundle, build_scoped_changes
from kerbtap.cli import main
from tests.repo_factory import commit, git, init_repo

BRANCH = "feat/proj-9-thing"
GATE_LOG = Path(".kerbtap") / "gate-log" / "events.jsonl"
REVIEWS = Path(".kerbtap") / "telemetry" / "reviews.jsonl"
VERBS = Path(".kerbtap") / "telemetry" / "ungated-commit-verbs.jsonl"


@pytest.fixture(autouse=True)
def no_gh(monkeypatch):
    monkeypatch.setattr("kerbtap.collect.github.gh_available", lambda: False)


def _repo(tmp_path: Path, subject: str = "PROJ-9: the thing", body: str = "") -> Path:
    repo = init_repo(tmp_path / "repo")
    commit(repo, "base")
    git(repo, "checkout", "-q", "-b", BRANCH)
    commit(repo, subject, body=body)
    return repo


def _write_lines(repo: Path, rel: Path, *lines: dict) -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("".join(json.dumps(ln) + "\n" for ln in lines), encoding="utf-8")


def _gate(decision: str, reason: str | None = None, branch: str = BRANCH) -> dict:
    return {"ts": "2026-11-17T10:00:00Z", "gate": "precommit_doc_consistency_guard", "decision": decision,
            "reason_code": "x", "bypass_form": None, "bypass_reason": reason, "expires": None,
            "branch": branch, "head": "a" * 40}


def _record(repo: Path, target: str = BRANCH) -> dict:
    return change_record.build_record(repo, target)


def test_branch_record_shows_trail(tmp_path, capsys):
    repo = _repo(tmp_path)
    _write_lines(repo, GATE_LOG, _gate("bypass", "doc-pass recorded"), _gate("clear"), _gate("deny", branch="other"))
    _write_lines(repo, REVIEWS, {"ts": "t", "change": "PROJ-9", "review": "plan", "passes": 2, "blockers": 1,
                                 "out_of_scope": 0})
    assert main(["change-record", BRANCH, "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    short = git(repo, "rev-parse", "--short=12", "HEAD").stdout.strip()
    assert f"`{short}` PROJ-9: the thing - landed via" in out
    assert "AI-assisted: " in out and "review: none" in out
    assert "reason: doc-pass recorded" in out
    assert "precommit_doc_consistency_guard` clear" in out
    assert "PROJ-9 plan review: 2 passes, 1 blockers" in out
    assert "deny" not in out  # the other branch's line is not this change's


def test_open_pr_shows_head_checks(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(change_record, "_gh_pr_view", lambda r, n: {
        "number": n, "title": "PROJ-9: t", "state": "OPEN", "headRefName": BRANCH, "baseRefName": "main",
        "commits": [{"oid": head}], "mergeCommit": None})
    seen = []
    monkeypatch.setattr(change_record.github, "_checks_block", lambda r, sha: seen.append(sha) or {
        "head_sha": sha, "outcome": "pass", "runs": [{"name": "ci"}]})
    rec = _record(repo, "7")
    assert seen == [head]
    assert rec["properties"]["verified"]["checks"]["outcome"] == "pass"
    assert "checks on head" in change_record.render_markdown(rec)


def test_no_trail_reads_no_local_record(tmp_path):
    repo = _repo(tmp_path)
    text = change_record.render_markdown(_record(repo))
    assert text.count("no local record") == 5  # policy-bound gate log, review passes, bypasses, shadow denies, ungated verbs
    assert "0 denies" not in text and "0 bypasses" not in text
    _write_lines(repo, GATE_LOG, _gate("clear"))
    text = change_record.render_markdown(_record(repo))
    assert "Bypasses:\n- none recorded" in text


def test_pr_title_key_selects_reviews(tmp_path, monkeypatch):
    repo = _repo(tmp_path, subject="no key here")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(change_record, "_gh_pr_view", lambda r, n: {
        "number": n, "title": "PROJ-9: t", "state": "OPEN", "headRefName": BRANCH, "baseRefName": "main",
        "commits": [{"oid": head}], "mergeCommit": None})
    _write_lines(repo, REVIEWS, {"ts": "t", "change": "PROJ-9", "review": "plan", "passes": 1, "blockers": 0,
                                 "out_of_scope": 0}, {"ts": "t", "change": "PROJ-8", "review": "plan", "passes": 1,
                                                      "blockers": 0, "out_of_scope": 0})
    rec = _record(repo, "7")
    assert [r["change"] for r in rec["local_trail"]["reviews"]["lines"]] == ["PROJ-9"]
    assert rec["properties"]["authorized"]["ticket_refs"] == ["PROJ-9"]


def test_no_ticket_reads_none_recorded(tmp_path):
    repo = _repo(tmp_path, subject="no key here")
    assert "## Authorized\n\n- none recorded" in change_record.render_markdown(_record(repo))


def test_bypass_reason_is_cleaned(tmp_path, capsys):
    repo = _repo(tmp_path)
    _write_lines(repo, GATE_LOG, _gate("bypass", "tamper-proof reason"))
    assert main(["change-record", BRANCH, "--repo", str(repo)]) == 0
    assert "tamper-proof" not in capsys.readouterr().out


def test_json_matches_markdown(tmp_path, capsys):
    repo = _repo(tmp_path)
    _write_lines(repo, GATE_LOG, _gate("bypass", "why"), _gate("clear"))
    _write_lines(repo, REVIEWS, {"ts": "t", "change": "PROJ-9", "review": "code", "passes": 1, "blockers": 0,
                                 "out_of_scope": 0})
    assert main(["change-record", BRANCH, "--repo", str(repo), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema"] == "governor.change-record/1"
    props = data["properties"]
    assert props["exception_controlled"]["bypasses"]["lines"][0]["reason"] == "why"
    assert [ln["decision"] for ln in props["policy_bound"]["gate_log"]["lines"]] == ["clear"]
    assert props["approved"]["review_passes"]["lines"][0]["review"] == "code"
    assert set(data["commits"][0]) == set(asdict(build_scoped_changes(repo, [data["commits"][0]["source_refs"]["commit_sha"]])[0][0]))
    assert data["local_trail"]["gate_log"]["state"] == "recorded"


def test_scoped_change_equals_pack_change(tmp_path):
    repo = init_repo(tmp_path / "repo")
    commit(repo, "base")
    git(repo, "checkout", "-q", "-b", BRANCH)
    hook = repo / ".claude" / "hooks" / "precommit_review_guard.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("# hook\n", encoding="utf-8")
    (repo / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "PROJ-9: gate surface")
    commit(repo, "PROJ-9: reviewed", body="Co-Authored-By: Claude <noreply@anthropic.com>\n\nReviewed: adversarial review PASS")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", BRANCH, "-m", f"Merge pull request #3 from x/{BRANCH}")
    commit(repo, "PROJ-10: direct")
    shas = git(repo, "rev-list", "main").stdout.split()
    packed = {c.change_id: c for c in build_bundle(repo).changes}
    scoped, unread = build_scoped_changes(repo, shas)
    assert unread == [] and len(scoped) == len(shas) == len(packed)
    for change in scoped:
        want, got = asdict(packed[change.change_id]), asdict(change)
        want["custody"].pop("retrieved_ts"), got["custody"].pop("retrieved_ts")
        assert got == want
    assert {c.landed_via for c in scoped} >= {"merge-pr", "pr-branch-commit", "direct-to-main"}


def test_branch_not_reachable_from_head(tmp_path):
    repo = _repo(tmp_path)
    git(repo, "checkout", "-q", "main")
    rec = _record(repo)
    assert rec["unread_commits"] == []
    assert [c["subject"] for c in rec["commits"]] == ["PROJ-9: the thing"]
