"""Component 4 — exception-log.md: documented exceptions and gaps (spec §4.4).

An honest exception log is a credibility signal: a governed process has *documented*
exceptions, not zero exceptions. Every narrative sentence here is derived from evidence
actually collected in the assessed repository — never from assumptions carried over from
the repository this tool was first built in.
"""

from __future__ import annotations

from datetime import datetime

from kerbtap.build.common import pr_checks
from kerbtap.collect.github import CHECKS_LIMITS
from kerbtap.honesty import INTEGRITY_DISCLAIMER
from kerbtap.model import Change, EvidenceBundle


GATE_LOG_LIMIT = ("The gate log is a local file. Any local user or agent can edit or delete it, "
                  "so these counts are recorded, not verified.")


def _when(change: Change) -> datetime | None:
    try:
        return datetime.fromisoformat(change.author["authored_date"])
    except (KeyError, ValueError):
        return None


def _all_direct_predate_prs(changes: list[Change]) -> bool:
    """Whether every direct-to-main change predates the first PR-landed change.

    Evidence-derived: only when this holds may the log describe the direct commits as
    pre-discipline founding/setup work. Any unparseable date disables the claim.
    """
    direct = [_when(c) for c in changes if c.landed_via == "direct-to-main"]
    via_pr = [_when(c) for c in changes if c.landed_via != "direct-to-main"]
    if not direct or not via_pr or None in direct or None in via_pr:
        return False
    return max(direct) < min(via_pr)  # type: ignore[type-var]


def render(bundle: EvidenceBundle) -> str:
    scope = bundle.scope
    changes = bundle.changes

    direct = [c for c in changes if c.landed_via == "direct-to-main"]
    unreviewed_deliverables = [
        c for c in changes
        if not c.review.attested and any(gate.outcome == "no-recorded-evidence" for gate in c.policy_gates)
    ]
    overrides = [c for c in unreviewed_deliverables if c.review.verification == "override"]
    claimed = [c for c in unreviewed_deliverables if c.review.verification == "claimed"]
    silent = [c for c in unreviewed_deliverables if c.review.verification == "none"]
    undetermined = [
        c for c in changes if any(gate.outcome == "undetermined" for gate in c.policy_gates)
    ]

    out: list[str] = []
    out.append("# Exception Log")
    out.append("")
    out.append(f"*Repository:* `{scope.repository}` — commit range `{scope.commit_range}`.")
    out.append("")
    out.append(INTEGRITY_DISCLAIMER)
    out.append("")
    out.append("An honest exception log records where the governed process had gaps. Documented "
               "exceptions are a credibility signal, not a defect to hide.")
    out.append("")

    out.append("## 1. Direct-to-main changes (landed outside the pull-request gate)")
    out.append("")
    if direct:
        lead = (f"{len(direct)} of {scope.assessed_commits} assessed commits landed directly "
                f"on the default-branch mainline rather than through a pull request (they are "
                f"non-merge commits, carry no pull-request reference, and sit on the "
                f"first-parent history).")
        pr_landed = (scope.merge_pr_count + scope.squash_pr_count
                     + scope.pr_branch_commit_count)
        if pr_landed:
            lead += (f" The remaining changes landed through pull requests: "
                     f"{scope.merge_pr_count} merge-commit PRs plus "
                     f"{scope.pr_branch_commit_count} feature-branch commits they brought in, "
                     f"and {scope.squash_pr_count} squash-merge PRs.")
        out.append(lead)
        if _all_direct_predate_prs(changes):
            out.append("")
            out.append("All direct-to-main changes predate the repository's first "
                       "pull-request-landed change (consistent with setup commits made before a "
                       "pull-request discipline was established — derived from the recorded "
                       "authored dates, not assumed).")
        out.append("")
        out.append("| Change | Author (as reported by git) | Subject |")
        out.append("|---|---|---|")
        for c in direct:
            out.append(f"| `{c.change_id}` | {_clean(c.author['name'])} | {_clean(c.subject)} |")
        out.append("")
    else:
        out.append("None found — every assessed change landed through a pull request.")
        out.append("")

    out.append("## 2. Changes where a review gate applied but no integrity-checked review record exists")
    out.append("")
    out.append("This section counts only changes that the review gate applied to. Review claims and "
               "overrides on other changes are counted in `report.md`.")
    out.append("")
    if unreviewed_deliverables:
        out.append(f"{len(unreviewed_deliverables)} change(s) touched a path the review gate guarded "
                   f"at that commit but carry no integrity-checked `Governor-Review` PASS trailer. An "
                   f"integrity-checked record requires the trailer bound to a review artifact (recorded by "
                   f"`kerbtap review record`); the cases below fall short in one of three honest ways.")
        out.append("")
        if overrides:
            out.append(f"**Explicit overrides ({len(overrides)}).** The review gate was bypassed "
                       f"with its bypass variable; no review artifact was recorded. The override is "
                       f"logged here rather than hidden — that is the point of recording it.")
            out.append("")
            out.append("| Change | Subject |")
            out.append("|---|---|")
            for c in overrides:
                out.append(f"| `{c.change_id}` | {_clean(c.subject)} |")
            out.append("")
        if claimed:
            out.append(f"**Unchecked review claims ({len(claimed)}).** The change claims a review "
                       f"that could not be re-checked — either a free-text note with no structured "
                       f"trailer, or a structured `Governor-Review` trailer whose review artifact is "
                       f"missing, altered, records a different verdict, or does not cover this "
                       f"change at the committed content. Not counted as attested.")
            out.append("")
            out.append("| Change | Subject |")
            out.append("|---|---|")
            for c in claimed:
                out.append(f"| `{c.change_id}` | {_clean(c.subject)} |")
            out.append("")
        if silent:
            out.append(f"**No review signal at all ({len(silent)}).** The change carries neither a "
                       f"review trailer, a review claim, nor a recorded override. Nothing in the "
                       f"record shows the gate was consulted.")
            out.append("")
            out.append("| Change | Subject |")
            out.append("|---|---|")
            for c in silent:
                out.append(f"| `{c.change_id}` | {_clean(c.subject)} |")
            out.append("")
    else:
        out.append("None found — every change that touched a path the review gate guarded carries an "
                   "integrity-checked `Governor-Review` PASS trailer bound to a review artifact.")
        out.append("")
    if undetermined:
        out.append(f"**Applicability undetermined ({len(undetermined)}).** The review gate's "
                   f"configuration at these commits could not be read with certainty, so the pack "
                   f"does not say whether the gate applied — in either direction.")
        out.append("")
        out.append("| Change | Subject |")
        out.append("|---|---|")
        for c in undetermined:
            out.append(f"| `{c.change_id}` | {_clean(c.subject)} |")
        out.append("")

    failing = [c for c, block in pr_checks(bundle) if block.get("outcome") == "fail"]
    out.append("## 3. Pull-request head commits with a failing check run at collection time")
    out.append("")
    if failing:
        out.append(f"{len(failing)} pull request(s) have a check run that failed or timed out on the "
                   f"head commit, as read at collection time. {CHECKS_LIMITS}")
        out.append("")
        out.append("| Change | Pull request | Subject |")
        out.append("|---|---|---|")
        for c in failing:
            out.append(f"| `{c.change_id}` | #{(c.pull_request or {}).get('number')} | {_clean(c.subject)} |")
        out.append("")
    else:
        out.append("None found among the pull requests whose check runs were read. The report "
                   "counts the pull requests whose check runs were not read or did not settle.")
        out.append("")

    out.append("## 4. Governance gaps self-reported in a dogfood/process-observation log")
    out.append("")
    if not bundle.dogfood_observations:
        out.append("No dogfood or process-observation log was found in this repository, so there "
                   "are no self-reported governance gaps to record here.")
        out.append("")
    else:
        out.append("These are self-reported by the repository under assessment (docs/replication/"
                   "dogfood-log.md) — the process documenting its own exceptions. All parsed "
                   "observations are listed; each headline is quoted from the log itself.")
        out.append("")
        out.append("| Observation | Recorded headline (quoted) |")
        out.append("|---|---|")
        for obs in sorted(bundle.dogfood_observations, key=_obs_sort_key):
            out.append(f"| {obs['id']} | {_clean(obs['headline'])} |")
        out.append("")

    out.extend(_gate_log_section(bundle))
    return "\n".join(out)


