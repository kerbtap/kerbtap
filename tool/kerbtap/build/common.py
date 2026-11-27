"""Shared computation and rendering helpers for the six pack components."""

from __future__ import annotations

from collections import Counter

from kerbtap.model import Change, EvidenceBundle

# Human-readable names for the clause ids used in per-change control outcomes.
CLAUSE_NAMES: dict[str, str] = {
    "SOC2-CC8.1": "SOC 2 CC8.1 (change authorize/approve)",
    "ISO-A.8.32": "ISO/IEC 27001:2022 A.8.32 (change management)",
    "DORA-Art.17": "DORA (EU) 2024/1774 Art. 17 (documented, approved, segregated ICT change)",
    "SSDF-PW.7": "US SSDF (NIST SP 800-218) PW.7 (code review)",
    "DORA-Art.33": "DORA (EU) 2024/1774 Art. 33 (change-event audit trail)",
}


def clause_coverage(bundle: EvidenceBundle) -> dict[str, Counter[str]]:
    """Aggregate per-change control outcomes into a pass/partial/absent tally per clause."""
    coverage: dict[str, Counter[str]] = {}
    for change in bundle.changes:
        for clause_id, outcome in change.control_outcomes.items():
            coverage.setdefault(clause_id, Counter())[outcome] += 1
    return coverage


def control_evidence_counts(bundle: EvidenceBundle) -> dict[str, int]:
    """Count the evidence supporting each observed control (by its signal)."""
    changes = bundle.changes
    pr_changes = sum(
        1 for c in changes if c.landed_via in {"merge-pr", "squash-pr", "pr-branch-commit"}
    )
    # Segregation is counted per change actually EVIDENCED (Art.17 attested/observed:
    # recorded merge actor != author, or a recorded review PASS) — never the raw PR
    # count, which would overclaim approvals that were not collected.
    segregation_evidenced = sum(
        1 for c in changes
        if c.control_outcomes.get("DORA-Art.17") in {"attested", "observed"}
    )
    return {
        "review_attested": bundle.review_attested_count,
        "feature_branch_pr": pr_changes,
        "segregation": segregation_evidenced,
        "enforcing_hook": _present(bundle, "Pre-commit review gate"),
        "audit_trail": _present(bundle, "Decision log (git-versioned)"),
    }


def pr_checks(bundle: EvidenceBundle) -> list[tuple[Change, dict]]:
    """``(change, checks)`` for each pull request's mainline change that carries a checks record."""
    out = []
    for c in bundle.changes:
        checks = (c.pull_request or {}).get("checks")
        if isinstance(checks, dict):
            out.append((c, checks))
    return out


def is_present(bundle: EvidenceBundle, name: str) -> bool:
    """Whether a named source system was actually detected as present in the repo."""
    return any(s.name == name and s.present for s in bundle.source_systems)


def ai_signal_fragments(bundle: EvidenceBundle) -> list[str]:
    """Headline fragments naming ONLY the true (ungoverned) AI signals — never an absent one.

    Derived compositionally from the detected-signal flags so the one-line headline can
    never assert a signal (e.g. ``.claude/``) that the assessment paragraph does not.
    """
    fragments: list[str] = []
    if bundle.ai_has_claude_dir:
        fragments.append("AI coding tooling configured (`.claude/`)")
    if bundle.ai_trailer_count:
        fragments.append(
            f"AI-assisted authorship evidenced ({bundle.ai_trailer_count} commit(s) carry "
            f"AI co-author trailers)"
        )
    return fragments


def ai_scope_line(bundle: EvidenceBundle) -> str:
    """The one-line 'Delivery workflow' headline, composed from the detected signals."""
    if bundle.ai_has_governance:
        return "governed AI-orchestration evidenced (repo-level assessment, below)"
    fragments = ai_signal_fragments(bundle)
    if fragments:
        return "; ".join(fragments) + ", governance not evidenced (assessment below)"
    return "no AI tooling or orchestration evidence detected (assessment below)"


def _present(bundle: EvidenceBundle, name: str) -> int:
    return 1 if is_present(bundle, name) else 0


# Honest definitions of the per-change outcome vocabulary, printed under every table.
OUTCOME_LEGEND = (
    "*Outcome vocabulary (differentiated per clause from actual evidence): "
    "**attested** = the change carries a structured `Governor-Review` PASS trailer bound "
    "to a review artifact by content hash (recorded by `kerbtap review record`, "
    "re-checkable with `kerbtap review verify`); a free-text review note alone is not "
    "attested; "
    "**observed** = the specific control artifact is evidenced — an approving GitHub review "
    "for CC8.1/PW.7, an approver≠author merge for Art.17, or a pull-request landing for "
    "A.8.32 (whose mechanism is the PR itself); "
    "**process-only** = landed via a pull-request process, but the specific control artifact "
    "(an approving review for CC8.1/PW.7, or approver≠author for Art.17) is not evidenced; "
    "**not-observed** = the change did not go through the mechanism at all (direct-to-main). "
    "A plain pull-request landing is not treated as per-change proof of approval.*"
)


def coverage_summary_lines(bundle: EvidenceBundle) -> list[str]:
    """Render a compact per-clause coverage summary as markdown table rows + legend."""
    coverage = clause_coverage(bundle)
    lines = [
        "| Control clause | Attested | Observed | Process-only | Not-observed |",
        "|---|---|---|---|---|",
    ]
    for clause_id, name in CLAUSE_NAMES.items():
        if clause_id not in coverage:
            continue
        tally = coverage[clause_id]
        lines.append(
            f"| {name} | {tally.get('attested', 0)} | {tally.get('observed', 0)} "
            f"| {tally.get('process-only', 0)} | {tally.get('not-observed', 0)} |"
        )
    lines.append("")
    lines.append(OUTCOME_LEGEND)
    return lines
