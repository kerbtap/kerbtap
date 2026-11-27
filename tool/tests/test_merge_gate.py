"""CODEOWNERS + branch-protection (merge gate) as approval evidence.

The ruling invariants: the pack reads only what the current credentials can see and
says so (tri-state, never a false "absent"); the merge gate is a repo-level CURRENT-
configuration control, never per-change historical evidence; CODEOWNERS contents
(owner handles/emails) never enter the pack.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kerbtap.assemble import build_bundle
from kerbtap.build import render_pack
from kerbtap.collect import github as github_mod
from kerbtap.collect.github import collect_branch_protection, summarize_merge_gate
from kerbtap.collect.governance import detect_codeowners
from tests.repo_factory import commit, init_repo


# --- fake gh api router ---------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _route_key(endpoint: str) -> str:
    """Map a gh api endpoint to a stable route key (specific patterns first)."""
    if endpoint == "repos/{owner}/{repo}":
        return "repo"
    if "/rules/branches/" in endpoint:
        return "rules"
    if endpoint.endswith("/protection"):
        return "protection"
    if "/branches/" in endpoint:
        return "branch"
    raise AssertionError(f"unrecognized endpoint {endpoint!r}")


def _router(responses: dict[str, object]):
    """Fake subprocess.run for `gh api <endpoint>`: responses keyed by route key.

    A value of ("HTTP 403",) / ("HTTP 404",) raises CalledProcessError with that stderr;
    any other value is JSON-serialized as stdout.
    """

    def fake_run(argv, **kwargs):
        assert argv[:2] == ["gh", "api"], f"unexpected call: {argv}"
        key = _route_key(argv[2])
        if key not in responses:
            raise AssertionError(f"no fake response for route {key!r} ({argv[2]!r})")
        value = responses[key]
        if isinstance(value, tuple):
            raise subprocess.CalledProcessError(1, argv, stderr=f"gh: x ({value[0]})")
        return _FakeCompleted(json.dumps(value))

    return fake_run


_REPO_MAIN = {"default_branch": "main"}


def _collect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, responses: dict) -> dict:
    monkeypatch.setattr(github_mod.subprocess, "run", _router(responses))
    return collect_branch_protection(tmp_path)


# --- collect_branch_protection over the API state space --------------------------------


class TestCollectBranchProtection:
    def test_ruleset_protected_zero_reviews_is_present_but_not_review_requiring(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The real markitdown shape: ruleset protection ON, approving reviews = 0.
        bp = _collect(monkeypatch, tmp_path, {
            "repo": _REPO_MAIN,
            "branch": {"protected": True},
            "rules":[
                {"type": "deletion"},
                {"type": "pull_request",
                 "parameters": {"required_approving_review_count": 0,
                                "require_code_owner_review": False}},
            ],
        })
        assert bp["state"] == "present"
        assert bp["required_approving_reviews"] == 0
        assert bp["requires_review"] is False
        assert "0 approving review(s) required" in str(bp["note"])
        assert bp["rule_types"] == ["deletion", "pull_request"]

    def test_ruleset_requiring_reviews_and_code_owners(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bp = _collect(monkeypatch, tmp_path, {
            "repo": _REPO_MAIN,
            "branch": {"protected": True},
            "rules":[
                {"type": "pull_request",
                 "parameters": {"required_approving_review_count": 2,
                                "require_code_owner_review": True}},
            ],
        })
        assert bp["state"] == "present"
        assert bp["requires_review"] is True
        assert "code-owner review required" in str(bp["note"])

    def test_classic_protection_fallback_when_no_ruleset_pr_rule(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bp = _collect(monkeypatch, tmp_path, {
            "repo": _REPO_MAIN,
            "branch": {"protected": True},
            "rules":[{"type": "deletion"}],
            "protection":{
                "required_pull_request_reviews": {
                    "required_approving_review_count": 1,
                    "require_code_owner_reviews": True,
                }
            },
        })
        assert bp["state"] == "present"
        assert bp["required_approving_reviews"] == 1
        assert bp["requires_review"] is True

    def test_ruleset_without_pr_rule_and_no_classic_is_a_known_no_review_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The real flowsint shape: rulesets readable but carry no pull_request rule,
        # classic protection 404. The configuration IS known — a finding, not an unknown.
        bp = _collect(monkeypatch, tmp_path, {
            "repo": _REPO_MAIN,
            "branch": {"protected": True},
            "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}],
            "protection": ("HTTP 404",),
        })
        assert bp["state"] == "present"
        assert bp["requires_review"] is False
        assert "no pull-request review requirement is configured" in str(bp["note"])
        assert "not readable" not in str(bp["note"])
        assert summarize_merge_gate(bp) == (
            "present on `main` (no pull-request review requirement configured)"
        )

    def test_protected_but_config_unreadable_asserts_presence_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Non-admin on a classic-protected repo: rules 403, protection detail 403.
        bp = _collect(monkeypatch, tmp_path, {
            "repo": _REPO_MAIN,
            "branch": {"protected": True},
            "rules":("HTTP 403",),
            "protection":("HTTP 403",),
        })
        assert bp["state"] == "present"
        assert bp["requires_review"] is None
        assert "not readable with the current credentials" in str(bp["note"])
        assert "no review-requirement detail is asserted" in str(bp["note"]).lower()

    def test_sparse_detail_count_unknown_yields_unknown_not_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # count unknown + codeowner False: review requirement is UNKNOWN, never False.
        bp = _collect(monkeypatch, tmp_path, {
            "repo": _REPO_MAIN,
            "branch": {"protected": True},
            "rules": [{"type": "pull_request",
                       "parameters": {"require_code_owner_review": False}}],
        })
        assert bp["state"] == "present"
        assert bp["requires_review"] is None
        assert "approving-review count not readable" in str(bp["note"])

    def test_sparse_detail_codeowner_true_is_decisive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bp = _collect(monkeypatch, tmp_path, {
            "repo": _REPO_MAIN,
            "branch": {"protected": True},
            "rules": [{"type": "pull_request",
                       "parameters": {"require_code_owner_review": True}}],
        })
        assert bp["requires_review"] is True

    def test_unprotected_branch_is_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The real free-plan-private shape: protected=false, rules endpoint plan-gated.
        bp = _collect(monkeypatch, tmp_path, {
            "repo": _REPO_MAIN,
            "branch": {"protected": False},
        })
        assert bp["state"] == "absent"
        assert "not marked protected" in str(bp["note"])

    def test_unresolvable_repo_is_not_collected_never_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bp = _collect(monkeypatch, tmp_path, {"repo": ("HTTP 404",)})
        assert bp["state"] == "not-collected"
        assert "not resolvable" in str(bp["note"])

    def test_malformed_api_json_degrades_to_not_collected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            github_mod.subprocess, "run", lambda *a, **k: _FakeCompleted("not json {")
        )
        assert collect_branch_protection(tmp_path)["state"] == "not-collected"

    def test_barred_term_in_branch_name_is_redacted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bp = _collect(monkeypatch, tmp_path, {
            "repo": {"default_branch": "immutable-main"},
            "branch": {"protected": False},
        })
        assert "immutable" not in json.dumps(bp).lower()
        assert "[integrity-term omitted]" in str(bp["default_branch"])


class TestSummarizeMergeGate:
    def test_present_with_params(self) -> None:
        line = summarize_merge_gate({
            "state": "present", "default_branch": "main",
            "required_approving_reviews": 2, "require_code_owner_review": True,
        })
        assert "2 approving review(s) required" in line
        assert "code-owner review required" in line

    def test_present_detail_unreadable(self) -> None:
        line = summarize_merge_gate({
            "state": "present", "default_branch": "main",
            "required_approving_reviews": None, "require_code_owner_review": None,
        })
        assert "not readable" in line

    def test_absent_and_not_collected(self) -> None:
        assert "not enabled" in summarize_merge_gate(
            {"state": "absent", "default_branch": "main"}
        )
        assert "not determinable this run" in summarize_merge_gate(
            {"state": "not-collected", "note": "gh unavailable."}
        )


# --- CODEOWNERS detection ---------------------------------------------------------------


class TestDetectCodeowners:
    def test_github_location_takes_precedence(self, tmp_path: Path) -> None:
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "CODEOWNERS").write_text(
            "# comment\n*.py @org/reviewers\ndocs/ owner@corp.com\n", encoding="utf-8"
        )
        (tmp_path / "CODEOWNERS").write_text("* @root-owner\n", encoding="utf-8")
        record = detect_codeowners(tmp_path)
        assert record == {"path": ".github/CODEOWNERS", "rule_count": 2}

    @pytest.mark.parametrize("rel", ["CODEOWNERS", "docs/CODEOWNERS"])
    def test_other_standard_locations(self, tmp_path: Path, rel: str) -> None:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("* @owner\n", encoding="utf-8")
        record = detect_codeowners(tmp_path)
        assert record == {"path": rel, "rule_count": 1}

    def test_comments_only_file_is_present_with_zero_rules(self, tmp_path: Path) -> None:
        (tmp_path / "CODEOWNERS").write_text("# only comments\n\n", encoding="utf-8")
        assert detect_codeowners(tmp_path) == {"path": "CODEOWNERS", "rule_count": 0}

    def test_absent(self, tmp_path: Path) -> None:
        assert detect_codeowners(tmp_path) is None

    def test_record_never_carries_file_contents(self, tmp_path: Path) -> None:
        (tmp_path / "CODEOWNERS").write_text("* jane.doe@corp.com\n", encoding="utf-8")
        record = detect_codeowners(tmp_path)
        assert "jane.doe@corp.com" not in json.dumps(record)


# --- end-to-end: pack rendering across gate states ---------------------------------------


def _patch_gh(monkeypatch: pytest.MonkeyPatch, bp: dict | None) -> None:
    """Patch the gh-dependent collectors; bp=None means gh unavailable."""
    if bp is None:
        monkeypatch.setattr(github_mod, "gh_available", lambda: False)
        return
    monkeypatch.setattr(github_mod, "gh_available", lambda: True)
    monkeypatch.setattr(github_mod, "collect_pull_requests", lambda *a, **k: [])
    monkeypatch.setattr(github_mod, "collect_pr_details", lambda *a, **k: {})
    monkeypatch.setattr(github_mod, "collect_branch_protection", lambda *a, **k: bp)


_BP_REQUIRING = {
    "state": "present", "default_branch": "main", "rule_types": ["pull_request"],
    "required_approving_reviews": 1, "require_code_owner_review": True,
    "requires_review": True,
    "note": "The default branch (`main`) is protected (current configuration read at "
            "collection time): 1 approving review(s) required; code-owner review required.",
}
_BP_ZERO = {
    "state": "present", "default_branch": "main", "rule_types": ["pull_request"],
    "required_approving_reviews": 0, "require_code_owner_review": False,
    "requires_review": False,
    "note": "The default branch (`main`) is protected (current configuration read at "
            "collection time): 0 approving review(s) required.",
}
_BP_UNREADABLE = {
    "state": "present", "default_branch": "main", "rule_types": None,
    "required_approving_reviews": None, "require_code_owner_review": None,
    "requires_review": None,
    "note": "The default branch (`main`) is marked protected, but the protection "
            "configuration is not readable with the current credentials (rulesets: "
            "forbidden; classic protection: forbidden). Presence is asserted from the "
            "branch metadata only; no review-requirement detail is asserted.",
}


class TestPackRendering:
    def test_gh_down_renders_tristate_not_a_false_absence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_gh(monkeypatch, None)
        repo = init_repo(tmp_path / "r")
        commit(repo, "change")
        rendered = render_pack(build_bundle(repo))
        report = rendered["report.md"]
        assert "| Branch protection (GitHub, default branch) | not determinable |" in report
        manifest = json.loads(rendered["manifest.json"])
        assert manifest["merge_gate"]["state"] == "not-collected"
        assert "not per-change evidence" in manifest["merge_gate"]["scope_note"].lower()
        # The remediation gap is hedged, not asserted as absence.
        remediation = rendered["remediation-plan.md"]
        assert "Branch protection was not readable this run" in remediation

    def test_review_requiring_gate_removes_the_no_gate_gap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_gh(monkeypatch, dict(_BP_REQUIRING))
        repo = init_repo(tmp_path / "r")
        commit(repo, "change")
        rendered = render_pack(build_bundle(repo))
        remediation = rendered["remediation-plan.md"]
        assert "No enforcing pre-commit / pre-merge review gate" not in remediation
        report = rendered["report.md"]
        assert "| Branch protection (GitHub, default branch) | yes |" in report
        assert "1 approving review(s) required; code-owner review required" in report

    def test_zero_review_gate_surfaces_the_real_finding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The markitdown shape: gate present, approving reviews NOT required.
        _patch_gh(monkeypatch, dict(_BP_ZERO))
        repo = init_repo(tmp_path / "r")
        commit(repo, "change")
        remediation = render_pack(build_bundle(repo))["remediation-plan.md"]
        assert "does not require an approving review" in remediation
        assert "No enforcing pre-commit / pre-merge review gate" not in remediation

    def test_unreadable_detail_gets_the_verify_gap_not_an_absence_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_gh(monkeypatch, dict(_BP_UNREADABLE))
        repo = init_repo(tmp_path / "r")
        commit(repo, "change")
        rendered = render_pack(build_bundle(repo))
        remediation = rendered["remediation-plan.md"]
        assert "could not be verified" in remediation
        assert "No enforcing pre-commit / pre-merge review gate" not in remediation
        assert "The default branch is not protected" not in remediation

    def test_empty_gate_record_hedges_as_not_collected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_gh(monkeypatch, {})  # malformed/empty record
        repo = init_repo(tmp_path / "r")
        commit(repo, "change")
        remediation = render_pack(build_bundle(repo))["remediation-plan.md"]
        assert "Branch protection was not readable this run" in remediation
        assert "The default branch is not protected" not in remediation

    def test_codeowners_present_in_pack_but_contents_never_leak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_gh(monkeypatch, None)
        repo = init_repo(tmp_path / "r")
        (repo / ".github").mkdir()
        (repo / ".github" / "CODEOWNERS").write_text(
            "*.py @org/security jane.doe@corp.com\n", encoding="utf-8"
        )
        commit(repo, "add codeowners")
        rendered = render_pack(build_bundle(repo))
        report = rendered["report.md"]
        assert "| Code-review ownership (CODEOWNERS) | yes |" in report
        manifest = json.loads(rendered["manifest.json"])
        assert manifest["codeowners"] == {"path": ".github/CODEOWNERS", "rule_count": 1}
        for filename, text in rendered.items():
            assert "jane.doe@corp.com" not in text, f"CODEOWNERS content leaked into {filename}"
            assert "@org/security" not in text, f"CODEOWNERS content leaked into {filename}"

    def test_control_map_renders_merge_gate_and_codeowners_sections(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_gh(monkeypatch, dict(_BP_REQUIRING))
        repo = init_repo(tmp_path / "r")
        (repo / "CODEOWNERS").write_text("* @owner\n", encoding="utf-8")
        commit(repo, "add codeowners")
        control_map = render_pack(build_bundle(repo))["control-map.md"]
        assert "Deny-by-default merge gate" in control_map
        assert "Merge gate present on `main`" in control_map
        assert "not per-change evidence" in control_map
        assert "Code-review ownership routing (CODEOWNERS)" in control_map
        assert "1 ownership rule(s)" in control_map

    def test_per_change_outcomes_are_untouched_by_a_present_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A currently-configured gate must NOT upgrade historical per-change outcomes —
        current configuration is not evidence that past changes were gated."""
        _patch_gh(monkeypatch, dict(_BP_REQUIRING))
        repo = init_repo(tmp_path / "r")
        commit(repo, "direct change")
        manifest = json.loads(render_pack(build_bundle(repo))["manifest.json"])
        outcomes = manifest["changes"][0]["control_outcomes"]
        assert set(outcomes.values()) == {"not-observed"}  # direct-to-main stays honest
