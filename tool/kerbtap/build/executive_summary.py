"""Component 6 — executive-summary.md: the one-page top line (spec §4.6).

This is the highest-risk surface for overclaiming, so the honest-scope disclaimer is
mandatory here, not optional.
"""

from __future__ import annotations

from kerbtap.build.common import ai_signal_fragments, coverage_summary_lines
from kerbtap.build.remediation import top_priorities
from kerbtap.honesty import HONEST_CLAIM, INTEGRITY_DISCLAIMER
from kerbtap.model import EvidenceBundle


def render(bundle: EvidenceBundle) -> str:
    scope = bundle.scope
    out: list[str] = []
    out.append("# Executive Summary")
    out.append("")
    out.append("## Scope")
    out.append("")
    out.append(f"Assessed the change process of `{scope.repository}` across "
               f"{scope.assessed_commits} commits (`{scope.commit_range}`) and "
               f"{scope.assessed_prs} pull requests, as of {scope.as_of}.")
    out.append("")

    out.append("## Headline finding")
    out.append("")
    pr_landed = scope.merge_pr_count + scope.squash_pr_count + scope.pr_branch_commit_count
    if bundle.ai_has_governance:
        lead = ("The repository shows evidence of governed AI-orchestrated delivery: governance "
                "tooling is present. The counts that follow show how its changes landed and how "
                "many carry an attested review. ")
    else:
        fragments = ai_signal_fragments(bundle)
        if fragments:
            lead = ("This repository shows " + "; ".join(fragments) + " but no evidence of governed "
                    "AI-orchestrated delivery; the assessment below reflects its git/GitHub change "
                    "process as recorded. ")
        else:
            lead = ("No AI tooling or orchestration evidence was detected in this repository; the "
                    "assessment below reflects its git/GitHub change process as recorded. ")
    if bundle.review_attested_count:
        review_clause = (f"{bundle.review_attested_count} change(s) carry an integrity-checked "
                         f"`Governor-Review` PASS trailer bound to a review artifact")
    else:
        review_clause = "no change carries an integrity-checked `Governor-Review` PASS trailer"
    out.append(f"{lead}{pr_landed} of {scope.assessed_commits} commits landed through "
               f"{scope.assessed_prs} pull requests; {review_clause}. "
               f"{scope.direct_to_main_count} commit(s) landed directly on the mainline — documented "
               f"below and in the exception log.")
    out.append("")

    out.append("## Control-coverage summary")
    out.append("")
    out.extend(coverage_summary_lines(bundle))
    out.append("")

    out.append("## Top remediation priorities")
    out.append("")
    priorities = top_priorities(bundle)
    if priorities:
        for gap in priorities:
            out.append(f"- {gap}")
    else:
        out.append("- No P1 remediation gaps were derived from the assessed evidence.")
    out.append("")

    out.append("## Trust-claim scope (printed on this page, mandatory)")
    out.append("")
    out.append(HONEST_CLAIM)
    out.append("")
    out.append(INTEGRITY_DISCLAIMER)
    out.append("")
    return "\n".join(out)
