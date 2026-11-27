"""Component 5 — remediation-plan.md: gaps → prioritized, buildable fixes (spec §4.5).

Recommendations are conditioned on what the tool actually found. Dogfood-specific
findings are only surfaced when a dogfood log is present; otherwise the plan draws solely
on evidence-derived gaps (direct-to-main commits, missing review gate) so it never asserts
a gap the assessed repository does not have.
"""

from __future__ import annotations

from kerbtap.build.common import is_present
from kerbtap.honesty import INTEGRITY_DISCLAIMER
from kerbtap.model import EvidenceBundle

# Curated remediations for KNOWN self-reported observation shapes. A curated story is
# asserted only when the observation id matches AND the parsed headline carries ALL of
# that story's distinctive tokens — a single common word ("merge", "evidence") is not
# enough, so a foreign repo whose "O4" means something entirely different never inherits
# this repo-of-origin's story by keyword coincidence. The gap text names only the ids
# that actually matched. Observations matching nothing are surfaced through one generic
# self-reported item instead (never dropped silently).
_DOGFOOD_REMEDIATIONS: tuple[dict[str, object], ...] = (
    {
        "obs_ids": ("O4",),
        "tokens": ("bypass", "evidence artifact"),
        "priority": "P1",
        "gap": "Review attestation is a bare env-var bypass with no recorded evidence artifact (dogfood {ids}).",
        "fix": "Record the reviewer verdict as a content-addressed artifact and bind the commit to it "
               "with a structured `Governor-Review` trailer (`kerbtap review record`), re-checkable "
               "via `kerbtap review verify`, so a change carries a verifiable attestation rather than "
               "a bare assertion. Remaining gap: identity binding (a signing key the committer does not "
               "hold) and a server-side merge gate that blocks on it.",
        "clause": "SOC 2 CC8.1; ISO A.8.32",
    },
    {
        "obs_ids": ("O11", "O12"),
        "tokens": ("attestation", "merge"),
        "priority": "P1",
        "gap": "The review attestation is not the merge authorization; merges swing on a verbal grant "
               "(dogfood {ids}).",
        "fix": "Make the recorded architect-PASS token the machine-checkable merge authorization, so a "
               "merge gate can verify the approval rather than requiring a human natural-language grant.",
        "clause": "DORA Art. 17 (segregation / approval)",
    },
    {
        "obs_ids": ("O8",),
        "tokens": ("sprint", "start", "done"),
        "priority": "P2",
        "gap": "No control ensures the recorded sprint/process state matches reality; work was marked "
               "done against never-started sprints (dogfood {ids}).",
        "fix": "Add a sprint-lifecycle control: a ritual (ideally a hook/report) that refuses 'done' "
               "bookkeeping against a sprint still in `future` state, and flags issues transitioned to "
               "Done while their sprint was never started.",
        "clause": "DORA Art. 33 (audit trail integrity)",
    },
    {
        "obs_ids": ("O13",),
        "tokens": ("subtask", "mislabeled"),
        "priority": "P3",
        "gap": "Task-tracker hygiene: mis-parented/mislabeled decision subtasks leave the audit trail "
               "carrying mislabeled records (dogfood {ids}).",
        "fix": "Have decision-subtask templates inherit the parent decision id, and add a control that "
               "flags brief/ratification subtasks left open once the parent decision is logged Decided.",
        "clause": "DORA Art. 33 (audit trail integrity)",
    },
)


def _dogfood_items(observations: list[dict[str, str]]) -> list[dict[str, str]]:
    """Curated items where id AND all distinctive tokens match; generic item for the rest."""
    headlines = {o["id"]: o["headline"].lower() for o in observations}
    items: list[dict[str, str]] = []
    matched_ids: set[str] = set()
    for rem in _DOGFOOD_REMEDIATIONS:
        hits = [
            obs_id
            for obs_id in rem["obs_ids"]  # type: ignore[union-attr]
            if obs_id in headlines
            and all(token in headlines[obs_id] for token in rem["tokens"])  # type: ignore[union-attr]
        ]
        if hits:
            item = {k: str(rem[k]) for k in ("priority", "gap", "fix", "clause")}
            item["gap"] = item["gap"].format(ids="/".join(hits))
            items.append(item)
            matched_ids.update(hits)
    unmatched = [o for o in observations if o["id"] not in matched_ids]
    if unmatched:
        items.append({
            "priority": "P2",
            "gap": f"The repository self-reports {len(unmatched)} process observation(s) in its "
                   f"dogfood/process log that this plan does not map to a curated fix.",
            "fix": "Review each self-reported observation in the repository's own log and convert "
                   "every governance-relevant one into a tracked remediation with an owner.",
            "clause": "DORA Art. 33 (audit trail integrity)",
        })
    return items


