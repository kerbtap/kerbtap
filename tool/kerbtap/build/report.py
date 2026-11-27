"""Component 1 — report.md: the executive-readable findings (spec §4.1)."""

from __future__ import annotations

from collections import Counter

from kerbtap.assemble import not_attested_list
from kerbtap.build.common import ai_scope_line, coverage_summary_lines, is_present, pr_checks
from kerbtap.collect.github import CHECKS_LIMITS, summarize_merge_gate
from kerbtap.honesty import HONEST_CLAIM, INTEGRITY_DISCLAIMER
from kerbtap.model import EvidenceBundle

_HOOK = "Pre-commit review gate"
_DECISION_LOG = "Decision log (git-versioned)"
_DOGFOOD_LOG = "Dogfood log (git-versioned)"
_CHECK_OUTCOMES = ("pass", "fail", "no-recorded-evidence", "undetermined")


def render(bundle: EvidenceBundle) -> str:
    scope = bundle.scope
    hook_present = is_present(bundle, _HOOK)
    decision_log_present = is_present(bundle, _DECISION_LOG)
    dogfood_present = is_present(bundle, _DOGFOOD_LOG)

    out: list[str] = []
    out.append("# AI SDLC Evidence Pack — Report")
    out.append("")
    out.append("## Assessed scope")
    out.append("")
    if bundle.internal:
        out.append(f"- **Repository:** `{scope.repository}` (`{scope.repo_path}`)")
    else:
        out.append(f"- **Repository:** `{scope.repository}`")
    out.append(f"- **Commit range:** `{scope.commit_range}`")
    out.append(f"- **As of:** {scope.as_of}")
    out.append(f"- **Assessed changes:** {scope.assessed_commits} commits; "
               f"{scope.assessed_prs} pull requests "
               f"({scope.merge_pr_count} merge-PR, {scope.squash_pr_count} squash-PR).")
    out.append(f"- **Delivery workflow:** {ai_scope_line(bundle)}.")
    out.append("")
    out.append("## Trust-claim scope, stated plainly")
    out.append("")
    out.append(HONEST_CLAIM)
    out.append("")
    out.append(INTEGRITY_DISCLAIMER)
    out.append("")

    out.append("## How this repository's change process maps to the attestation-gate pattern")
    out.append("")
    out.append("The pattern the pack assesses a process against is: a privileged action executes only "
               "after a policy check passes and a logged approval is issued (policy gate → logged "
               "approval → decision log → deny-by-default). Each bullet below reflects only what was "
               "actually detected in this repository.")
    out.append("")

    # Policy-checked bullet — conditioned on the review hook actually being present,
    # plus the GitHub merge gate (branch protection / rulesets) as read this run.
    gate_sentence = (f" GitHub merge gate (branch protection/rulesets, current "
                     f"configuration): {summarize_merge_gate(bundle.branch_protection)}.")
    unscoped = sum(1 for c in bundle.changes if c.review.attested and c.review.scope == "not-recorded")
    attested_clause = (f"{bundle.review_attested_count} change(s) carry an integrity-checked "
                       f"`Governor-Review` PASS trailer bound to a review artifact")
    if unscoped:
        attested_clause += (f" ({unscoped} of them name no reviewed files, so which files those "
                            f"reviews covered is not recorded)")
    differs = sum(1 for c in bundle.changes if c.review.attested and c.review.contract_state == "differs")
    no_contract = sum(1 for c in bundle.changes if c.review.attested and c.review.contract_state == "not-recorded")
    if differs:
        attested_clause += f" ({differs} of them were recorded under a different review contract)"
    if no_contract:
        attested_clause += f" ({no_contract} of them have no review contract recorded (contract not recorded))"
    unreadable = sum(
        1 for c in bundle.changes if c.review.attested and c.review.contract_state == "current contract unreadable"
    )
    if unreadable:
        attested_clause += (f" ({unreadable} of them could not be compared with the current review "
                            f"contract, because the current contract was unreadable)")
    if hook_present:
        out.append(f"- **Policy-checked:** a deny-mode pre-commit hook gates deliverable commits; "
                   f"{attested_clause}.{gate_sentence}")
    elif bundle.review_attested_count:
        out.append(f"- **Policy-checked:** no deny-mode pre-commit review gate found in this "
                   f"repository; however {attested_clause}.{gate_sentence}")
    else:
        out.append(f"- **Policy-checked:** no deny-mode pre-commit review gate found in this "
                   f"repository, and no change carries an integrity-checked `Governor-Review` PASS trailer.{gate_sentence}")

    # Logged bullet — claim only the logs that are actually present.
    logged: list[str] = []
    if decision_log_present:
        logged.append("a git-versioned decision log records governance decisions with their "
                      "PR/commit evidence (append-only is a documented convention, not an enforced "
                      "control — the underlying versioned record is git history)")
    if dogfood_present:
        logged.append(f"a dogfood log records process observations "
                      f"({len(bundle.dogfood_observations)} parsed)")
    if logged:
        out.append("- **Logged:** " + "; ".join(logged) + ".")
    else:
        out.append("- **Logged:** no git-versioned decision log or dogfood log found in this "
                   "repository.")

    # Reviewed-approval bullet — strictly evidence-derived: the merge actor is whatever
    # git/GitHub recorded per PR. A segregation-of-duties CONVENTION is never asserted
    # from the mere presence of a process log (that was a repo-of-origin assumption),
    # and the merged_by pointer is claimed only when PR details were actually collected.
    pr_landed = scope.merge_pr_count + scope.squash_pr_count + scope.pr_branch_commit_count
    if bundle.gh_available:
        segregation = ("the merge actor is recorded per PR whose details were collected "
                       "this run (see `pull_request.merged_by` in the manifest; PRs beyond "
                       "the enrichment cap carry git-only evidence); no deliberate "
                       "segregation-of-duties policy is asserted beyond what git/GitHub "
                       "records show")
    else:
        segregation = ("merge actors were not collected this run (no GitHub repository was read), so "
                       "nothing is asserted about who merged each PR")
    out.append(f"- **Reviewed approval before merge:** {pr_landed} of {scope.assessed_commits} "
               f"commits landed via pull request ({scope.assessed_prs} PRs: "
               f"{scope.merge_pr_count} merge-commit + {scope.squash_pr_count} squash-merge), "
               f"{segregation}.")

    # Check-runs bullet — per pull request's mainline change, from the publisher-attributed
    # check runs on its head commit (manifest `pull_request.checks`).
    checks = pr_checks(bundle)
    if checks:
        tally = Counter(str(block.get("outcome")) for _, block in checks)
        parts = ", ".join(f"{tally[o]} {o}" for o in _CHECK_OUTCOMES if tally[o])
        out.append(f"- **Check runs on the pull-request head commit:** {len(checks)} pull request(s) "
                   f"assessed: {parts} (see `pull_request.checks` in the manifest, which names the "
                   f"GitHub App that published each run). {CHECKS_LIMITS}")

    # Gaps bullet — evidence-derived counts, not a fixed caveat.
    gap_bits = [f"{scope.direct_to_main_count} commit(s) landed directly on the mainline "
                f"(outside any pull-request gate)"]
    override_count = sum(1 for c in bundle.changes if c.review.verification == "override")
    claimed_count = sum(1 for c in bundle.changes if c.review.verification == "claimed")
    silent_count = sum(
        1 for c in bundle.changes
        if c.review.verification == "none"
        and any(g.outcome == "no-recorded-evidence" for g in c.policy_gates)
    )
    undetermined_count = sum(
        1 for c in bundle.changes if any(g.outcome == "undetermined" for g in c.policy_gates)
    )
    if override_count:
        gap_bits.append(f"{override_count} change(s) bypassed the review gate with an explicit "
                        f"override and carry no review artifact")
    if claimed_count:
        gap_bits.append(f"{claimed_count} of all assessed changes, gated or not, claim a review that could not be checked — "
                        f"either a free-text note with no structured trailer, or a structured "
                        f"trailer whose review artifact is missing, altered, records a "
                        f"different verdict, or does not cover the change at the committed content")
    if silent_count:
        gap_bits.append(f"{silent_count} change(s) touched a path the review gate guarded and "
                        f"carry no review signal at all")
    if undetermined_count:
        gap_bits.append(f"{undetermined_count} change(s) could not be assessed against the review "
                        f"gate because its configuration at that commit could not be read")
    out.append("- **Where it is partial/absent:** " + "; ".join(gap_bits) +
               " (the exception log and remediation plan detail this).")
    out.append("")

    out.extend(_enforcement_layer_lines(bundle.enforcement_layer))

    out.append("## Repo-level AI-orchestration assessment")
    out.append("")
    out.append(str(bundle.repo_level_attestation["basis"]))
    out.append("")

    out.append("## Control-coverage summary")
    out.append("")
    out.extend(coverage_summary_lines(bundle))
    out.append("")
    out.append("See `control-map.md` for the full observed-control → clause mapping and the "
               "authorship-agnostic framing.")
    out.append("")

    out.append("## Named source systems")
    out.append("")
    out.append("| Source system | Present | Evidence |")
    out.append("|---|---|---|")
    for s in bundle.source_systems:
        # Tri-state: None = not determinable this run — never rendered as a false "no".
        present = "not determinable" if s.present is None else ("yes" if s.present else "no")
        out.append(f"| {s.name} | {present} | {s.evidence} |")
    out.append("")

    out.append("## Limitations")
    out.append("")
    out.append("This assessment attests a **change process**, not artifact provenance or code "
               "correctness. Specifically, it is:")
    out.append("")
    for item in not_attested_list():
        out.append(f"- {item}")
    out.append("")
    return "\n".join(out)


