#!/usr/bin/env python3
# Adapted for kerbtap from the starter template (starter/.claude/hooks/ is the
# pristine copy). Env var renamed; TRACKED_PATHS filled with this repo's canonical
# living docs. Scope resolution, command parsing and the bypass predicate moved to
# commit_scope.py.
"""PreToolUse hook — block git commits touching a canonical living doc without a
consistency pass first.

kerbtap's canonical living docs are read as current truth by every later
session: the rules file, the agent-boundary file, the decision log, the
governing workplan, and the authoritative vision. Their failure mode is not a
crash, it is silent decay — a reference to a file that moved, a count that no
longer matches, a section that contradicts the one below it. Nothing surfaces
that at runtime, so it accumulates until a session reasons from a doc that has
been wrong for weeks.

This hook makes `/verify-claims doc-pass` a precondition of the commit rather
than a good intention. Commits touching none of the tracked docs are unaffected
— the hook exits silently.

Bypass:
    PROJECT_DOC_REVIEWED=1 PROJECT_DOC_REVIEWED_REASON="<why>" git commit -m "..."
    optional: PROJECT_DOC_REVIEWED_UNTIL=YYYY-MM-DDTHH:MM:SSZ

A bypass with no reason, a past or unreadable expiry, or a gate-log line that cannot be
written is refused. Each deny and each bypass is one line in ``<state folder>/gate-log/``.

Record: after the pass, ``kerbtap doc-pass record <doc>...`` stores the blob hash of
each passed doc in ``<main worktree>/<state folder>/doc-pass/records.jsonl``. The commit is
allowed with no bypass when every tracked doc in it has a record of the blob that lands
(``commit_scope.landing_blob``). The allowance is one ``clear`` line in the gate log; if
that line cannot be written the gate denies.

The hook does NOT validate that the pass actually happened — that is a
self-attestation pattern, the same one ``precommit_review_guard.py`` uses. A record
proves the operator ran the record command on the bytes that land, and no more.

The claim this gate carries is "a living doc was swept for decay" — a different
claim from its neighbour's "a deliverable was adversarially reviewed". They share
``commit_scope`` for scope and keep separate bypass names, so neither token can
open the other's gate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from commit_scope import (
        ScopeError,
        commit_repo_dir,
        commit_verdict,
        deny,
        is_git_commit,
        landing_blob,
        read_payload,
        record_event,
        staged_paths,
        state_folder,
    )
except ImportError:  # pragma: no cover - exercised by test_engine_missing_fails_closed
    # The engine is missing or unreadable. An uncaught ImportError here would abort the
    # hook, and an aborted PreToolUse hook fails OPEN — silently, for as long as nobody
    # notices. This gate never blocks, so warn instead: a gate that cannot
    # evaluate its own precondition must say so rather than stay silent.
    import json as _json

    print(
        _json.dumps(
            {
                "systemMessage": (
                    "KERBTAP SHADOW (precommit_doc_consistency_guard would deny): "
                    f"cannot import commit_scope.py from {Path(__file__).resolve().parent} — "
                    "the shared scope engine is missing or unreadable, so this gate cannot "
                    "tell whether the commit touches a canonical living doc. Restore the "
                    "file (it is vendored at starter/.claude/hooks/commit_scope.py)."
                )
            }
        )
    )
    sys.exit(0)

BYPASS_ENV = "PROJECT_DOC_REVIEWED"
GATE = "precommit_doc_consistency_guard"
REVIEW_COMMAND = "/verify-claims doc-pass"
RECORD_COMMAND = "kerbtap doc-pass record"

# Exact repo-root-relative paths, matched literally. Kept deliberately short: a
# gate on every doc in the repo is a gate the operator learns to bypass
# reflexively, which is worse than no gate at all.
TRACKED_PATHS = frozenset(
    {
        "CLAUDE.md",
        "AGENTS.md",
        "docs/decision-log.md",
        "docs/workplan.md",
        "docs/vision.md",
    }
)


def _staged_tracked_docs(project_dir: str, command: str, cwd: str) -> list[str]:
    """Tracked canonical docs in the prospective commit."""
    return [f for f in staged_paths(project_dir, command, cwd) if f in TRACKED_PATHS]


def _records(project_dir: str) -> set[tuple[str, str]]:
    """``{(path, blob)}`` from the records file. A line that cannot be used is skipped."""
    try:
        common = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"], cwd=project_dir,
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        ).strip()
        root = (Path(project_dir) / common).resolve().parent
        text = (root / state_folder(root) / "doc-pass" / "records.jsonl").read_text(encoding="utf-8")
    except (subprocess.SubprocessError, OSError, UnicodeDecodeError):
        return set()
    found: set[tuple[str, str]] = set()
    for line in text.splitlines():
        try:
            item = json.loads(line)
            found.add((item["path"], item["blob"]))
        except (ValueError, RecursionError, TypeError, KeyError):
            continue
    return found


def _uncovered(project_dir: str, command: str, docs: list[str], cwd: str) -> list[str]:
    """The docs with no record of their landing blob."""
    records = _records(project_dir)
    return [
        d for d in sorted(docs)
        if (blob := landing_blob(project_dir, command, d, cwd)) is None or (d, blob) not in records
    ]


def _verdict(command: str, project_dir: str, reason_code: str) -> str:
    """Settle the deny: exit on a valid bypass, else the refusal note to append."""
    refusal = commit_verdict(command, BYPASS_ENV, GATE, project_dir, reason_code)
    if refusal is None:
        sys.exit(0)
    return f" BYPASS REFUSED: {refusal}." if refusal else ""


def main() -> None:
    data = read_payload()
    if data is None:
        sys.exit(0)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    command = data.get("tool_input", {}).get("command", "")
    if not is_git_commit(command):
        sys.exit(0)

    # The shell's directory, not the project root: a commit made in a linked worktree
    # lands that worktree's index, which the project root's index says nothing about.
    cwd = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or "."
    project_dir = commit_repo_dir(command, cwd)

    try:
        docs = _staged_tracked_docs(project_dir, command, cwd)
    except ScopeError as exc:
        refusal = _verdict(command, project_dir, "scope-unreadable")
        deny(
            f"The commit scope could not be read: {exc}. This gate cannot tell whether the "
            f"commit touches a canonical living doc, so it would deny. Fix the repository "
            f"state.{refusal}",
            project_dir,
            GATE,
        )
        sys.exit(0)
    if not docs:
        sys.exit(0)

    uncovered = _uncovered(project_dir, command, docs, cwd)
    if not uncovered:
        if record_event(project_dir, {"gate": GATE, "decision": "clear", "reason_code": "doc-pass-record"}):
            sys.exit(0)
        note = "The record matches, but the clear line could not be written to the gate log. "
    else:
        note = f"No record of the content that lands for: {', '.join(uncovered)}. "

    sample = ", ".join(sorted(docs)[:3])
    more = f" (+{len(docs) - 3} more)" if len(docs) > 3 else ""
    refusal = _verdict(command, project_dir, "living-docs")
    reason = (
        f"Commit touches {len(docs)} canonical living doc(s): {sample}{more}. {note}"
        f"Run `{REVIEW_COMMAND}` over the changed docs first — stale references, "
        f"drifted counts, and sections that now contradict each other are caught "
        f"at commit time or not at all. Once the pass is done, run "
        f"`{RECORD_COMMAND} <doc>...` on the passed docs, stage them and commit."
    )

    deny(reason + refusal, project_dir, GATE)
    sys.exit(0)


if __name__ == "__main__":
    main()
