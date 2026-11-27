"""Component 3 — control-map.md: observed controls → framework clauses (spec §4.3)."""

from __future__ import annotations

from kerbtap.build.common import control_evidence_counts, coverage_summary_lines
from kerbtap.collect.github import summarize_merge_gate
from kerbtap.honesty import INTEGRITY_DISCLAIMER
from kerbtap.model import EvidenceBundle

_SIGNAL_LABELS = {
    "review_attested": "changes with a structured Governor-Review PASS trailer bound to a "
                       "review artifact (a free-text note alone does not count)",
    "feature_branch_pr": "changes landed via a pull request (merge or squash)",
    "segregation": "changes where segregation of duties is evidenced (the recorded merge "
                   "actor differs from the recorded author); a review trailer is not "
                   "identity-bound and does not attest segregation",
    "enforcing_hook": "enforcing deny-mode review hook present (1 = present)",
    "audit_trail": "git-versioned decision log present (1 = present)",
}


def _evidence_line(bundle: EvidenceBundle, signal: str, counts: dict[str, int]) -> str:
    """The per-control evidence sentence — tri-state signals get honest prose, not 0s."""
    if signal == "merge_gate":
        return f"Merge gate {summarize_merge_gate(bundle.branch_protection)}."
    if signal == "codeowners":
        if bundle.codeowners is not None:
            return (f"CODEOWNERS file at {bundle.codeowners['path']} defining "
                    f"{bundle.codeowners['rule_count']} ownership rule(s); contents "
                    f"deliberately not quoted.")
        return "No CODEOWNERS file found at any standard location."
    count = counts.get(signal, 0)
    label = _SIGNAL_LABELS.get(signal, signal)
    return f"{count} {label}."


def render(bundle: EvidenceBundle) -> str:
    clauses = bundle.control_clauses
    counts = control_evidence_counts(bundle)
    caveats: dict[str, str] = clauses.get("clause_caveats", {})

    out: list[str] = []
    out.append("# Control Map")
    out.append("")
    out.append(f"*Repository:* `{bundle.scope.repository}` — commit range "
               f"`{bundle.scope.commit_range}`, as of {bundle.scope.as_of}.")
    out.append("")
    out.append(INTEGRITY_DISCLAIMER)
    out.append("")
    out.append("This map records where the repository's **observed** controls line up with the "
               "**authorship-agnostic** control clauses that auditors and reviewers test today. "
               "It is a mapping, not an audit opinion or a legal-compliance certification.")
    out.append("")
    out.append("## Honest framing")
    out.append("")
    out.append(clauses.get("honesty_note", ""))
    out.append("")

    out.append("## Observed controls → clauses")
    out.append("")
    for control in clauses.get("controls", []):
        signal = control.get("signal", "")
        out.append(f"### {control['observed_control']}")
        out.append("")
        out.append(f"- **Evidence in this repo:** {_evidence_line(bundle, signal, counts)}")
        out.append(f"- **Detected by:** {control.get('detected_by', '')}")
        out.append("")
        out.append("| Framework | Clause | Control intent (cited, not restated) |")
        out.append("|---|---|---|")
        for clause in control.get("clauses", []):
            out.append(
                f"| {clause['framework']} | {clause['clause']} | {clause['intent']} |"
            )
        out.append("")
        # Attach any framework caveats relevant to this control's clauses.
        frameworks = {c["framework"] for c in control.get("clauses", [])}
        for framework in sorted(frameworks):
            if framework in caveats:
                out.append(f"> Caveat ({framework}): {caveats[framework]}")
        out.append("")

    out.append("## Per-clause coverage across assessed changes")
    out.append("")
    out.extend(coverage_summary_lines(bundle))
    out.append("")
    out.append("## Sourcing")
    out.append("")
    out.append(clauses.get("source", ""))
    out.append("")
    return "\n".join(out)
