"""Unit tests for git commit-body signal parsing."""

from __future__ import annotations

import pytest

from kerbtap.collect.git import (
    ticket_keys,
    classify_landing,
    detect_review,
    extract_branch,
    extract_jira_refs,
    extract_pr_number,
)
from tests.factories import REVIEWED_BODY, make_commit

# A commit body carrying a well-formed verified review trailer.
_VERIFIED_BODY = (
    "evidence-pack-spec.md — N1 spec.\n\n"
    "Governor-Review: PASS\n"
    "Governor-Reviewer: architect\n"
    f"Governor-Review-Ref: sha256:{'a' * 64}"
)


class TestDetectReview:
    def test_structured_trailer_pass_is_attested_verified(self) -> None:
        signal = detect_review(make_commit(body=_VERIFIED_BODY))
        assert signal.attested is True
        assert signal.verification == "integrity-checked"
        assert signal.reviewer == "architect"
        assert signal.ref == f"sha256:{'a' * 64}"

    def test_free_text_prose_pass_is_claimed_not_attested(self) -> None:
        # The exact spoofable pattern the trailer replaces: prose alone never attests.
        signal = detect_review(make_commit(body=REVIEWED_BODY))
        assert signal.attested is False
        assert signal.verification == "claimed"

    def test_governor_reviewed_override_is_recorded_not_attested(self) -> None:
        signal = detect_review(make_commit(body="Landed with GOVERNOR_REVIEWED=1 bypass."))
        assert signal.attested is False
        assert signal.verification == "override"

    def test_pass_trailer_without_valid_ref_is_claimed(self) -> None:
        body = "x\n\nGovernor-Review: PASS\nGovernor-Reviewer: architect\nGovernor-Review-Ref: nope"
        signal = detect_review(make_commit(body=body))
        assert signal.attested is False
        assert signal.verification == "claimed"

    def test_empty_body_is_none(self) -> None:
        signal = detect_review(make_commit(body=""))
        assert signal.attested is False
        assert signal.verification == "none"


@pytest.mark.parametrize("key", ["TASK-208", "PROJ-1", "INLINE-004", "AB2-17", "MY_KEY-3"])
def test_ticket_keys_accepts_shape(key: str) -> None:
    assert ticket_keys(f"fixes {key}.") == [key]


@pytest.mark.parametrize(
    "text",
    ["SHA-256", "UTF-8", "ISO-8601", "RFC-2119", "CVE-2024-1234", "HTTP-2", "proj-9", "X-1"],
)
def test_ticket_keys_refuses(text: str) -> None:
    assert ticket_keys(f"uses {text} here") == []


def test_ticket_keys_hash_names_in_a_commit_body() -> None:
    commit = make_commit(subject="hash", body="uses SHA-256 over UTF-8 bytes")
    assert extract_jira_refs(commit) == []


class TestExtractJiraRefs:
    def test_extracts_from_subject_and_body(self) -> None:
        commit = make_commit(subject="R5 dossier (PROJ-36)", body="Closes PROJ-36. See PROJ-42.")
        assert extract_jira_refs(commit) == ["PROJ-36", "PROJ-42"]

    def test_no_refs(self) -> None:
        assert extract_jira_refs(make_commit(subject="chore: tidy", body="")) == []

    def test_dedupes_preserving_order(self) -> None:
        commit = make_commit(subject="PROJ-138", body="PROJ-138 again, then PROJ-72")
        assert extract_jira_refs(commit) == ["PROJ-138", "PROJ-72"]


class TestExtractPrNumber:
    def test_merge_commit_subject(self) -> None:
        commit = make_commit(subject="Merge pull request #34 from org/deliverable/spec")
        assert extract_pr_number(commit) == 34

    def test_squash_commit_subject(self) -> None:
        commit = make_commit(subject="R5 — cost-inputs dossier (Sprint 3, PROJ-36) (#17)")
        assert extract_pr_number(commit) == 17

    def test_no_pr_reference(self) -> None:
        assert extract_pr_number(make_commit(subject="Rename product to Governor")) is None


class TestExtractBranch:
    def test_merge_branch(self) -> None:
        commit = make_commit(subject="Merge pull request #34 from org/deliverable/evidence-pack-spec")
        assert extract_branch(commit) == "deliverable/evidence-pack-spec"

    def test_no_branch(self) -> None:
        assert extract_branch(make_commit(subject="direct commit")) is None


class TestClassifyLanding:
    def test_merge_pr(self) -> None:
        commit = make_commit(subject="Merge pull request #34 from x/y", parents=["p1", "p2"])
        assert classify_landing(commit) == "merge-pr"

    def test_squash_pr(self) -> None:
        commit = make_commit(subject="R5 dossier (#17)")
        assert classify_landing(commit) == "squash-pr"

    def test_direct_to_main_when_on_first_parent(self) -> None:
        commit = make_commit(sha="d" * 40, subject="Founding commit: product vision")
        assert classify_landing(commit, {"d" * 40}) == "direct-to-main"

    def test_direct_to_main_without_first_parent_info(self) -> None:
        commit = make_commit(subject="Founding commit: product vision")
        assert classify_landing(commit) == "direct-to-main"

    def test_feature_branch_commit_not_on_first_parent_is_pr_branch(self) -> None:
        # A reviewed deliverable commit that entered via a merge PR (not on mainline)
        # must NOT be mislabeled direct-to-main.
        commit = make_commit(sha="f" * 40, subject="evidence-pack-spec.md — N1 spec")
        assert classify_landing(commit, {"m" * 40}) == "pr-branch-commit"


@pytest.mark.parametrize(
    "subject,expected",
    [
        ("Merge pull request #12 from a/b", 12),
        ("Some deliverable (Sprint 6, PROJ-49) (#32)", 32),
        ("no pr here", None),
    ],
)
def test_pr_number_parametrized(subject: str, expected: int | None) -> None:
    assert extract_pr_number(make_commit(subject=subject)) == expected