def _enforcement_layer_lines(layer: dict) -> list[str]:
    """The installed-enforcement-layer section: one row per kerbtap gate, fixed wording."""
    lines = ["## Installed enforcement layer (observed at assessment time)", ""]
    if not layer:
        lines += ["The enforcement layer was not observed this run.", ""]
        return lines
    if not layer.get("installed"):
        lines += ["No kerbtap enforcement layer (`.claude/hooks/` or `.claude/settings.json`) "
                  "was found in this repository.", ""]
        return lines
    lines.append(str(layer["scope_note"]))
    lines.append("")
    source_dirty = layer.get("governor_uncommitted_changes")
    source_note = {
        True: " — its hook sources or installer have uncommitted changes, so the comparison copy is not that commit's",
        None: " — whether its hook sources and installer match that commit could not be read",
    }.get(source_dirty, "")
    lines.append(f"Compared with kerbtap {layer['governor_version']} (checkout "
                 f"`{str(layer['governor_commit'])[:12]}`{source_note}); assessed commit "
                 f"`{str(layer['assessed_commit'])[:12]}`"
                 + ("; `.claude/` has uncommitted changes, so the working tree differs from that "
                    "commit." if layer.get("claude_dir_uncommitted_changes") else "."))
    lines.append("")
    rows = layer.get("gates") or []
    if not rows:
        lines += ["No kerbtap gate file was found.", ""]
        return lines
    lines.append("| File | Kind | Installer wiring | State | Detail |")
    lines.append("|---|---|---|---|---|")
    for row in rows:
        wiring = "; ".join(row["wiring"]) or "—"
        lines.append(f"| `{row['path']}` | {row['kind']} | {wiring} | {row['state']} | {row['detail']} |")
    lines.append("")
    return lines
