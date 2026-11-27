#!/usr/bin/env python3
"""Integration test for session_start_context.py.

Drives the actual hook as a subprocess, exactly as the harness invokes it: a
SessionStart JSON payload on stdin, ``CLAUDE_PROJECT_DIR`` in the environment,
and the emitted ``additionalContext`` read back from stdout.

Every check the hook performs has a case that makes it FIRE and a case that makes
it stay quiet — a test that only ever exercises the quiet path cannot tell a
working check from a check that is wired to nothing.

Run: ``python3 global/hooks/test_session_start_context.py`` (exit 0 = all pass).
No pytest dependency — plain asserts so it runs anywhere the hook does.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "session_start_context.py"

HEADING_LOG = """# Decisions

## 2026-07-27 — the heading-style entry

Body prose mentioning 2026-01-01 which is not an entry.

## 2026-07-01 — an older entry
"""

BULLET_LOG = """# Decision Log

Format per entry: date, what, why.

## Decided

- **2026-07-27 · the bullet-style entry.** Body text.
- **2026-07-01 · an older entry.** Body text.
"""

PROSE_ONLY_LOG = """# Decisions

Nothing decided yet. Written 2026-07-27, but that is prose, not an entry.
"""


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _repo(tmp, commits=1):
    """Initialise a throwaway repo with *commits* commits on a known branch."""
    _git(["init", "-b", "work"], tmp)
    _git(["config", "user.email", "test@example.com"], tmp)
    _git(["config", "user.name", "Test"], tmp)
    for i in range(commits):
        (Path(tmp) / f"f{i}.txt").write_text("x\n")
        _git(["add", f"f{i}.txt"], tmp)
        _git(["commit", "-m", f"commit {i}"], tmp)
    return tmp


def _write_log(tmp, rel, content, age_days=0):
    path = Path(tmp) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
    return path


def _run_hook(project_dir, payload=None):
    """Invoke the hook; return (exit_code, additionalContext, raw_stdout)."""
    if payload is None:
        payload = json.dumps({"hook_event_name": "SessionStart", "cwd": project_dir})
    env = {**os.environ, "CLAUDE_PROJECT_DIR": project_dir}
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, text=True,
        capture_output=True, env=env,
    )
    context = ""
    try:
        context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return proc.returncode, context, proc.stdout


# --- checks ---------------------------------------------------------------
# Each returns a failure string, or None when it passes.


def check_branch_and_commits(tmp):
    _repo(tmp, commits=2)
    code, ctx, out = _run_hook(tmp)
    if code != 0:
        return f"expected exit 0, got {code} (stdout={out!r})"
    if "Branch: work" not in ctx:
        return f"branch not reported in context: {ctx!r}"
    if "commit 1" not in ctx or "Recent commits:" not in ctx:
        return f"recent commits not reported: {ctx!r}"
    return None


def check_clean_tree_omits_status(tmp):
    _repo(tmp)
    _, ctx, _ = _run_hook(tmp)
    if "Working tree:" in ctx:
        return f"clean tree should not emit a status block: {ctx!r}"
    return None


def check_dirty_tree_reports_status(tmp):
    _repo(tmp)
    (Path(tmp) / "dirty.txt").write_text("y\n")
    _, ctx, _ = _run_hook(tmp)
    if "Working tree:" not in ctx or "dirty.txt" not in ctx:
        return f"dirty tree not reported: {ctx!r}"
    return None


def check_status_truncation_fires(tmp):
    _repo(tmp)
    for i in range(8):  # > MAX_STATUS_LINES
        (Path(tmp) / f"extra{i}.txt").write_text("y\n")
    _, ctx, _ = _run_hook(tmp)
    if "+3 more changed path(s)" not in ctx:
        return f"truncation note missing for 8 changed paths: {ctx!r}"
    return None


def check_status_truncation_silent_under_limit(tmp):
    _repo(tmp)
    for i in range(3):  # < MAX_STATUS_LINES
        (Path(tmp) / f"extra{i}.txt").write_text("y\n")
    _, ctx, _ = _run_hook(tmp)
    if "more changed path(s)" in ctx:
        return f"truncation note fired below the limit: {ctx!r}"
    return None


def check_heading_style_log(tmp):
    _repo(tmp)
    _write_log(tmp, "docs/decisions/log.md", HEADING_LOG)
    _, ctx, _ = _run_hook(tmp)
    if "2026-07-27 — the heading-style entry" not in ctx:
        return f"heading-style headline not extracted: {ctx!r}"
    if "2026-07-01" in ctx:
        return f"older entry leaked — newest-first assumption broken: {ctx!r}"
    return None


def check_bullet_style_log(tmp):
    _repo(tmp)
    _write_log(tmp, "docs/decision-log.md", BULLET_LOG)
    _, ctx, _ = _run_hook(tmp)
    if "2026-07-27 · the bullet-style entry" not in ctx:
        return f"bullet-style headline not extracted: {ctx!r}"
    return None


def check_candidate_precedence(tmp):
    """docs/decisions/log.md wins over docs/decision-log.md — declared order."""
    _repo(tmp)
    _write_log(tmp, "docs/decisions/log.md", HEADING_LOG)
    _write_log(tmp, "docs/decision-log.md", BULLET_LOG)
    _, ctx, _ = _run_hook(tmp)
    if "the heading-style entry" not in ctx or "the bullet-style entry" in ctx:
        return f"candidate precedence not honoured: {ctx!r}"
    return None


def check_prose_date_is_not_an_entry(tmp):
    _repo(tmp)
    _write_log(tmp, "docs/decision-log.md", PROSE_ONLY_LOG)
    _, ctx, _ = _run_hook(tmp)
    if "Last decision log entry" in ctx:
        return f"a bare prose date was mistaken for an entry: {ctx!r}"
    return None


def check_stale_note_fires(tmp):
    _repo(tmp)
    _write_log(tmp, "docs/decision-log.md", BULLET_LOG, age_days=40)
    _, ctx, _ = _run_hook(tmp)
    if "last updated 40 days ago" not in ctx:
        return f"staleness note missing on a 40-day-old log: {ctx!r}"
    return None


def check_stale_note_silent_when_fresh(tmp):
    _repo(tmp)
    _write_log(tmp, "docs/decision-log.md", BULLET_LOG)
    _, ctx, _ = _run_hook(tmp)
    if "last updated" in ctx:
        return f"staleness note fired on a fresh log: {ctx!r}"
    return None


def check_no_log_still_reports_git(tmp):
    _repo(tmp)
    _, ctx, _ = _run_hook(tmp)
    if "Branch: work" not in ctx or "Last decision log entry" in ctx:
        return f"missing-log path degraded wrongly: {ctx!r}"
    return None


def check_non_repo_is_survivable(tmp):
    # No git init at all.
    code, ctx, out = _run_hook(tmp)
    if code != 0:
        return f"expected exit 0 outside a repo, got {code} (stdout={out!r})"
    if "Branch: unknown" not in ctx:
        return f"expected an unknown branch outside a repo: {ctx!r}"
    return None


def check_malformed_stdin_is_survivable(tmp):
    _repo(tmp)
    code, ctx, out = _run_hook(tmp, payload="not json at all")
    if code != 0:
        return f"expected exit 0 on malformed stdin, got {code} (stdout={out!r})"
    if "Branch: work" not in ctx:
        return f"context should still build from CLAUDE_PROJECT_DIR: {ctx!r}"
    return None


def check_non_object_json_is_survivable(tmp):
    _repo(tmp)
    code, ctx, out = _run_hook(tmp, payload="[1, 2, 3]")
    if code != 0:
        return f"expected exit 0 on non-object JSON, got {code} (stdout={out!r})"
    if "Branch: work" not in ctx:
        return f"context should still build from CLAUDE_PROJECT_DIR: {ctx!r}"
    return None


CASES = [
    ("branch + recent commits reported", check_branch_and_commits),
    ("clean tree omits the status block", check_clean_tree_omits_status),
    ("dirty tree reports the status block", check_dirty_tree_reports_status),
    ("status truncation note fires above the limit", check_status_truncation_fires),
    ("status truncation note silent below the limit", check_status_truncation_silent_under_limit),
    ("heading-style decision log parsed", check_heading_style_log),
    ("bullet-style decision log parsed", check_bullet_style_log),
    ("candidate path precedence honoured", check_candidate_precedence),
    ("prose date is not an entry", check_prose_date_is_not_an_entry),
    ("staleness note fires on an old log", check_stale_note_fires),
    ("staleness note silent on a fresh log", check_stale_note_silent_when_fresh),
    ("missing decision log still reports git", check_no_log_still_reports_git),
    ("outside a git repo the hook survives", check_non_repo_is_survivable),
    ("malformed stdin survives", check_malformed_stdin_is_survivable),
    ("non-object JSON stdin survives", check_non_object_json_is_survivable),
]


def main():
    failures = []
    for desc, check in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            problem = check(tmp)
        if problem:
            failures.append(f"FAIL: {desc}\n  {problem}")
        else:
            print(f"pass: {desc}")

    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"\n{len(failures)}/{len(CASES)} case(s) failed", file=sys.stderr)
        sys.exit(1)
    print(f"\nall {len(CASES)} cases passed")


if __name__ == "__main__":
    main()
