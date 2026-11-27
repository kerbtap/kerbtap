"""Check runs on a pull request's head commit, as Evidence Pack evidence.

The pack reads each check run's publishing GitHub App rather than the combined rollup,
which accepts a green result from any publisher. Every outcome row is driven here,
including the ones the pack must refuse to call a pass.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kerbtap.assemble import _pr_evidence
from kerbtap.build import render_pack
from kerbtap.collect import github as github_mod
from kerbtap.collect.github import CHECKS_LIMITS, classify_check_runs, collect_pr_details
from tests.factories import make_bundle

HEAD = "f" * 40


def _run(conclusion: str | None, status: str = "completed", name: str = "checks", app: str = "github-actions") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "app": {"slug": app}}


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _pr_view(number: int = 9, head: str | None = HEAD) -> dict:
    payload = {
        "number": number, "title": "t", "state": "MERGED", "body": "b", "reviews": [],
        "author": {"login": "a"}, "mergedBy": {"login": "b"}, "mergeCommit": {"oid": "abc"},
        "baseRefName": "main", "headRefName": "feat/x", "mergedAt": "2026-01-01T00:00:00Z",
    }
    if head is not None:
        payload["headRefOid"] = head
    return payload


def _collect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, check_runs: object, view: dict | None = None) -> dict:
    """Run ``collect_pr_details`` against fake ``gh pr view`` and ``gh api`` responses.

    ``check_runs`` is the check-runs payload, or ``("HTTP 404",)`` to fail that call.
    """
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return _FakeCompleted(json.dumps(view or _pr_view()))
        assert argv[:2] == ["gh", "api"] and "/check-runs" in argv[2], argv
        if isinstance(check_runs, tuple):
            raise subprocess.CalledProcessError(1, argv, stderr=f"gh: x ({check_runs[0]})")
        return _FakeCompleted(json.dumps(check_runs))

    monkeypatch.setattr(github_mod, "gh_available", lambda: True)
    monkeypatch.setattr(github_mod.subprocess, "run", fake_run)
    details = collect_pr_details(tmp_path, [9])
    details["_calls"] = calls
    return details


# --- the outcome table, row by row ------------------------------------------------------


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([], "no-recorded-evidence"),
        ([_run("failure")], "fail"),
        ([_run("timed_out")], "fail"),
        ([_run("failure"), _run("cancelled")], "fail"),  # a failure wins over an unsettled run
        ([_run("success"), _run("skipped"), _run("neutral")], "pass"),
        ([_run("success")], "pass"),
        ([_run("skipped"), _run("neutral")], "undetermined"),  # nothing ran to success
        ([_run("success"), _run("cancelled")], "undetermined"),
        ([_run("success"), _run("stale")], "undetermined"),
        ([_run("action_required")], "undetermined"),
        ([_run("success"), _run(None, status="in_progress")], "undetermined"),
        ([_run("success", status="queued")], "undetermined"),  # a conclusion without completion
        ([_run("some_new_value")], "undetermined"),
    ],
)
def test_classify_check_runs(runs: list[dict], expected: str) -> None:
    assert classify_check_runs(runs) == expected


# --- the collector -----------------------------------------------------------------------


def test_collector_records_the_publisher_of_each_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    details = _collect(monkeypatch, tmp_path, {"total_count": 1, "check_runs": [_run("success")]})
    checks = details[9]["checks"]
    assert checks["outcome"] == "pass"
    assert checks["head_sha"] == HEAD
    assert checks["runs"] == [{"name": "checks", "status": "completed", "conclusion": "success", "app": "github-actions"}]
    assert checks["limits"] == CHECKS_LIMITS
    assert any(f"commits/{HEAD}/check-runs" in argv[2] for argv in details["_calls"] if argv[1] == "api")


def test_failing_run_is_recorded_as_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    details = _collect(monkeypatch, tmp_path, {"total_count": 2, "check_runs": [_run("success"), _run("failure")]})
    assert details[9]["checks"]["outcome"] == "fail"


def test_truncated_list_is_undetermined_not_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    details = _collect(monkeypatch, tmp_path, {"total_count": 101, "check_runs": [_run("success")]})
    assert details[9]["checks"]["outcome"] == "undetermined"


def test_failed_read_is_undetermined(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    details = _collect(monkeypatch, tmp_path, ("HTTP 404",))
    assert details[9]["checks"]["outcome"] == "undetermined"
    assert "runs" not in details[9]["checks"]


def test_missing_head_sha_skips_the_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    details = _collect(monkeypatch, tmp_path, {"total_count": 0, "check_runs": []}, view=_pr_view(head=None))
    assert details[9]["checks"]["outcome"] == "undetermined"
    assert not [argv for argv in details["_calls"] if argv[1] == "api"]


def test_barred_term_in_a_check_name_is_redacted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    details = _collect(
        monkeypatch, tmp_path, {"total_count": 1, "check_runs": [_run("success", name="immutable-build")]}
    )
    assert "immutable" not in details[9]["checks"]["runs"][0]["name"].lower()


# --- PRs whose details were not collected -------------------------------------------------


@pytest.mark.parametrize(
    ("gh_up", "capped", "details"),
    [(False, set(), {}), (True, {9}, {}), (True, set(), {})],
    ids=["gh-unavailable", "capped", "view-returned-nothing"],
)
def test_uncollected_pr_carries_undetermined_checks(gh_up: bool, capped: set[int], details: dict) -> None:
    record = _pr_evidence(landing="merge-pr", pr_number=9, pr_details=details, gh_up=gh_up, capped=capped)
    assert record is not None
    assert record["checks"]["outcome"] == "undetermined"


def test_non_pr_change_carries_no_checks() -> None:
    assert _pr_evidence(landing="direct-to-main", pr_number=None, pr_details={}, gh_up=True) is None


# --- rendering ------------------------------------------------------------------------------


def _bundle_with_checks(outcome: str, *, dogfood: bool = True):
    bundle = make_bundle()
    bundle.changes[0].pull_request["checks"] = {"outcome": outcome, "basis": "x", "runs": []}
    if not dogfood:
        bundle.dogfood_observations = []
    return bundle


def test_failing_check_section_renders_without_a_dogfood_log() -> None:
    pack = render_pack(_bundle_with_checks("fail", dogfood=False))
    log = pack["exception-log.md"]
    assert "## 3. Pull-request head commits with a failing check run at collection time" in log
    assert "| `aaa` | #34 |" in log
    assert "## 4. Governance gaps" in log


def test_report_counts_check_outcomes() -> None:
    report = render_pack(_bundle_with_checks("pass"))["report.md"]
    assert "**Check runs on the pull-request head commit:** 1 pull request(s) assessed: 1 pass" in report


def test_check_outcomes_do_not_change_review_gap_counts() -> None:
    before = render_pack(make_bundle())["exception-log.md"].split("## 3.")[0]
    after = render_pack(_bundle_with_checks("undetermined"))["exception-log.md"].split("## 3.")[0]
    assert before == after
