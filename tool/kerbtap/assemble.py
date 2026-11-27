"""Assemble collected evidence into the typed :class:`EvidenceBundle`.

This is where raw commits + PRs + governance signals become per-change records with
control outcomes. It computes derived facts (landing method, review attestation,
control coverage) but invents nothing: every field traces to a collected source.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kerbtap import __version__
from kerbtap.collect import enforcement_layer, gate_surface
from kerbtap.collect import git as git_collect
from kerbtap.collect import governance, github
from kerbtap.init import default_source_repo
from kerbtap.honesty import neutralize_barred, redact_quoted
from kerbtap.review.record import coverage_from, current_contract_hash, resolve_pack_signal
from kerbtap.model import (
    Change,
    Commit,
    Custody,
    EvidenceBundle,
    PolicyGate,
    PullRequest,
    ReviewRecord,
    Scope,
    SourceSystem,
)

logger = logging.getLogger(__name__)

# AI states (do not collapse):
#   "governed"                — governance scaffold and/or dogfood log → governed orchestration.
#   "ai-assisted-ungoverned"  — AI evidenced (a .claude/ dir and/or AI co-author trailers) but
#                               no governance scaffold or dogfood log.
#   "none"                    — no .claude/ dir, no dogfood, no AI co-author trailers.
_STATE_GOVERNED = "governed"
_STATE_UNGOVERNED = "ai-assisted-ungoverned"
_STATE_NONE = "none"

_STATE_NONE_BASIS = (
    "No evidence of AI tooling or orchestration found in this repository "
    "(no .claude/ directory, no dogfood log, no AI co-author trailers)."
)

# Precision-first co-author trailer classification (see _ai_coauthor_lines). The 7A
# honesty asymmetry governs the design: prefer UNDER-claiming (missing an AI trailer) to
# OVER-claiming (labeling a human co-author as AI — a false claim about a person). Naive
# substring matching fails that test: "gpt" hits "egypt", "claude" hits "Claudette", and
# Cody/Devin/Claude/Cursor are real human names. Two decisive tiers instead:
#   Tier 1 — the email is a known AI *service* address (case-insensitive). Whole
#            corporate domains are deliberately NOT decisive: a human employee at
#            anthropic.com/sourcegraph.com must never be labeled AI by their employer.
#   Tier 2 — the ENTIRE name field is unambiguously an AI tool (anchored match). Bare
#            human given names that double as product names (Claude, Cody, Devin,
#            Cursor, bare "gpt") are NOT in this tier; they count only via Tier 1.
# Bots/CI (github-actions, dependabot, anything "[bot]") are excluded before either
# tier, EXCEPT the named AI-coding-agent GitHub apps (Devin, Jules), which are AI
# authorship even though GitHub renders them as app bots.
_COAUTHOR_RE = re.compile(r"^[^\S\n]*co-authored-by:(?P<rest>[^\n]*)", re.IGNORECASE | re.MULTILINE)
_TRAILER_NAME_EMAIL_RE = re.compile(r"^\s*(?P<name>[^<]*?)\s*(?:<(?P<email>[^>]*)>)?\s*$")
# Matched with fullmatch() against the parsed email field: the WHOLE address must be a
# known service address, so suffix collisions (a GitHub user named "...notcopilot", a
# human sales-agent@cursor.com) can never label a person as AI.
_AI_EMAIL_RE = re.compile(
    r"(?:"
    r"noreply@anthropic\.com"                       # Claude Code service address
    r"|\d*\+?copilot@users\.noreply\.github\.com"   # GitHub Copilot service account
    r"|(?:agent|cursoragent)@cursor\.(?:com|sh)"    # Cursor agent service address
    r"|devin-ai-integration@devin\.ai"              # Devin integration service address
    r"|aider@aider\.chat"                           # aider service address
    r")",
    re.IGNORECASE,
)
_AI_NAME_RE = re.compile(
    r"^(?:"
    r"claude\s+(?:code|opus|sonnet|haiku|fable|\d)[\w .()\[\]-]*"  # Claude Code / Claude Opus 4.8 (1M context)
    r"|(?:github\s+)?copilot"
    r"|chatgpt|gpt-[\w.]+"
    r"|codex(?:\s+cli)?"
    r"|gemini(?:\s+(?:cli|code\s+assist))?"
    r"|cursor\s+agent"
    r"|sourcegraph\s+cody"
    r"|devin\s+ai[\w .-]*"
    r"|aider(?:\s*\(.*\))?"                          # aider's canonical "aider (gpt-4o)"
    r")$",
    re.IGNORECASE,
)
_BOT_RE = re.compile(r"\[bot\]|github-actions|dependabot", re.IGNORECASE)
# AI coding agents that land as GitHub apps ("...[bot]"): AI authorship, not automation.
# Exact service slugs only (fully anchored), so ordinary app bots stay excluded.
_AI_APP_BOT_RE = re.compile(r"^(?:devin-ai-integration|google-labs-jules)\s*\[bot\]$", re.IGNORECASE)


@dataclass(slots=True)
class _AiEvidence:
    """The AI evidence actually detected in the repository."""

    state: str  # one of the _STATE_* constants
    orchestrated: bool  # True only for governed AI-orchestration
    basis: str
    trailer_commits: set[str]  # distinct commits carrying >=1 AI co-author trailer
    trailer_line_count: int  # total AI co-author trailer lines (>= trailer_commits)
    has_claude_dir: bool
    has_governance: bool


def _ai_coauthor_lines(text: str) -> list[str]:
    """Return the AI co-author trailer lines in ``text`` (bots and humans excluded)."""
    ai: list[str] = []
    for match in _COAUTHOR_RE.finditer(text):
        rest = match.group("rest").strip()
        parsed = _TRAILER_NAME_EMAIL_RE.match(rest)
        name = (parsed.group("name") or "").strip() if parsed else rest
        email = (parsed.group("email") or "").strip() if parsed else ""
        if _AI_APP_BOT_RE.match(name):
            ai.append(match.group(0).strip())  # AI coding agent shipped as a GitHub app
            continue
        if _BOT_RE.search(rest):
            continue  # CI/automation bot, never counted as AI authorship
        if (email and _AI_EMAIL_RE.fullmatch(email)) or _AI_NAME_RE.match(name):
            ai.append(match.group(0).strip())
    return ai


def _commit_ai_trailer_lines(commit: Commit) -> list[str]:
    return _ai_coauthor_lines(commit.body) + _ai_coauthor_lines(commit.subject)


def _has_ai_trailer(commit: Commit) -> bool:
    return bool(_commit_ai_trailer_lines(commit))


def _trailer_phrase(n_commits: int, n_lines: int) -> str:
    phrase = f"{n_commits} commit(s) carrying an AI co-author trailer"
    if n_lines and n_lines != n_commits:
        phrase += f" ({n_lines} AI co-author trailer lines in total)"
    return phrase


def _detect_ai_evidence(
    source_systems: list[SourceSystem], commits: list[Commit], claude_dir: bool
) -> _AiEvidence:
    """Classify the repository's AI evidence from what the tool actually found.

    The ``.claude/`` directory is detected *generically* (its mere existence) as a tooling
    signal, distinct from the specific governance-scaffold paths (hooks, settings, agents)
    and from AI co-author trailers. Governed AI-orchestration is asserted ONLY when the
    governance scaffold and/or a dogfood log is present. Every basis names the ACTUAL
    signals found, so it can never contradict the one-line headline (which reads the same
    detected flags).
    """
    scaffold_kinds = {"enforcing-hook", "config", "reviewer-agent"}
    has_scaffold = any(s.present and s.kind in scaffold_kinds for s in source_systems)
    has_dogfood = any(s.present and s.name.startswith("Dogfood log") for s in source_systems)
    has_governance = has_scaffold or has_dogfood

    trailer_commits: set[str] = set()
    trailer_line_count = 0
    for commit in commits:
        lines = _commit_ai_trailer_lines(commit)
        if lines:
            trailer_commits.add(commit.sha)
            trailer_line_count += len(lines)
    n_commits = len(trailer_commits)

    if has_governance:
        items: list[str] = []
        if has_scaffold:
            items.append("a kerbtap governance scaffold (.claude/ hooks, settings, and/or agents)")
        if has_dogfood:
            items.append("a dogfood log (docs/replication/dogfood-log.md) recording an AI agent driving delivery")
        if n_commits:
            items.append(_trailer_phrase(n_commits, trailer_line_count))
        basis = (
            "Repo-level inference (not a per-commit cryptographic binding): governed "
            "AI-orchestrated delivery is evidenced by " + "; ".join(items) + "."
        )
        if not n_commits:
            basis += (
                " There are no per-commit AI co-author trailers, so no per-commit human/AI split "
                "is asserted from trailers."
            )
        return _AiEvidence(
            _STATE_GOVERNED, True, basis, trailer_commits, trailer_line_count, claude_dir, True
        )

    if claude_dir or n_commits:
        frags: list[str] = []
        if claude_dir:
            frags.append("a `.claude/` Claude Code configuration (e.g. skills), indicating AI coding tooling")
        if n_commits:
            frags.append(_trailer_phrase(n_commits, trailer_line_count))
        basis = (
            "AI-assisted development is evidenced by " + "; ".join(frags) + ", but no kerbtap "
            "governance scaffold (hooks/agents/settings) or dogfood log is present, so governed "
            "AI-orchestration of delivery is not evidenced."
        )
        return _AiEvidence(
            _STATE_UNGOVERNED, False, basis, trailer_commits, trailer_line_count, claude_dir, False
        )

    return _AiEvidence(_STATE_NONE, False, _STATE_NONE_BASIS, set(), 0, claude_dir, False)

_NOT_ATTESTED = [
    "Not artifact provenance (a build-artifact question — SLSA/in-toto/sigstore territory).",
    "Not correctness or security of the code itself.",
    "Not cryptographic tamper-resistance or a cryptographic integrity guarantee (see the integrity-scope disclaimer).",
    "Not a legal-compliance certification or an audit opinion; the control map is a mapping.",
    "Not the whole codebase unless the assessed scope says so.",
]


def _packaged_by() -> str:
    return f"kerbtap v{__version__} (7A semi-manual pack generator), automated collection"


# Honest per-change outcome vocabulary, differentiated PER CLAUSE from actual evidence:
#   'attested'     — an integrity-checked 'Governor-Review: PASS' record bound to a review artifact
#                    by content hash (strongest); a free-text review note alone is not.
#   'observed'     — the specific control artifact is evidenced (an approving GitHub review
#                    for CC8.1/PW.7; an approver!=author merge for Art.17; a PR landing for
#                    A.8.32, whose mechanism *is* the PR).
#   'process-only' — landed via a pull-request process, but that specific control artifact
#                    is not evidenced (no approving review; or approver==author/unknown).
#   'not-observed' — the change did not go through the mechanism at all (direct-to-main).
OUTCOME_ATTESTED = "attested"
OUTCOME_OBSERVED = "observed"
OUTCOME_PROCESS_ONLY = "process-only"
OUTCOME_NOT_OBSERVED = "not-observed"


def _clause_outcome(
    *, has_pr: bool, review_attested: bool, observed_condition: bool, review_attests: bool = True
) -> str:
    """Outcome for one clause: attested > (observed | process-only) > not-observed.

    ``review_attests`` gates whether an integrity-checked review record counts as attestation for
    *this* clause. It is False for segregation of duties (DORA Art.17): a review trailer
    proves a review happened, not that the approver differs from the author — the trailer's
    reviewer is a self-authored label, not an identity-bound approver — so segregation must
    stand on its own evidence (merge actor != author), never on a review trailer.
    """
    if review_attested and review_attests:
        return OUTCOME_ATTESTED
    if not has_pr:
        return OUTCOME_NOT_OBSERVED
    return OUTCOME_OBSERVED if observed_condition else OUTCOME_PROCESS_ONLY


def _change_control_outcomes(
    *,
    has_pr: bool,
    review_attested: bool,
    has_approving_review: bool,
    segregation_evidenced: bool,
) -> dict[str, str]:
    """Compute per-change control outcomes, differentiated per clause (spec §4.2)."""
    return {
        # A.8.32 (change management): the PR landing itself is the observed mechanism.
        "ISO-A.8.32": _clause_outcome(
            has_pr=has_pr, review_attested=review_attested, observed_condition=True
        ),
        # CC8.1 (authorize/approve) & PW.7 (code review): need an approving GitHub review.
        "SOC2-CC8.1": _clause_outcome(
            has_pr=has_pr, review_attested=review_attested, observed_condition=has_approving_review
        ),
        "SSDF-PW.7": _clause_outcome(
            has_pr=has_pr, review_attested=review_attested, observed_condition=has_approving_review
        ),
        # Art.17 (segregation + approval): needs approver != author (segregation evidenced).
        # A review trailer does NOT attest this — it is not identity-bound (review_attests=False).
        "DORA-Art.17": _clause_outcome(
            has_pr=has_pr,
            review_attested=review_attested,
            observed_condition=segregation_evidenced,
            review_attests=False,
        ),
    }


# Very large repositories can reference many hundreds of PRs; each enrichment is one
# `gh pr view` network call. Enrichment is therefore capped at the most recent PRs, and
# every capped-out record says so explicitly (no silent truncation).
_PR_ENRICH_LIMIT = 150


def _select_prs_for_enrichment(
    numbers: list[int], limit: int = _PR_ENRICH_LIMIT
) -> tuple[list[int], set[int]]:
    """Split referenced PR numbers into (most-recent ``limit`` to enrich, capped-out rest)."""
    unique = sorted(set(numbers), reverse=True)
    return unique[:limit], set(unique[limit:])


def _pr_evidence(
    *,
    landing: str,
    pr_number: int | None,
    pr_details: dict[int, dict],
    gh_up: bool,
    capped: set[int] | frozenset[int] = frozenset(),
) -> dict[str, object] | None:
    """Return enriched PR evidence for a PR's mainline change, or None.

    Only the mainline change that *represents* the PR (the merge commit or the squash
    commit) carries the full record, so the manifest stays bounded. Feature-branch commits
    keep only the pr_number pointer in their source refs.
    """
    if landing not in {"merge-pr", "squash-pr"} or pr_number is None:
        return None
    method = "merge-commit" if landing == "merge-pr" else "squash"
    if not gh_up:
        return {
            "number": pr_number,
            "collection": "not collected: gh unavailable",
            "merge_method_inferred": method,
            "checks": dict(github.CHECKS_NOT_COLLECTED),
        }
    if pr_number in capped:
        return {
            "number": pr_number,
            "collection": (
                f"not collected: PR-detail enrichment capped at the {_PR_ENRICH_LIMIT} most "
                f"recent referenced PRs; this PR's evidence comes from git alone"
            ),
            "merge_method_inferred": method,
            "checks": dict(github.CHECKS_NOT_COLLECTED),
        }
    detail = dict(pr_details.get(pr_number, {"number": pr_number, "collection": "not collected: gh pr view returned no data"}))
    detail.setdefault("checks", dict(github.CHECKS_NOT_COLLECTED))
    detail["merge_method_inferred"] = method
    return detail


_REVIEW_GATE_NAME = "precommit_review_guard (blocks guarded commits until the bypass prefix is typed; does not check for a recorded review)"

# Fixed wording for every gate outcome. Never built from repo-sourced text.
_BASIS_MERGE = ("Merge commit: the gate evaluates `git commit`; a merge is assessed through the "
                "commits it brings in.")
_BASIS_ABSENT = "The review gate was not installed at this commit."
_BASIS_UNWIRED = ("The review gate's file was present but not wired in the repository's tracked "
                  "`.claude/settings.json` at this commit (wiring kept outside the repository is "
                  "not visible to the pack).")
_BASIS_UNREADABLE = ("The review gate's configuration at this commit could not be read with "
                     "certainty, so whether it applied is undetermined.")
_BASIS_PASS = ("The change touched a path the gate guarded at this commit and carries an "
               "integrity-checked review record (a trailer bound to a review artifact).")
_BASIS_GUARDED = ("The change touched a path the gate guarded at this commit and carries no "
                  "integrity-checked review record.")
_BASIS_UNGUARDED = "The change touched no path the gate guarded at this commit."


def _guarded_paths(
    commit: Commit, surface: gate_surface.GateSurface, paths: tuple[str, ...]
) -> tuple[str, ...] | None:
    """The changed paths the gate guarded at this commit; ``None`` when unknowable."""
    if commit.is_merge or surface.state in (gate_surface.ABSENT, gate_surface.UNWIRED):
        return ()
    if surface.state != gate_surface.ACTIVE:
        return None
    return tuple(p for p in paths if surface.guards(p))


def _review_gate(
    commit: Commit, surface: gate_surface.GateSurface, guarded: tuple[str, ...] | None, review_attested: bool
) -> PolicyGate:
    """The review gate's outcome for one change, from the gate's state at that commit.

    Applicability comes from the commit's own changed paths and the surface the gate was
    configured to guard in the commit's own tree — never from whether the message happens
    to carry a review signal, so it is settled before the review record is consulted. A
    surface that cannot be read is ``undetermined``, not a guess. ``applies`` is true only
    when the gate applied to this change.
    """
    if commit.is_merge:
        return PolicyGate(_REVIEW_GATE_NAME, applies=False, outcome="not-applicable", basis=_BASIS_MERGE)
    if surface.state == gate_surface.ABSENT:
        return PolicyGate(_REVIEW_GATE_NAME, applies=False, outcome="not-applicable", basis=_BASIS_ABSENT)
    if surface.state == gate_surface.UNWIRED:
        return PolicyGate(_REVIEW_GATE_NAME, applies=False, outcome="not-applicable", basis=_BASIS_UNWIRED)
    if guarded is None:
        return PolicyGate(_REVIEW_GATE_NAME, applies=True, outcome="undetermined", basis=_BASIS_UNREADABLE)
    if not guarded:
        return PolicyGate(_REVIEW_GATE_NAME, applies=False, outcome="not-applicable", basis=_BASIS_UNGUARDED)
    if review_attested:
        return PolicyGate(_REVIEW_GATE_NAME, applies=True, outcome="pass", basis=_BASIS_PASS)
    return PolicyGate(_REVIEW_GATE_NAME, applies=True, outcome="no-recorded-evidence", basis=_BASIS_GUARDED)


def _change_ai_assisted(commit: Commit, ai_ev: _AiEvidence) -> dict[str, object]:
    """Per-change AI-assistance record, derived from actual evidence (never hardcoded)."""
    if commit.sha in ai_ev.trailer_commits:
        return {
            "value": True,
            "evidence": "per-commit AI co-author trailer",
            "basis": "An AI co-author trailer is recorded in this commit.",
        }
    if ai_ev.orchestrated:
        return {"value": True, "evidence": "repo-level inference", "basis": ai_ev.basis}
    return {"value": False, "evidence": "none", "basis": ai_ev.basis}


def _build_change(
    commit: Commit,
    *,
    repo_path: Path,
    surface: gate_surface.GateSurface,
    paths: tuple[str, ...],
    ai_ev: _AiEvidence,
    pr_lookup: dict[int, PullRequest],
    pr_details: dict[int, dict],
    gh_up: bool,
    internal: bool,
    first_parent: set[str],
    merge_map: dict[str, tuple[int | None, str | None]],
    retrieved_ts: str,
    capped_prs: set[int],
    current_contract: str | None,
) -> Change:
    landing = git_collect.classify_landing(commit, first_parent)
    pr_number = git_collect.extract_pr_number(commit)
    branch = git_collect.extract_branch(commit)
    if landing == "pr-branch-commit":
        # A feature-branch commit merged via a PR: inherit the merge's PR/branch.
        merged_pr, merged_branch = merge_map.get(commit.sha, (None, None))
        pr_number = pr_number or merged_pr
        branch = branch or merged_branch
    has_pr = landing in {"merge-pr", "squash-pr", "pr-branch-commit"}
    # detect_review is a pure, body-only classifier; the pack must not treat a structured
    # trailer as attested from syntax alone, so resolve and hash-check its artifact against
    # THIS commit's tree (not the working checkout) — a no-op that leaves prose claims /
    # overrides untouched on repos without artifacts.
    guarded = _guarded_paths(commit, surface, paths)
    coverage = coverage_from(() if commit.is_merge else paths, guarded)
    review_signal = resolve_pack_signal(
        repo_path, git_collect.detect_review(commit, surface.override_names), commit.sha, coverage, current_contract
    )
    review_attested = review_signal.attested

    # Per-clause evidence drawn from the PR's collected reviews / merge actor (when gh ran).
    pr_detail = pr_details.get(pr_number) if pr_number is not None else None
    reviews = (pr_detail or {}).get("reviews") or []
    has_approving_review = any((r.get("state") or "").upper() == "APPROVED" for r in reviews)
    merged_by = (pr_detail or {}).get("merged_by")
    pr_author_login = (pr_detail or {}).get("author_login")
    segregation_evidenced = bool(merged_by and pr_author_login and merged_by != pr_author_login)

    pr = pr_lookup.get(pr_number) if pr_number is not None else None
    reviewer = review_signal.reviewer if review_attested else "none recorded"

    gate = _review_gate(commit, surface, guarded, review_attested)

    # Every repo-sourced free-form string is redacted before it enters the pack, so the
    # fail-closed guard only ever sees kerbtap's own controlled prose (never a repo's own
    # "immutable"/path in a subject, branch, or name).
    safe_subject, _ = redact_quoted(commit.subject)
    branch_raw = branch or (pr.head_ref if pr else None)
    safe_branch = redact_quoted(branch_raw)[0] if branch_raw else None

    source_refs: dict[str, str | int | None] = {
        "commit_sha": commit.sha,
        "commit_sha_role": "content pointer / fingerprint (NOT tamper-evidence)",
        "pr_number": pr_number,
        "branch": safe_branch,
    }

    author: dict[str, str] = {
        # The author NAME is repo-sourced free-form text (it can literally be an email
        # address or contain a path/barred term) — unified redaction, like every quote.
        "name": redact_quoted(commit.author_name)[0],
        "recorded_as": "author of record, as reported by git (not cryptographically bound)",
        "authored_date": commit.authored_date,
    }
    if internal:
        # Emails are PII; included only in internal/dogfood mode (§5 external-share hygiene).
        author["email"] = neutralize_barred(commit.author_email)[0]

    return Change(
        change_id=commit.short_sha,
        subject=safe_subject,
        source_refs=source_refs,
        author=author,
        ai_assisted=_change_ai_assisted(commit, ai_ev),
        review=ReviewRecord(
            attested=review_attested,
            evidence=review_signal.evidence,
            reviewer=reviewer,
            verification=review_signal.verification,
            scope=review_signal.scope or "",
            contract=review_signal.contract,
            contract_state=review_signal.contract_state,
        ),
        policy_gates=[gate],
        jira_refs=git_collect.extract_jira_refs(commit),
        control_outcomes=_change_control_outcomes(
            has_pr=has_pr,
            review_attested=review_attested,
            has_approving_review=has_approving_review,
            segregation_evidenced=segregation_evidenced,
        ),
        custody=Custody(
            source_system="git" + (" + GitHub (gh)" if pr is not None else ""),
            retrieved_ts=retrieved_ts,
            packaged_by=_packaged_by(),
        ),
        landed_via=landing,
        pull_request=_pr_evidence(
            landing=landing,
            pr_number=pr_number,
            pr_details=pr_details,
            gh_up=gh_up,
            capped=capped_prs,
        ),
    )


def _build_changes(
    repo_path: Path,
    commits: list[Commit],
    *,
    paths_by_commit: dict[str, tuple[str, ...]],
    ai_ev: _AiEvidence,
    pr_lookup: dict[int, PullRequest],
    gh_up: bool,
    internal: bool,
    first_parent: set[str],
    merge_map: dict[str, tuple[int | None, str | None]],
    retrieved_ts: str,
) -> list[Change]:
    """The per-change record of each of ``commits``: what the pack and the scoped entry both build."""
    surfaces = gate_surface.surfaces_by_commit(repo_path, [c.sha for c in commits])

    # Enrich the PRs actually referenced by the assessed changes (bounded, standalone).
    # Very large repos are capped to the most recent PRs — never silently: each capped
    # record and a log line say so.
    pr_numbers = [n for n in (git_collect.extract_pr_number(c) for c in commits) if n is not None]
    enrich_numbers, capped_prs = _select_prs_for_enrichment(pr_numbers)
    if capped_prs:
        logger.warning(
            "PR-detail enrichment capped: enriching the %d most recent of %d referenced "
            "PRs; %d older PRs carry git-only evidence (noted per record)",
            len(enrich_numbers), len(enrich_numbers) + len(capped_prs), len(capped_prs),
        )
    pr_details = github.collect_pr_details(repo_path, enrich_numbers) if gh_up else {}

    current_contract = current_contract_hash()  # read and hashed once for the run
    return [
        _build_change(
            commit,
            repo_path=repo_path,
            surface=surfaces.get(commit.sha, gate_surface.UNREADABLE_SURFACE),
            paths=paths_by_commit.get(commit.sha, ()),
            ai_ev=ai_ev,
            pr_lookup=pr_lookup,
            pr_details=pr_details,
            gh_up=gh_up,
            internal=internal,
            first_parent=first_parent,
            merge_map=merge_map,
            retrieved_ts=retrieved_ts,
            capped_prs=capped_prs,
            current_contract=current_contract,
        )
        for commit in commits
    ]


def build_scoped_changes(
    repo_path: Path, shas: list[str], *, internal: bool = False
) -> tuple[list[Change], list[str]]:
    """Build the per-change record of the commits ``shas`` only, the way :func:`build_bundle` builds it.

    Skips the enforcement layer, branch protection and the dogfood log. Returns the changes
    (newest first, as the pack lists them) and the ``shas`` this clone does not hold.
    """
    repo_path = repo_path.resolve()
    retrieved_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    commits = git_collect.collect_commits(repo_path)
    held = [sha for sha in shas if git_collect.has_commit(repo_path, sha)]
    unread = [sha for sha in shas if sha not in held]
    selected = git_collect.collect_commits(repo_path, held)
    gh_up = github.gh_available()
    gh_repo = gh_up and github.repo_resolves(repo_path)
    prs = github.collect_pull_requests(repo_path) if gh_repo else []
    source_systems = governance.detect_source_systems(
        repo_path,
        gh_available=gh_up,
        github_repo=gh_repo,
        shallow=git_collect.is_shallow(repo_path),
        codeowners=governance.detect_codeowners(repo_path),
        branch_protection={"state": "not-collected", "note": "not read by the scoped path."},
    )
    ai_ev = _detect_ai_evidence(source_systems, commits, claude_dir=(repo_path / ".claude").exists())
    changes = _build_changes(
        repo_path,
        selected,
        paths_by_commit={c.sha: git_collect.commit_changed_paths(repo_path, c.sha) for c in selected},
        ai_ev=ai_ev,
        pr_lookup={pr.number: pr for pr in prs},
        gh_up=gh_up,
        internal=internal,
        first_parent=git_collect.first_parent_shas(repo_path),
        merge_map=git_collect.merge_branch_map(repo_path, selected),
        retrieved_ts=retrieved_ts,
    )
    logger.info("Built %d scoped changes (%d commits not in this clone)", len(changes), len(unread))
    return changes, unread


def build_bundle(
    repo_path: Path, *, internal: bool = False, source_repo: Path | None = None
) -> EvidenceBundle:
    """Collect from all sources and assemble the evidence bundle for ``repo_path``.

    Args:
        repo_path: The git repository to assess.
        internal: When True, include PII (absolute repo path + author emails) for internal
            dogfood use. Default False produces an external-safe pack.
        source_repo: The kerbtap checkout whose installer renders the comparison copy of
            the enforcement layer. Defaults to the checkout this package runs from.
    """
    repo_path = repo_path.resolve()
    retrieved_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    commits = git_collect.collect_commits(repo_path)
    first_parent = git_collect.first_parent_shas(repo_path)
    merge_map = git_collect.merge_branch_map(repo_path, commits)
    gh_up = github.gh_available()
    gh_repo = gh_up and github.repo_resolves(repo_path)
    prs = github.collect_pull_requests(repo_path) if gh_repo else []
    pr_lookup = {pr.number: pr for pr in prs}

    codeowners = governance.detect_codeowners(repo_path)
    branch_protection = (
        github.collect_branch_protection(repo_path)
        if gh_up
        else {
            "state": "not-collected",
            "note": "gh CLI unavailable this run; the merge-gate (branch protection / "
                    "ruleset) state was not read and is not asserted either way.",
        }
    )
    source_systems = governance.detect_source_systems(
        repo_path,
        gh_available=gh_up,
        github_repo=gh_repo,
        shallow=git_collect.is_shallow(repo_path),
        codeowners=codeowners,
        branch_protection=branch_protection,
    )
    layer = enforcement_layer.observe(repo_path, source_repo or default_source_repo(), __version__)
    control_clauses = governance.load_control_clauses()
    dogfood = governance.parse_dogfood_observations(repo_path)
    gate_events = governance.read_gate_log(repo_path)
    ai_ev = _detect_ai_evidence(source_systems, commits, claude_dir=(repo_path / ".claude").exists())
    changes = _build_changes(
        repo_path,
        commits,
        paths_by_commit=git_collect.changed_paths(repo_path),
        ai_ev=ai_ev,
        pr_lookup=pr_lookup,
        gh_up=gh_up,
        internal=internal,
        first_parent=first_parent,
        merge_map=merge_map,
        retrieved_ts=retrieved_ts,
    )

    merge_count = sum(1 for c in changes if c.landed_via == "merge-pr")
    squash_count = sum(1 for c in changes if c.landed_via == "squash-pr")
    pr_branch_count = sum(1 for c in changes if c.landed_via == "pr-branch-commit")
    direct_count = sum(1 for c in changes if c.landed_via == "direct-to-main")
    review_count = sum(1 for c in changes if c.review.attested)

    newest = commits[0].short_sha if commits else "none"
    oldest = commits[-1].short_sha if commits else "none"

    scope = Scope(
        # Repo name is repo-sourced free-form text → unified redaction. The internal-only
        # path keeps its structure (scrub_paths not applied) since internal mode shows it.
        repository=redact_quoted(_repo_name(repo_path))[0],
        repo_path=neutralize_barred(str(repo_path))[0],
        commit_range=f"{oldest}..{newest}",
        as_of=retrieved_ts,
        assessed_commits=len(commits),
        assessed_prs=merge_count + squash_count,
        merge_pr_count=merge_count,
        squash_pr_count=squash_count,
        pr_branch_commit_count=pr_branch_count,
        direct_to_main_count=direct_count,
    )

    _AI_NOTES = {
        _STATE_GOVERNED: (
            "Repo-level assessment of the workflow from detected evidence, not a per-commit "
            "cryptographic binding of any human or model to any change."
        ),
        _STATE_UNGOVERNED: (
            "AI-assisted development is evidenced (see basis) but no kerbtap governance "
            "scaffold or dogfood log is present; the pack does not assert governed "
            "AI-orchestration of delivery."
        ),
        _STATE_NONE: (
            "No AI tooling or orchestration was evidenced in this repository; the pack does "
            "not assert any."
        ),
    }
    repo_level_attestation = {
        "ai_state": ai_ev.state,
        "ai_orchestrated": ai_ev.orchestrated,
        "ai_signals": {
            "governance_scaffold_or_dogfood": ai_ev.has_governance,
            "claude_dir_present": ai_ev.has_claude_dir,
            "ai_trailer_commits": len(ai_ev.trailer_commits),
            "ai_trailer_lines": ai_ev.trailer_line_count,
        },
        "basis": ai_ev.basis,
        "note": _AI_NOTES[ai_ev.state],
    }

    return EvidenceBundle(
        scope=scope,
        changes=changes,
        source_systems=source_systems,
        repo_level_attestation=repo_level_attestation,
        control_clauses=control_clauses,
        review_attested_count=review_count,
        gh_available=gh_repo,
        ai_orchestrated=ai_ev.orchestrated,
        ai_state=ai_ev.state,
        ai_has_claude_dir=ai_ev.has_claude_dir,
        ai_has_governance=ai_ev.has_governance,
        ai_trailer_count=len(ai_ev.trailer_commits),
        internal=internal,
        dogfood_observations=dogfood,
        gate_events=gate_events,
        branch_protection=branch_protection,
        codeowners=codeowners,
        enforcement_layer=layer,
    )


def not_attested_list() -> list[str]:
    """The §2 'NOT attested' list, exposed for the manifest and report."""
    return list(_NOT_ATTESTED)


def _repo_name(repo_path: Path) -> str:
    return repo_path.name
