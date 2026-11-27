"""Exception-log section 2 and the report's claim count each state their population."""

from __future__ import annotations

import dataclasses

from kerbtap.build import exception_log, report
from kerbtap.model import PolicyGate
from tests.factories import make_bundle


def test_section2_states_gated_scope() -> None:
    text = exception_log.render(make_bundle())
    assert "counts only changes that the review gate applied to" in text


def test_ungated_claim_is_scoped_in_report() -> None:
    bundle = make_bundle()
    claimed = dataclasses.replace(
        bundle.changes[1],
        review=dataclasses.replace(bundle.changes[1].review, verification="claimed"),
        policy_gates=[PolicyGate(gate="precommit_review_guard", applies=False, outcome="not-applicable")],
    )
    assert not any(g.applies for g in claimed.policy_gates)
    bundle = dataclasses.replace(bundle, changes=[bundle.changes[0], claimed, bundle.changes[2]])
    assert all(g.outcome != "no-recorded-evidence" for c in bundle.changes for g in c.policy_gates)
    assert "None found — every change that touched a path the review gate guarded" in exception_log.render(bundle)
    assert "1 of all assessed changes, gated or not," in report.render(bundle)
