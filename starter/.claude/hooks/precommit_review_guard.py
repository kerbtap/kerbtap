#!/usr/bin/env python3
# Adapted for kerbtap from the starter template (starter/.claude/hooks/ is the
# pristine copy). Review target re-pointed from Python source to the configured
# guarded surface; env var renamed; switched to "deny" mode. Scope resolution,
# command parsing and the bypass predicate moved to commit_scope.py.
"""PreToolUse hook — block git commits of guarded files without review.

Any commit touching a guarded file — the surface is configured by
:data:`TRACKED_PATH_PREFIXES` and :data:`TRACKED_SUFFIXES`, which ``kerbtap init``
rewrites per repository — gets an adversarial review *before* the commit lands.
After-the-fact review is theatre: by the time the flaw surfaces post-commit, the bad
change is in branch history and a fix is a separate cycle.

The prose here is deliberately domain-neutral. This copy is the one ``kerbtap init``
installs into other repositories, so wording that
named *this* project's kind of deliverable was read by users of every other project as a
description of their own source files. What the gate guards is a configured surface, not
a domain.

The hook does not validate that the review actually happened — that
is a self-attestation pattern. The friction itself is the safety net:
the commit goes through only after the operator consciously types the
bypass prefix.

Bypass:
    PROJECT_REVIEWED=1 PROJECT_REVIEWED_REASON="<why>" git commit -m "..."
    optional: PROJECT_REVIEWED_UNTIL=YYYY-MM-DDTHH:MM:SSZ

A bypass with no reason, a past or unreadable expiry, or a gate-log line that cannot be
written is refused. Each deny and each bypass is one line in ``<state folder>/gate-log/``.

Telemetry, never a decision: ``rebase --continue``, ``cherry-pick``, ``revert``, ``merge``
and ``am`` create commits without ``git commit``, so this gate never evaluates what they
land. Each such invocation is appended to ``<state folder>/telemetry/ungated-commit-verbs.jsonl`` (verb, branch, HEAD
before the command) so the rate is measurable. The record says a verb ran, not what it
landed — at this point that is not yet known. A failed write is ignored.

Commits not touching a guarded file are unaffected — the hook exits silently.
This includes a repository with no commits yet: ``git diff --cached`` lists staged
paths before the first commit, so the gate has everything it needs and keeps
enforcing.

The claim this gate carries is "a guarded change was adversarially reviewed". Its
neighbour ``precommit_doc_consistency_guard`` carries a different one, and the two
share ``commit_scope`` for scope but keep separate bypass names — a token that
cleared both would let a doc sweep open the review gate.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from commit_scope import (
        ScopeError,
        bypass_usage,
        commit_creating_verb,
        commit_repo_dir,
        commit_verdict,
        deny,
        is_git_commit,
        read_payload,
        run,
        staged_paths,
        state_folder,
    )
except ImportError:  # pragma: no cover - exercised by test_engine_missing_fails_closed
    # The engine is missing or unreadable. An uncaught ImportError here would abort the
    # hook, and an aborted PreToolUse hook fails OPEN — the commit proceeds ungated,
    # silently, for as long as nobody notices. Deny instead: a gate that cannot evaluate
    # its own precondition must not report the property it never checked.
    import json as _json

    print(
        _json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "precommit_review_guard cannot import commit_scope.py from "
                        f"{Path(__file__).resolve().parent} — the shared scope engine is "
                        "missing or unreadable, so this gate cannot tell whether the commit "
                        "touches a guarded file. Restore the file (it is vendored at "
                        "starter/.claude/hooks/commit_scope.py) before committing."
                    ),
                }
            }
        )
    )
    sys.exit(0)

BYPASS_ENV = "PROJECT_REVIEWED"
GATE = "precommit_review_guard"

# The agent the refusal tells the operator to run. A constant, not prose, so
# ``kerbtap init`` can retarget it per repository the way it retargets the surface
# tuples — an installed repo has a `reviewer` agent, not kerbtap's `architect`.
REVIEWER_AGENT = "architect"

TRACKED_PATH_PREFIXES = ("docs/deliverables/",)
# Both prose deliverables (.md) and the data files that carry their load-bearing
# numbers (.json — e.g. model/assumptions.json) are gated. A commit that touched only
# the JSON would otherwise change every derived figure in the most-scrutinised
# deliverable with no adversarial review. str.endswith accepts a tuple.
TRACKED_SUFFIXES = (".md", ".json")


def _record_ungated_verb(record_dir: str, repo_dir: str, verb: str) -> None:
    """Append one telemetry line under ``record_dir``. Never raises, never denies."""
    try:
        line = json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "gate": "precommit_review_guard",
                "verb": verb,
                "branch": run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_dir),
                "head": run(["git", "rev-parse", "HEAD"], repo_dir),
            }
        )
        path = Path(record_dir) / state_folder(Path(record_dir)) / "telemetry" / "ungated-commit-verbs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except (OSError, ValueError):
        pass


def _on_surface(path: str) -> bool:
    """Whether ``path`` is on the guarded surface (SURF-001).

    A suffix match AND a prefix match. A non-empty prefix matches by ``startswith``; the
    member ``""`` matches every path that does not start with ``.``.
    """
    if not path.endswith(TRACKED_SUFFIXES):
        return False
    if "" in TRACKED_PATH_PREFIXES and not path.startswith("."):
        return True
    return any(p and path.startswith(p) for p in TRACKED_PATH_PREFIXES)


def _staged_deliverable_files(project_dir: str, command: str, cwd: str) -> list[str]:
    """Guarded files in the prospective commit."""
    return [f for f in staged_paths(project_dir, command, cwd) if _on_surface(f)]


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
    # The shell's directory, not the project root: a commit made in a linked worktree
    # lands that worktree's index, which the project root's index says nothing about.
    cwd = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or "."

    verb = commit_creating_verb(command)
    if verb:
        _record_ungated_verb(os.environ.get("CLAUDE_PROJECT_DIR") or cwd, cwd, verb)

    if not is_git_commit(command):
        sys.exit(0)

    project_dir = commit_repo_dir(command, cwd)

    try:
        files = _staged_deliverable_files(project_dir, command, cwd)
    except ScopeError as exc:
        refusal = _verdict(command, project_dir, "scope-unreadable")
        deny(
            f"The commit scope could not be read: {exc}. This gate cannot tell whether the "
            f"commit touches a guarded file, so it denies. Fix the repository state, or "
            f"clear this local gate with {bypass_usage(BYPASS_ENV)}.{refusal}",
            project_dir,
            GATE,
        )
        sys.exit(0)
    if not files:
        # Non-deliverable commits (config, hooks, vision doc, README) — nothing to gate.
        sys.exit(0)

    sample = ", ".join(files[:3])
    more = f" (+{len(files) - 3} more)" if len(files) > 3 else ""
    refusal = _verdict(command, project_dir, "guarded-files")
    reason = (
        f"Commit touches {len(files)} guarded file(s): "
        f"{sample}{more}. Run an adversarial review ({REVIEWER_AGENT} agent) of the "
        f"change and read the result BEFORE committing, then record the verdict: "
        f"`kerbtap review record --review-output <verdict.json> "
        f"--verdict PASS --reviewer {REVIEWER_AGENT}` writes a content-addressed artifact and "
        f"prints a `Governor-Review` trailer — append that trailer to the commit message "
        f"so the Evidence Pack can resolve the review to the bytes that were reviewed. "
        f"That trailer is an integrity-checked record, not proof an independent reviewer "
        f"ran: the party it describes can author it. This gate does not check the "
        f"message for a trailer — it does not validate that the review actually "
        f"happened, by design (see the module docstring) — so to pass it you still "
        f"use {bypass_usage(BYPASS_ENV)}; the prefix only clears this local gate. "
        f"Committing WITHOUT recording a trailer leaves no record at all — note it in the "
        f"commit body (`{BYPASS_ENV}`) if you do, so the pack surfaces it as an override."
    )

    deny(reason + refusal, project_dir, GATE)
    sys.exit(0)


if __name__ == "__main__":
    main()
