"""Cross-product evidence matrix: the AI-orchestration narrative is derived
compositionally from the full detected-signal set, so coverage is principled rather
than driven by whichever real repos we happened to test.

Independent signals: has_claude_dir, has_governance_scaffold, has_dogfood_log,
has_decision_log, ai_trailer_commits, and the PR-landing axis. For every cell we assert:
(a) the headline (report 'Delivery workflow' line) and the assessment paragraph (manifest
    basis) are mutually consistent — both are checked against ground truth;
(b) neither asserts an absent signal (never claims .claude/, a hook, a decision log, a
    dogfood log, or a trailer that is not there);
(c) the AI-trailer count is exact.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from kerbtap.assemble import _ai_coauthor_lines, _has_ai_trailer, build_bundle
from kerbtap.build import render_pack
from tests.factories import make_commit


# --- repo builder -------------------------------------------------------------------


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


_CLAUDE_TRAILER = "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"


def build_repo(
    path: Path,
    *,
    claude_skill: bool = False,
    scaffold: bool = False,
    dogfood: bool = False,
    decision_log: bool = False,
    ai_trailer_commits: int = 0,
    merge_pr: bool = False,
) -> None:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True, text=True)
    _git(path, "config", "user.name", "Dev")
    _git(path, "config", "user.email", "dev@example.com")

    if claude_skill:
        (path / ".claude" / "skills" / "enricher").mkdir(parents=True)
        (path / ".claude" / "skills" / "enricher" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    if scaffold:
        (path / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
        (path / ".claude" / "hooks" / "precommit_review_guard.py").write_text("# gate\n", encoding="utf-8")
        (path / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
        (path / ".claude" / "agents" / "architect").mkdir(parents=True, exist_ok=True)
        (path / ".claude" / "agents" / "architect" / "SKILL.md").write_text("# architect\n", encoding="utf-8")
    if dogfood:
        (path / "docs" / "replication").mkdir(parents=True, exist_ok=True)
        (path / "docs" / "replication" / "dogfood-log.md").write_text(
            "# Dogfood Log\n\n**O4 — The bypass leaves no evidence artifact.** More.\n", encoding="utf-8"
        )
    if decision_log:
        (path / "docs").mkdir(parents=True, exist_ok=True)
        (path / "docs" / "decision-log.md").write_text("# Decision log\n", encoding="utf-8")

    (path / "base.txt").write_text("base", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "Initial commit")

    for i in range(ai_trailer_commits):
        (path / f"ai_{i}.txt").write_text(str(i), encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-m", f"AI-assisted change {i}", "-m", _CLAUDE_TRAILER)

    if merge_pr:
        _git(path, "checkout", "-b", "feat")
        (path / "feat.txt").write_text("feature", encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-m", "feature work")
        _git(path, "checkout", "main")
        _git(path, "merge", "--no-ff", "feat", "-m", "Merge pull request #1 from org/feat")


# --- matrix -------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    cid: str
    kwargs: dict
    state: str
    governed: bool
    has_claude: bool
    has_hook: bool
    has_log: bool  # decision log OR dogfood present
    trailers: int


CELLS: list[Cell] = [
    # 1. governance scaffold present (governed) — with / without trailers.
    Cell("1a-governed-scaffold", {"scaffold": True, "decision_log": True},
         "governed", True, True, True, True, 0),
    Cell("1b-governed-scaffold+trailers", {"scaffold": True, "decision_log": True, "ai_trailer_commits": 2},
         "governed", True, True, True, True, 2),
    # 2. .claude/ skills only, no scaffold/dogfood — with / without trailers.
    Cell("2a-claude-skill-only", {"claude_skill": True},
         "ai-assisted-ungoverned", False, True, False, False, 0),
    Cell("2b-claude-skill+trailers", {"claude_skill": True, "ai_trailer_commits": 2},
         "ai-assisted-ungoverned", False, True, False, False, 2),
    # 3. NO .claude/, AI trailers, no governance (loop-engineering shape).
    Cell("3-trailers-only", {"ai_trailer_commits": 3},
         "ai-assisted-ungoverned", False, False, False, False, 3),
    # 4. NO .claude/, dogfood log present but no scaffold.
    Cell("4-dogfood-no-scaffold", {"dogfood": True},
         "governed", True, False, False, True, 0),
    # 5. nothing at all (markitdown shape).
    Cell("5-nothing", {}, "none", False, False, False, False, 0),
    # 6. orthogonal PR axis.
    Cell("6a-governed+merge-pr", {"scaffold": True, "merge_pr": True},
         "governed", True, True, True, False, 0),
    Cell("6b-nothing-all-direct", {}, "none", False, False, False, False, 0),
]

# Phrases that POSITIVELY claim a signal is present (honest negations like "no .claude/
# directory" are allowed and must not trip the never-assert-absent checks).
_CLAUDE_CLAIMS = ("configured (`.claude/`)", "`.claude/` Claude Code configuration", ".claude/ hooks")
_TRAILER_CLAIMS = ("carry AI co-author trailers", "carrying an AI co-author trailer")


def _scope_line(report: str) -> str:
    for line in report.splitlines():
        if line.startswith("- **Delivery workflow:**"):
            return line
    return ""


@pytest.mark.parametrize("cell", CELLS, ids=[c.cid for c in CELLS])
def test_evidence_matrix_cell(cell: Cell, tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    build_repo(repo, **cell.kwargs)
    rendered = render_pack(build_bundle(repo))
    manifest = json.loads(rendered["manifest.json"])
    report = rendered["report.md"]
    att = manifest["repo_level_attestation"]
    scope_line = _scope_line(report)
    basis = att["basis"]

    # (c) exact trailer count.
    assert att["ai_signals"]["ai_trailer_commits"] == cell.trailers
    assert att["ai_state"] == cell.state
    assert att["ai_orchestrated"] is cell.governed

    # (a)+(b) neither the headline nor the paragraph POSITIVELY asserts an absent signal
    # (honest negations such as "no .claude/ directory" are allowed).
    for text in (scope_line, basis):
        if not cell.has_claude:
            for claim in _CLAUDE_CLAIMS:
                assert claim not in text, f"{cell.cid}: claimed .claude/ in {text!r}"
        if cell.trailers == 0:
            for claim in _TRAILER_CLAIMS:
                assert claim not in text, f"{cell.cid}: claimed trailers in {text!r}"

    # trailers present + ungoverned → both headline and paragraph name them with exact count.
    if cell.trailers and not cell.governed:
        assert f"{cell.trailers} commit(s) carry AI co-author trailers" in scope_line
        assert f"{cell.trailers} commit(s) carrying an AI co-author trailer" in basis
    # trailers present + governed → paragraph names them (headline is the governed one-liner).
    if cell.trailers and cell.governed:
        assert "AI co-author trailer" in basis

    # headline shape per state (mutually consistent with the paragraph).
    if cell.governed:
        assert scope_line.endswith("governed AI-orchestration evidenced (repo-level assessment, below).")
        assert "governed AI-orchestrated delivery is evidenced by" in basis
    elif cell.has_claude or cell.trailers:
        assert "governance not evidenced" in scope_line
        assert "governed AI-orchestration of delivery is not evidenced" in basis
    else:
        assert "no AI tooling or orchestration evidence detected" in scope_line
        assert "No evidence of AI tooling or orchestration found" in basis

    # attestation-gate bullets never assert an absent control.
    hook_claim = "a deny-mode pre-commit hook gates deliverable commits"
    if cell.has_hook:
        assert hook_claim in report
    else:
        assert hook_claim not in report
        assert "no deny-mode pre-commit review gate found in this repository" in report
    if not cell.has_log:
        assert "no git-versioned decision log or dogfood log found" in report


# --- per-clause outcome differentiation (end-to-end) --------------------------------


def test_pr_without_approving_review_differentiates_per_clause(tmp_path: Path) -> None:
    """A PR-landed change with no approving GitHub review and no PASS must yield
    A.8.32=observed (the PR is the mechanism) but CC8.1/PW.7/Art.17=process-only."""
    repo = tmp_path / "r"
    repo.mkdir()
    build_repo(repo, merge_pr=True)  # git-only: no gh reviews, merge actor unknown
    manifest = json.loads(render_pack(build_bundle(repo))["manifest.json"])
    changes = manifest["changes"]

    merge = next(c for c in changes if c["landed_via"] == "merge-pr")
    outcomes = merge["control_outcomes"]
    assert outcomes["ISO-A.8.32"] == "observed"
    assert outcomes["SOC2-CC8.1"] == "process-only"
    assert outcomes["SSDF-PW.7"] == "process-only"
    assert outcomes["DORA-Art.17"] == "process-only"  # merged_by/author unknown → not segregated

    direct = next(c for c in changes if c["landed_via"] == "direct-to-main")
    assert set(direct["control_outcomes"].values()) == {"not-observed"}


# --- honesty-guard x repo-content: barred words in the repo's OWN text ---------------


def test_barred_words_in_repo_content_do_not_trip_guard(tmp_path: Path) -> None:
    """A repo whose own commit subjects/bodies contain barred integrity words must still
    produce a pack: the words are neutralized to [integrity-term omitted], the guard is
    not tripped, and the fixed disclaimer is intact."""
    from kerbtap.honesty import BARRED_TERMS, INTEGRITY_DISCLAIMER

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    _git(repo, "config", "user.name", "Dev")
    _git(repo, "config", "user.email", "dev@example.com")

    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git(repo, "add", "-A")
    # The headroom leak: a barred word in a commit SUBJECT (direct-to-main).
    _git(repo, "commit", "-m", "fix: A2 — system prompt immutable; memory routes")
    (repo / "b.txt").write_text("b", encoding="utf-8")
    _git(repo, "add", "-A")
    # A barred word in a squash-PR subject.
    _git(repo, "commit", "-m", "feat: tamper-proof audit-grade ledger (#7)")
    (repo / "c.txt").write_text("c", encoding="utf-8")
    _git(repo, "add", "-A")
    # A barred word inside a reviewed commit body (review-evidence excerpt path).
    _git(repo, "commit", "-m", "chore: harden", "-m",
         "Adversarial review: architect C1-C7, re-review PASS. The ledger is now immutable.")

    # (a) The pack IS written (render_pack runs the fail-closed guard on every component).
    rendered = render_pack(build_bundle(repo))
    manifest_text = rendered["manifest.json"]

    # (b) Barred terms appear ONLY as the neutralization marker, never raw.
    assert "[integrity-term omitted]" in manifest_text
    scrubbed = manifest_text.replace(INTEGRITY_DISCLAIMER, " ").lower()
    for term in BARRED_TERMS:
        assert term not in scrubbed, f"raw barred term {term!r} leaked into the manifest"

    # The specific subject leak is neutralized in the change record.
    manifest = json.loads(manifest_text)
    subjects = [c["subject"] for c in manifest["changes"]]
    assert any("[integrity-term omitted]" in s for s in subjects)
    assert not any("immutable" in s.lower() for s in subjects)

    # (c) The fixed disclaimer is intact.
    assert INTEGRITY_DISCLAIMER in manifest_text


# --- trailer classification unit cases (Bug 2) --------------------------------------


class TestTrailerClassification:
    def test_capital_claude_anthropic_email_is_ai(self) -> None:
        c = make_commit(body="Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>")
        assert _has_ai_trailer(c) is True

    def test_lowercase_variant_is_ai(self) -> None:
        c = make_commit(body="Co-authored-by: Claude Haiku 4.5 <noreply@anthropic.com>")
        assert _has_ai_trailer(c) is True

    def test_human_coauthor_is_not_ai(self) -> None:
        c = make_commit(body="Co-authored-by: Jane Human <jane@x.com>")
        assert _has_ai_trailer(c) is False

    def test_github_actions_bot_is_not_ai(self) -> None:
        c = make_commit(
            body="Co-authored-by: github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"
        )
        assert _has_ai_trailer(c) is False

    def test_human_named_cobus_is_not_ai(self) -> None:
        c = make_commit(body="Co-authored-by: Cobus Greyling <cobusgreyling@example.local>")
        assert _has_ai_trailer(c) is False

    def test_multiple_ai_trailers_in_one_commit_count_as_lines(self) -> None:
        body = (
            "feat: wire circuit breaker\n\n"
            "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>\n"
            "Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>\n"
            "Co-authored-by: Cobus Greyling <cobus@example.local>\n"
        )
        lines = _ai_coauthor_lines(body)
        assert len(lines) == 2  # two Claude lines; the human is excluded


def test_trailer_line_vs_commit_count(tmp_path: Path) -> None:
    """A single commit carrying multiple Claude trailers counts as 1 commit, N lines."""
    repo = tmp_path / "multi"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    _git(repo, "config", "user.name", "Dev")
    _git(repo, "config", "user.email", "dev@example.com")
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git(repo, "add", "-A")
    body = (
        "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>\n"
        "Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>\n"
        "Co-authored-by: github-actions[bot] <x@users.noreply.github.com>"
    )
    _git(repo, "commit", "-m", "one commit, two AI trailers", "-m", body)

    manifest = json.loads(render_pack(build_bundle(repo))["manifest.json"])
    signals = manifest["repo_level_attestation"]["ai_signals"]
    assert signals["ai_trailer_commits"] == 1  # distinct commits
    assert signals["ai_trailer_lines"] == 2  # two Claude lines; bot excluded