def _gate_log_section(bundle: EvidenceBundle) -> list[str]:
    """Section 5: denies, asks, bypasses, clears and shadow denies per gate, from the local gate log."""
    log = bundle.gate_events
    out = ["## 5. Gate decisions recorded in the local gate log", ""]
    path = log.get("path", ".kerbtap/gate-log/events.jsonl")
    events = log.get("events") or []
    if not log.get("present"):
        out.append(f"No gate log was found at `{path}`, so no gate decision is counted here.")
    else:
        counts: dict[str, dict[str, int]] = {}
        for event in events:  # type: ignore[union-attr]
            per_gate = counts.setdefault(event["gate"], {"deny": 0, "ask": 0, "bypass": 0, "clear": 0, "shadow-deny": 0})
            per_gate[event["decision"]] += 1
        out.append(f"Counted over every parsable line of `{path}` at generation time: "
                   f"{len(events)} event(s) parsed, {log.get('unparsed', 0)} line(s) unparsed.")
        out.append("")
        if counts:
            out.append("| Gate | Denies | Asks | Bypasses | Clears | Shadow denies |")
            out.append("|---|---|---|---|---|---|")
            for gate in sorted(counts):
                row = counts[gate]
                out.append(f"| {_clean(gate)} | {row['deny']} | {row['ask']} | {row['bypass']} | {row['clear']} | {row['shadow-deny']} |")
        else:
            out.append("The log holds no parsable event.")
    out.append("")
    out.append(GATE_LOG_LIMIT)
    out.append("")
    return out


def _obs_sort_key(obs: dict[str, str]) -> tuple[int, str]:
    digits = "".join(ch for ch in obs["id"] if ch.isdigit())
    return (int(digits) if digits else 0, obs["id"])


def _clean(text: str) -> str:
    """Escape pipe characters so quoted repo text cannot break the markdown table."""
    return text.replace("|", "\\|").strip()
