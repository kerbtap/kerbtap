"""EXS-001: the headline states landing and review only through counts."""

from __future__ import annotations

from dataclasses import replace

from kerbtap.build import executive_summary
from tests.factories import make_bundle

FIXED_CLAIM = "reviewed and land via pull request"


def _headline(bundle) -> str:
    text = executive_summary.render(bundle)
    return text.split("## Headline finding", 1)[1].split("## Control-coverage summary", 1)[0]


def test_headline_with_governance_states_counts_not_a_fixed_claim():
    bundle = replace(make_bundle(), ai_has_governance=True)
    headline = _headline(bundle)
    assert FIXED_CLAIM not in headline
    assert "2 of 3 commits landed through 2 pull requests" in headline
    assert "1 change(s) carry an integrity-checked" in headline
    assert "1 commit(s) landed directly on the mainline" in headline


def test_headline_all_direct_states_counts_not_a_fixed_claim():
    base = make_bundle()
    scope = replace(
        base.scope,
        assessed_prs=0,
        merge_pr_count=0,
        squash_pr_count=0,
        pr_branch_commit_count=0,
        direct_to_main_count=3,
    )
    bundle = replace(base, scope=scope, ai_has_governance=True, review_attested_count=0)
    headline = _headline(bundle)
    assert FIXED_CLAIM not in headline
    assert "0 of 3 commits landed through" in headline
    assert "no change carries" in headline
    assert "3 commit(s) landed directly on the mainline" in headline
