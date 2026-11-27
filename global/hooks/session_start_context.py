#!/usr/bin/env python3
"""SessionStart hook — inject the repository's current state at session start.

A session that opens with no idea which branch it is on, what is uncommitted, or
what was last decided reasons from whatever it remembers — which is the state of the world at
some earlier session, not this one. This hook makes the first turn of every
session start from measured state instead.

Emits ``additionalContext``: branch, a truncated working-tree status, the recent
commits, and the newest decision-log headline with an age note when the log has
gone stale. It states no rules — the project's doctrine lives in its ``CLAUDE.md``
and is loaded separately; duplicating it here would create a second copy to drift.

User-tier hook: project-agnostic, wired once in ``~/.claude/settings.json``, fires
in every repository. The decision-log location is discovered from
:data:`DECISION_LOG_CANDIDATES` rather than hardcoded, so a project that keeps its
log somewhere else simply gets the git half of the context.

Fails silent: any git fault, unreadable log, or malformed payload yields the
context that could be gathered (possibly none) and exit 0. A SessionStart hook
that errors would block the session opening, and no context is a far smaller
problem than no session.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Checked in order; the first one that exists wins. Covers the two conventions
# seen across projects — a decisions/ directory and a single top-level log.
DECISION_LOG_CANDIDATES = (
    "docs/decisions/log.md",
    "docs/decision-log.md",
    "docs/decisions.md",
)

# A decision-log entry headline, in either house style: an ISO-dated heading
# (``## 2026-07-27 — ...``) or an ISO-dated list entry (``- **2026-07-27 · ...``).
# The date must be the first thing after the marker, so prose that merely
# mentions a date is not mistaken for an entry.
ENTRY_RE = re.compile(r"^\s*(?:#{1,6}\s+|[-*]\s+)(?:\*\*)?(20\d\d-\d\d-\d\d)\b")

STALE_AFTER_DAYS = 10
MAX_STATUS_LINES = 5
RECENT_COMMITS = 5
HEADLINE_CHARS = 140


def run(cmd, cwd):
    """Run *cmd* in *cwd*; return stripped stdout, or ``""`` on any fault.

    Fail-open by design (see the module docstring): a missing git, a
    non-repository cwd, and a timeout are all "no context available", not
    conditions worth failing a session over.
    """
    try:
        return subprocess.check_output(
            cmd, cwd=cwd, stderr=subprocess.DEVNULL, text=True, timeout=3
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def find_decision_log(project_dir: str) -> Path | None:
    """First existing decision log among :data:`DECISION_LOG_CANDIDATES`."""
    for rel in DECISION_LOG_CANDIDATES:
        path = Path(project_dir) / rel
        if path.is_file():
            return path
    return None


def latest_entry(log_path: Path) -> str:
    """Headline of the newest dated entry, or ``""`` if there is none.

    Assumes the newest-entry-first convention. Reads the whole file rather than
    streaming it — decision logs are prose-sized, and the simpler read is worth
    more than the saved bytes.
    """
    try:
        content = log_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in content.splitlines():
        if ENTRY_RE.match(line):
            return line.lstrip("#-* ").strip()[:HEADLINE_CHARS]
    return ""


def staleness_note(log_path: Path) -> str:
    """`` (last updated N days ago …)`` when the log is older than the threshold."""
    try:
        age_days = (time.time() - log_path.stat().st_mtime) / 86400
    except OSError:
        return ""
    if age_days <= STALE_AFTER_DAYS:
        return ""
    return f" (last updated {int(age_days)} days ago — verify anything you recall)"


def build_context(project_dir: str) -> str:
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], project_dir) or "unknown"
    status = run(["git", "status", "--short"], project_dir)
    recent = run(["git", "log", "--oneline", f"-{RECENT_COMMITS}"], project_dir)

    parts = ["SESSION CONTEXT (auto-injected)", "", f"Branch: {branch}"]

    status_lines = [ln for ln in status.split("\n") if ln.strip()]
    if status_lines:
        parts.append("Working tree:")
        parts.extend(f"  {ln}" for ln in status_lines[:MAX_STATUS_LINES])
        hidden = len(status_lines) - MAX_STATUS_LINES
        if hidden > 0:
            parts.append(f"  … +{hidden} more changed path(s)")

    if recent:
        parts.append("Recent commits:")
        parts.extend(f"  {ln}" for ln in recent.split("\n"))

    log_path = find_decision_log(project_dir)
    if log_path is not None:
        headline = latest_entry(log_path)
        if headline:
            parts.append(f"Last decision log entry: {headline}{staleness_note(log_path)}")

    return "\n".join(parts)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        # Non-object JSON (null / list / scalar) — no cwd to read, fall through
        # to the environment.
        data = {}

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or "."

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": build_context(project_dir),
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