def _remediations(bundle: EvidenceBundle) -> list[dict[str, str]]:
    """Build the remediation list from actually-detected evidence."""
    items: list[dict[str, str]] = []
    scope = bundle.scope

    # Evidence-derived gaps that apply to any repository, from the assessed data.
    if scope.direct_to_main_count:
        items.append({
            "priority": "P1",
            "gap": f"{scope.direct_to_main_count} commit(s) landed directly on the mainline, "
                   f"outside any pull-request gate.",
            "fix": "Protect the default branch and require all changes to land via pull request, so "
                   "each change is reviewable and its approval and merge actor are recorded.",
            "clause": "ISO A.8.32; DORA Art. 17",
        })
    # Review-gate gap — conditioned on BOTH repo-local hooks and the GitHub merge gate
    # actually read this run. A repo whose branch protection already requires review has
    # no such gap; an unreadable state is hedged, never asserted as absence.
    if not is_present(bundle, "Pre-commit review gate"):
        bp = bundle.branch_protection
        # A missing/malformed record hedges as not-collected — never a false absence.
        bp_state = bp.get("state") or "not-collected"
        requires_review = bp.get("requires_review")
        if bp_state == "present" and requires_review is True:
            pass  # an enforcing merge gate exists — no gap to assert
        elif bp_state == "present" and requires_review is False:
            items.append({
                "priority": "P2",
                "gap": "A merge gate (branch protection/ruleset) exists on the default branch "
                       "but does not require an approving review, so unreviewed changes can "
                       "still merge (current configuration).",
                "fix": "Raise the gate's required approving reviews to at least 1 (and/or "
                       "require code-owner review) so that unreviewed changes cannot merge.",
                "clause": "SOC 2 CC8.1; US SSDF PW.7",
            })
        elif bp_state == "present":
            items.append({
                "priority": "P2",
                "gap": "The default branch is marked protected, but the protection "
                       "configuration was not readable with the current credentials, so "
                       "whether an approving review is required could not be verified.",
                "fix": "Verify (with repository-admin access) that the gate requires at least "
                       "one approving review before merge, and record that configuration.",
                "clause": "SOC 2 CC8.1; ISO A.8.32",
            })
        else:
            hedge = (
                " Branch protection was not readable this run, so only repo-local gates "
                "could be checked." if bp_state == "not-collected"
                else " The default branch is not protected."
            )
            items.append({
                "priority": "P2",
                "gap": "No enforcing pre-commit / pre-merge review gate was detected; review "
                       "is by convention only." + hedge,
                "fix": "Add a deny-by-default review gate (branch protection requiring an "
                       "approving review, or a pre-commit/CI check) so that unreviewed changes "
                       "cannot merge.",
                "clause": "SOC 2 CC8.1; ISO A.8.32",
            })
    if bundle.review_attested_count < scope.assessed_commits:
        items.append({
            "priority": "P2",
            "gap": f"Only {bundle.review_attested_count} of {scope.assessed_commits} changes carry a "
                   f"recorded review verdict in their evidence.",
            "fix": "Record a per-change review verdict (reviewer, outcome) as a durable artifact — a "
                   "PR review, a CI check result, or a review-record file — so approval is evidenced "
                   "per change rather than assumed.",
            "clause": "SOC 2 CC8.1; US SSDF PW.7",
        })

    # Dogfood-specific gaps only for observations the assessed repo actually reports
    # (id + headline keyword must both match — never asserted from log presence alone).
    items.extend(_dogfood_items(bundle.dogfood_observations))
    return items


def render(bundle: EvidenceBundle) -> str:
    items = _remediations(bundle)
    out: list[str] = []
    out.append("# Remediation Plan")
    out.append("")
    out.append(f"*Repository:* `{bundle.scope.repository}` — commit range "
               f"`{bundle.scope.commit_range}`.")
    out.append("")
    out.append(INTEGRITY_DISCLAIMER)
    out.append("")
    out.append("Gaps found in the assessment, each with a concrete, buildable fix and a priority. "
               "Every gap below is derived from evidence actually detected in this repository.")
    out.append("")
    if not items:
        out.append("No remediation gaps were derived from the assessed evidence.")
        out.append("")
        return "\n".join(out)

    out.append("| Priority | Gap | Recommended fix | Related control |")
    out.append("|---|---|---|---|")
    for item in items:
        out.append(f"| {item['priority']} | {item['gap']} | {item['fix']} | {item['clause']} |")
    out.append("")
    out.append("### Top priorities")
    out.append("")
    for item in items:
        if item["priority"] == "P1":
            out.append(f"- **{item['priority']} — {item['gap']}** {item['fix']}")
    out.append("")
    return "\n".join(out)


def top_priorities(bundle: EvidenceBundle) -> list[str]:
    """P1 remediation headlines, reused by the executive summary."""
    return [item["gap"] for item in _remediations(bundle) if item["priority"] == "P1"]
