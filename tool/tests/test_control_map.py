"""Tests for the static control-clause lookup and per-change control outcomes."""

from __future__ import annotations

from kerbtap.assemble import _change_control_outcomes
from kerbtap.collect.governance import load_control_clauses


class TestControlClauseLookup:
    def test_loads_expected_controls(self) -> None:
        data = load_control_clauses()
        control_ids = {c["control_id"] for c in data["controls"]}
        assert {
            "adversarial-review-gate",
            "change-mgmt-feature-branch-pr",
            "segregation-of-duties",
            "enforcing-precommit-review-hook",
            "decision-log-audit-trail",
        } <= control_ids

    def test_review_gate_maps_to_expected_clauses(self) -> None:
        data = load_control_clauses()
        gate = next(c for c in data["controls"] if c["control_id"] == "adversarial-review-gate")
        clause_ids = {c["clause_id"] for c in gate["clauses"]}
        assert clause_ids == {"SOC2-CC8.1", "ISO-A.8.32", "DORA-Art.17", "SSDF-PW.7"}

    def test_audit_trail_maps_to_dora_art_33(self) -> None:
        data = load_control_clauses()
        control = next(c for c in data["controls"] if c["control_id"] == "decision-log-audit-trail")
        assert [c["clause_id"] for c in control["clauses"]] == ["DORA-Art.33"]

    def test_decision_log_does_not_assert_append_only_as_proven(self) -> None:
        data = load_control_clauses()
        control = next(c for c in data["controls"] if c["control_id"] == "decision-log-audit-trail")
        detected = control["detected_by"].lower()
        # Honesty: 'append-only' may appear only when qualified as a convention, not enforced.
        assert "documented convention" in detected
        assert "not an enforced technical control" in detected

    def test_carries_honesty_note_and_caveats(self) -> None:
        data = load_control_clauses()
        assert "authorship-agnostic" in data["honesty_note"]
        assert "SOC 2" in data["clause_caveats"]
        assert "ISO/IEC 27001:2022" in data["clause_caveats"]


class TestChangeControlOutcomes:
    def test_recorded_pass_attests_review_clauses_but_not_segregation(self) -> None:
        # A review trailer attests review/approval/change-mgmt clauses, but NOT DORA Art.17
        # segregation of duties — the trailer is not identity-bound (approver != author is
        # a separate signal). Segregation with no merge-actor evidence stays process-only.
        outcomes = _change_control_outcomes(
            has_pr=True, review_attested=True, has_approving_review=False, segregation_evidenced=False
        )
        assert outcomes["ISO-A.8.32"] == "attested"
        assert outcomes["SOC2-CC8.1"] == "attested"
        assert outcomes["SSDF-PW.7"] == "attested"
        assert outcomes["DORA-Art.17"] == "process-only"

    def test_review_pass_with_segregation_makes_art17_observed(self) -> None:
        # Art.17 becomes observed only when approver != author is actually evidenced,
        # regardless of the review trailer.
        outcomes = _change_control_outcomes(
            has_pr=True, review_attested=True, has_approving_review=False, segregation_evidenced=True
        )
        assert outcomes["DORA-Art.17"] == "observed"

    def test_pr_no_review_no_approval_no_segregation_differentiates(self) -> None:
        outcomes = _change_control_outcomes(
            has_pr=True, review_attested=False, has_approving_review=False, segregation_evidenced=False
        )
        # Change management is observed via the PR mechanism; the rest are process-only.
        assert outcomes["ISO-A.8.32"] == "observed"
        assert outcomes["SOC2-CC8.1"] == "process-only"
        assert outcomes["SSDF-PW.7"] == "process-only"
        assert outcomes["DORA-Art.17"] == "process-only"

    def test_approving_review_makes_cc81_and_pw7_observed(self) -> None:
        outcomes = _change_control_outcomes(
            has_pr=True, review_attested=False, has_approving_review=True, segregation_evidenced=False
        )
        assert outcomes["SOC2-CC8.1"] == "observed"
        assert outcomes["SSDF-PW.7"] == "observed"
        # Segregation still not evidenced → Art.17 stays process-only.
        assert outcomes["DORA-Art.17"] == "process-only"

    def test_segregation_makes_art17_observed(self) -> None:
        outcomes = _change_control_outcomes(
            has_pr=True, review_attested=False, has_approving_review=False, segregation_evidenced=True
        )
        assert outcomes["DORA-Art.17"] == "observed"
        # No approving review → CC8.1/PW.7 remain process-only.
        assert outcomes["SOC2-CC8.1"] == "process-only"
        assert outcomes["SSDF-PW.7"] == "process-only"

    def test_direct_change_is_not_observed(self) -> None:
        outcomes = _change_control_outcomes(
            has_pr=False, review_attested=False, has_approving_review=False, segregation_evidenced=False
        )
        assert set(outcomes.values()) == {"not-observed"}

    def test_only_valid_vocabulary_emitted(self) -> None:
        vocab = {"attested", "observed", "process-only", "not-observed"}
        for has_pr in (True, False):
            for review in (True, False):
                for appr in (True, False):
                    for seg in (True, False):
                        values = set(_change_control_outcomes(
                            has_pr=has_pr, review_attested=review,
                            has_approving_review=appr, segregation_evidenced=seg,
                        ).values())
                        assert values <= vocab
