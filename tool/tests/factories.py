"""Small builders for test fixtures — real-shaped commits and a minimal bundle."""

from __future__ import annotations

from kerbtap.collect.governance import load_control_clauses
from kerbtap.model import (
    Change,
    Commit,
    Custody,
    EvidenceBundle,
    PolicyGate,
    ReviewRecord,
    Scope,
    SourceSystem,
)


def make_commit(
    *,
    sha: str = "a" * 40,
    subject: str = "Some change",
    body: str = "",
    parents: list[str] | None = None,
    author_name: str = "Ada Example",
    author_email: str = "ada@example.com",
    authored_date: str = "2026-07-06T16:00:00+02:00",
) -> Commit:
    return Commit(
        sha=sha,
        author_name=author_name,
        author_email=author_email,
        authored_date=authored_date,
        subject=subject,
        body=body,
        parents=parents if parents is not None else [],
    )


# A real deliverable commit body shape from this repo (adversarial review + PASS).
REVIEWED_BODY = (
    "evidence-pack-spec.md — N1 Evidence Pack artifact spec.\n\n"
    "Adversarial review: architect agent, C1-C7 with 7A-honesty as priority dimension. "
    "First pass NEEDS-CHANGES on one MAJOR; all fixed; re-review PASS. "
    "Closes PROJ-138."
)


def _make_change(
    change_id: str,
    *,
    landed_via: str,
    review_attested: bool,
    outcomes: dict[str, str],
    pull_request: dict | None = None,
) -> Change:
    return Change(
        change_id=change_id,
        subject=f"Change {change_id}",
        source_refs={
            "commit_sha": change_id * 4,
            "commit_sha_role": "content pointer / fingerprint (NOT tamper-evidence)",
            "pr_number": 1,
            "branch": "feat/x",
        },
        author={"name": "Ada Example", "recorded_as": "author of record (git)"},
        ai_assisted={"value": True, "basis": "repo-level inference"},
        review=ReviewRecord(
            attested=review_attested,
            evidence="Structured review trailer: PASS by architect, artifact sha256:<...>. "
            "Re-checkable with `governor review verify`."
            if review_attested
            else "No recorded review evidence in the commit body.",
            reviewer="architect" if review_attested else "none recorded",
            verification="integrity-checked" if review_attested else "none",
        ),
        policy_gates=[PolicyGate(gate="precommit_review_guard", applies=True, outcome="pass")],
        jira_refs=["PROJ-138"],
        control_outcomes=outcomes,
        custody=Custody(source_system="git", retrieved_ts="2026-07-06T14:00:00+00:00", packaged_by="governor v0.1.0"),
        landed_via=landed_via,
        pull_request=pull_request,
    )


def make_bundle() -> EvidenceBundle:
    """A small, deterministic EvidenceBundle for rendering/honesty tests."""
    changes = [
        _make_change(
            "aaa",
            landed_via="merge-pr",
            review_attested=True,
            outcomes={
                "ISO-A.8.32": "observed", "DORA-Art.17": "observed",
                "SOC2-CC8.1": "attested", "SSDF-PW.7": "attested",
            },
            pull_request={
                "number": 34,
                "state": "MERGED",
                "base_branch": "main",
                "head_branch": "feat/x",
                "author_login": "author-x",
                "merged_by": "owner-y",
                "merge_commit": "abc123",
                "reviews": [],
                "reviews_note": "recorded review is in the PR/commit body.",
                "body_excerpt": "Deliverable PR. Architect review PASS.",
                "collection": "gh pr view",
                "merge_method_inferred": "merge-commit",
            },
        ),
        _make_change(
            "bbb",
            landed_via="squash-pr",
            review_attested=False,
            outcomes={
                "ISO-A.8.32": "observed", "DORA-Art.17": "process-only",
                "SOC2-CC8.1": "process-only", "SSDF-PW.7": "process-only",
            },
        ),
        _make_change(
            "ccc",
            landed_via="direct-to-main",
            review_attested=False,
            outcomes={
                "ISO-A.8.32": "not-observed", "DORA-Art.17": "not-observed",
                "SOC2-CC8.1": "not-observed", "SSDF-PW.7": "not-observed",
            },
        ),
    ]
    scope = Scope(
        repository="governor",
        repo_path="/repo/governor",
        commit_range="ccc..aaa",
        as_of="2026-07-06T14:00:00+00:00",
        assessed_commits=3,
        assessed_prs=2,
        merge_pr_count=1,
        squash_pr_count=1,
        pr_branch_commit_count=0,
        direct_to_main_count=1,
    )
    source_systems = [
        SourceSystem("git version control", "version-control", True, "git log"),
        SourceSystem("Pre-commit review gate", "enforcing-hook", True, "deny-mode hook (.claude/hooks/...)"),
        SourceSystem("Decision log (git-versioned)", "audit-trail", True,
                     "A dated decision log versioned in git; append-only is a documented convention."),
        SourceSystem("Dogfood log (git-versioned)", "process-record", True,
                     "A dated process-observation log versioned in git."),
    ]
    return EvidenceBundle(
        scope=scope,
        changes=changes,
        source_systems=source_systems,
        repo_level_attestation={
            "ai_orchestrated": True,
            "basis": "Repo-level inference from the dogfood log and .claude/ scaffold; not per-commit binding.",
            "note": "repo-level, not per-commit",
        },
        control_clauses=load_control_clauses(),
        review_attested_count=1,
        gh_available=False,
        ai_orchestrated=True,
        dogfood_observations=[
            {"id": "O4", "headline": "The self-attestation bypass leaves no evidence artifact."},
            {"id": "O8", "headline": "Sprint lifecycle never started; done off merged PRs."},
            {"id": "O11", "headline": "The review attestation is not the merge authorization."},
            {"id": "O12", "headline": "Merge tension closed by a verbal grant, not the attestation."},
            {"id": "O13", "headline": "Decision subtasks mislabeled and orphaned."},
        ],
    )
