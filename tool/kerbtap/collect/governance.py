"""Detect the repository's own governance signals — the named source systems.

Each signal is a real file the repo runs under. The tool records presence/absence
honestly (an absent signal is a finding, not something to hide) and cites the file path
as the evidence pointer.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from kerbtap.honesty import redact_quoted
from kerbtap.model import SourceSystem
from kerbtap.state import state_rel
from kerbtap.textutil import first_sentence

logger = logging.getLogger(__name__)

# (name, kind, relative_path, human evidence phrasing)
# relative_path is one path, or several candidates checked in order (first found wins).
_SIGNAL_FILES: tuple[tuple[str, str, str | tuple[str, ...], str], ...] = (
    (
        "Pre-commit review gate",
        "enforcing-hook",
        ".claude/hooks/precommit_review_guard.py",
        "Deny-mode hook that blocks a commit of a guarded file until the operator types the bypass prefix. It does not check for a recorded review.",
    ),
    (
        "Governance configuration",
        "config",
        ".claude/settings.json",
        "Scaffold configuration wiring the hooks and agents into the workflow.",
    ),
    (
        "Adversarial reviewer agent",
        "reviewer-agent",
        (".claude/agents/reviewer/SKILL.md", ".claude/agents/architect/SKILL.md"),
        "The reviewer agent contract invoked for every gated deliverable review.",
    ),
    (
        "Decision log (git-versioned)",
        "audit-trail",
        "docs/decision-log.md",
        "A dated decision log recording governance decisions and their PR/commit evidence, "
        "versioned in git. Append-only is a documented convention, not an enforced technical "
        "control; the underlying versioned record is git history.",
    ),
    (
        "Dogfood log (git-versioned)",
        "process-record",
        "docs/replication/dogfood-log.md",
        "A dated record of governance-process observations found while operating the repo, "
        "versioned in git. Append-only is a documented convention, not an enforced technical "
        "control; the underlying versioned record is git history.",
    ),
)


# Standard CODEOWNERS locations, in GitHub's own lookup order.
_CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


def detect_codeowners(repo_path: Path) -> dict[str, object] | None:
    """Detect a CODEOWNERS file and count its rules.

    Records the path and rule count ONLY — the file's contents (owner handles and
    possibly emails) are deliberately never quoted into the pack (§5 external-share
    hygiene). Returns ``None`` when no CODEOWNERS file exists.
    """
    for rel in _CODEOWNERS_LOCATIONS:
        path = repo_path / rel
        if not path.exists():
            continue
        rules = 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and len(stripped.split()) >= 2:
                rules += 1
        return {"path": rel, "rule_count": rules}
    return None


def detect_source_systems(
    repo_path: Path,
    *,
    gh_available: bool,
    github_repo: bool = True,
    shallow: bool = False,
    codeowners: dict[str, object] | None = None,
    branch_protection: dict[str, object] | None = None,
) -> list[SourceSystem]:
    """Detect the named source systems present in the repo.

    Args:
        repo_path: The repository under assessment.
        gh_available: Whether the ``gh`` CLI was usable this run.
        github_repo: Whether a GitHub repository resolved for the checkout.
        shallow: Whether the repo is a shallow clone — in that case the git evidence
            line must not claim the full history was read (it was not).
        codeowners: The detected CODEOWNERS record (from :func:`detect_codeowners`).
        branch_protection: The collected merge-gate record
            (from ``github.collect_branch_protection``); its ``state`` may be
            ``not-collected``, which maps to ``present=None`` (not determinable) —
            never to a false "absent".
    """
    git_evidence = (
        "Commit history read via `git log` from a SHALLOW clone: history is truncated "
        "at the clone's depth boundary, so the assessed range is NOT the repository's "
        "full history."
        if shallow
        else "Full commit history read via `git log` (the primary source system)."
    )
    systems: list[SourceSystem] = [
        SourceSystem(
            name="git version control",
            kind="version-control",
            present=(repo_path / ".git").exists(),
            evidence=git_evidence,
        ),
        SourceSystem(
            name="GitHub pull requests",
            kind="pull-request-platform",
            present=gh_available and github_repo,
            evidence=(
                "Pull-request records read via the `gh` CLI."
                if gh_available and github_repo
                else "No GitHub repository resolves for this checkout; PR evidence inferred from git merge/squash refs only."
                if gh_available
                else "gh CLI unavailable this run; PR evidence inferred from git merge/squash refs only."
            ),
        ),
    ]
    for name, kind, rels, evidence in _SIGNAL_FILES:
        candidates = (rels,) if isinstance(rels, str) else rels
        found = None
        unchecked: list[str] = []
        for r in candidates:
            try:
                if (repo_path / r).exists():
                    found = r
                    break
            except OSError as exc:
                logger.warning("Cannot check %s for %s: %s", r, name, exc)
                unchecked.append(r)
        present: bool | None
        if found is not None:
            present, text = True, f"{evidence} ({found})"
        elif unchecked:
            present = None
            text = "Not determinable: could not check " + " or ".join(unchecked) + "."
        else:
            present, text = False, "Not found at " + " or ".join(candidates) + "."
        systems.append(SourceSystem(name=name, kind=kind, present=present, evidence=text))

    # Code-review ownership routing (CODEOWNERS). Contents are never quoted (PII).
    if codeowners is not None:
        co_evidence = (
            f"CODEOWNERS file at {codeowners['path']} defining "
            f"{codeowners['rule_count']} ownership rule(s). Advisory unless branch "
            f"protection/rulesets require code-owner review (see the merge-gate record); "
            f"owner handles are deliberately not quoted into the pack."
        )
    else:
        co_evidence = (
            "Not found at any standard location (" + ", ".join(_CODEOWNERS_LOCATIONS) + ")."
        )
    systems.append(
        SourceSystem(
            name="Code-review ownership (CODEOWNERS)",
            kind="review-ownership",
            present=codeowners is not None,
            evidence=co_evidence,
        )
    )

    # Merge gate (GitHub branch protection / rulesets) — tri-state: an unreadable
    # state is recorded as "not determinable" (present=None), never as absent.
    bp = branch_protection or {"state": "not-collected", "note": "not collected this run."}
    bp_state = bp.get("state")
    bp_present: bool | None = {"present": True, "absent": False}.get(str(bp_state))
    systems.append(
        SourceSystem(
            name="Branch protection (GitHub, default branch)",
            kind="merge-gate",
            present=bp_present,
            evidence=str(bp.get("note", "not collected this run.")),
        )
    )

    for system in systems:
        logger.info("Source system %-28s present=%s", system.name, system.present)
    return systems


def decision_log_lines(repo_path: Path) -> int:
    """Return the line count of the decision log, or 0 if absent."""
    path = repo_path / "docs" / "decision-log.md"
    if not path.exists():
        return 0
    # errors="replace": a repo's own file may hold invalid UTF-8; degrade, never crash.
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def parse_dogfood_observations(repo_path: Path) -> list[dict[str, str]]:
    """Extract observation ids and headlines from the dogfood log, if present.

    Returns a list of ``{"id": "O4", "headline": "..."}`` records. The dogfood log's
    known governance gaps feed the exception log and remediation plan honestly.
    """
    path = repo_path / "docs" / "replication" / "dogfood-log.md"
    if not path.exists():
        return []
    # errors="replace": a repo's own file may hold invalid UTF-8; degrade, never crash.
    text = path.read_text(encoding="utf-8", errors="replace")
    # Each observation is a bold-led block: **O4 — ...** possibly spanning wrapped lines,
    # up to the next observation or a blank-line paragraph break. Capture the whole block
    # so the lead sentence is reconstructed intact (no mid-wrap truncation).
    block_re = re.compile(r"\*\*(O\d+)\s+—\s+(.*?)(?=\n\n|\*\*O\d+\s+—|\Z)", re.DOTALL)
    observations: list[dict[str, str]] = []
    for match in block_re.finditer(text):
        obs_id = match.group(1)
        body = match.group(2).replace("*", " ")
        headline = redact_quoted(first_sentence(body))[0]
        observations.append({"id": obs_id, "headline": headline})
    logger.info("Parsed %d dogfood observations", len(observations))
    return observations


_GATE_DECISIONS = ("deny", "ask", "bypass", "clear", "shadow-deny")


def gate_log_rel(root: Path) -> Path:
    """The gate log path below ``root``, in the state folder (SF-001)."""
    return state_rel(root, "gate-log", "events.jsonl")


def gate_log_path(repo_path: Path) -> Path:
    """The gate log path. The hooks write it in the main worktree; a linked worktree has no copy."""
    root = repo_path
    try:
        common = Path(subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"], cwd=repo_path,
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        ).strip())
        root = (common if common.is_absolute() else repo_path / common).resolve().parent
    except (subprocess.SubprocessError, OSError):
        pass
    return root / gate_log_rel(root)


def read_gate_log(repo_path: Path) -> dict[str, object]:
    """Read the local gate log: every parsable line, and the count of those that are not.

    Returns ``{"path": str, "present": bool, "events": [...], "unparsed": int}``. A line is
    parsable when it is a JSON object with a text ``gate`` and a ``decision`` of ``deny``,
    ``ask``, ``bypass`` or ``clear``. The file is local and editable, so the result is a record, not a
    verification.
    """
    path = gate_log_path(repo_path)
    result: dict[str, object] = {"path": gate_log_rel(path.parents[2]).as_posix(), "present": False,
                                 "events": [], "unparsed": 0}
    try:
        # errors="replace": a local file may hold invalid UTF-8; degrade, never crash.
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return result
    except OSError as exc:
        logger.warning("Gate log %s could not be read: %s", path, exc)
        return result
    events: list[dict[str, str]] = []
    unparsed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            unparsed += 1
            continue
        if (isinstance(record, dict) and isinstance(record.get("gate"), str)
                and record.get("decision") in _GATE_DECISIONS):
            events.append({"gate": redact_quoted(record["gate"])[0], "decision": record["decision"]})
        else:
            unparsed += 1
    result.update(present=True, events=events, unparsed=unparsed)
    logger.info("Read %d gate-log events (%d unparsed lines)", len(events), unparsed)
    return result


def load_control_clauses() -> dict:
    """Load the static control-clause lookup table from packaged JSON data."""
    data_path = Path(__file__).resolve().parent.parent / "data" / "control_clauses.json"
    return json.loads(data_path.read_text(encoding="utf-8"))
