"""Typed data model for the Evidence Pack.

These dataclasses are the in-memory record the collectors populate and the builders
render. They deliberately mirror the manifest schema in the product spec
(spec §4.2): references and fingerprints, a review record, policy-gate references,
control outcomes, and per-item chain-of-custody
metadata — and, just as deliberately, NO cryptographic-signature field.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Commit:
    """A single git commit, as reported by ``git log``."""

    sha: str
    author_name: str
    author_email: str
    authored_date: str  # ISO-8601, as reported by git
    subject: str
    body: str
    parents: list[str]

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def short_sha(self) -> str:
        return self.sha[:12]


@dataclass(slots=True)
class PullRequest:
    """A pull request, as reported by the GitHub CLI (optional enhancement)."""

    number: int
    title: str
    body: str
    state: str
    merged_at: str | None
    head_ref: str | None


@dataclass(slots=True)
class ReviewRecord:
    """The review evidence detected for a change (spec §4.2 'Review record').

    ``verification`` is the honest strength of the signal: ``integrity-checked`` (a
    structured PASS trailer whose artifact resolves in the commit's tree and covers the
    change — the only state that sets ``attested``; never called *verified*),
    ``claimed`` (a free-text, incomplete or non-covering review claim),
    ``override`` (an explicit bypass of the review gate — ``GOVERNOR_REVIEWED`` or the
    repository's renamed variable — with no artifact), or ``none``.
    """

    attested: bool
    evidence: str
    reviewer: str
    verification: str = "none"
    # For an integrity-checked record only: "confirmed" (its artifact names the paths the
    # change must cover, at the committed content) or "not-recorded" (its artifact names no
    # reviewed files; the pack cannot tell when it was written).
    scope: str = ""
    # For an integrity-checked record only: the review-contract hash its artifact recorded
    # ("" when none) and "current", "differs" or "not-recorded" against the current contract.
    contract: str = ""
    contract_state: str = ""


@dataclass(slots=True)
class PolicyGate:
    """A policy/gate reference and its recorded outcome (spec §4.2)."""

    gate: str
    applies: bool
    outcome: str  # "pass" | "not-applicable" | "no-recorded-evidence" | "undetermined"
    # Why this outcome, in kerbtap's own fixed wording (never repo-sourced text).
    basis: str = ""


@dataclass(slots=True)
class Custody:
    """Chain-of-custody metadata for a change (spec §4.2, §5.3)."""

    source_system: str
    retrieved_ts: str
    packaged_by: str


@dataclass(slots=True)
class Change:
    """One assessed change — the per-change record in the manifest ``changes[]``."""

    change_id: str
    subject: str
    source_refs: dict[str, str | int | None]
    author: dict[str, str]
    ai_assisted: dict[str, object]
    review: ReviewRecord
    policy_gates: list[PolicyGate]
    jira_refs: list[str]
    control_outcomes: dict[str, str]
    custody: Custody
    landed_via: str  # "merge-pr" | "squash-pr" | "pr-branch-commit" | "direct-to-main"
    pull_request: dict[str, object] | None = None  # enriched PR evidence (mainline change only)


@dataclass(slots=True)
class SourceSystem:
    """A named source system the evidence was drawn from (spec §5.3, §5.5).

    ``present`` is tri-state: ``True`` / ``False`` / ``None`` — ``None`` means the
    state could not be determined this run (e.g. an API not readable with the current
    credentials). Rendering ``None`` as "no" would be a false absence claim.
    """

    name: str
    kind: str
    present: bool | None
    evidence: str


@dataclass(slots=True)
class Scope:
    """The assessed scope printed on the pack (spec §2, §5.1)."""

    repository: str
    repo_path: str
    commit_range: str
    as_of: str
    assessed_commits: int
    assessed_prs: int
    merge_pr_count: int
    squash_pr_count: int
    pr_branch_commit_count: int
    direct_to_main_count: int


@dataclass(slots=True)
class EvidenceBundle:
    """Everything the collectors gathered — the builders' single input."""

    scope: Scope
    changes: list[Change]
    source_systems: list[SourceSystem]
    repo_level_attestation: dict[str, object]
    control_clauses: dict[str, object]
    review_attested_count: int
    gh_available: bool
    ai_orchestrated: bool = False  # True only for governed AI-orchestration; never hardcoded
    ai_state: str = "none"  # "governed" | "ai-assisted-ungoverned" | "none"
    ai_has_claude_dir: bool = False  # a .claude/ directory exists (any contents)
    ai_has_governance: bool = False  # governance scaffold and/or dogfood log present
    ai_trailer_count: int = 0  # distinct commits carrying >=1 AI co-author trailer
    internal: bool = False  # True = include absolute path + author emails (dogfood use)
    dogfood_observations: list[dict[str, str]] = field(default_factory=list)
    # Local gate log (governance.read_gate_log): parsable events and the unparsed-line count.
    gate_events: dict[str, object] = field(default_factory=dict)
    # Merge-gate record (github.collect_branch_protection): state present/absent/
    # not-collected + readable parameters. CURRENT configuration only — repo-level
    # control evidence, never per-change historical evidence.
    branch_protection: dict[str, object] = field(default_factory=dict)
    # CODEOWNERS record (governance.detect_codeowners): path + rule count, or None.
    codeowners: dict[str, object] | None = None
    # Installed enforcement layer at assessment time (enforcement_layer.observe).
    enforcement_layer: dict[str, object] = field(default_factory=dict)
