#!/usr/bin/env python3
"""Integration test for destructive_git_guard.py.

Drives the real hook as a subprocess with a PreToolUse payload on stdin and reads
the decision back from stdout. Every rule is exercised in both directions: a
command it must deny and a near miss it must let through. A hook that crashed at
import would print nothing, so every deny case fails against it.

Run: ``python3 test_destructive_git_guard.py``
(exit 0 = all pass).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "destructive_git_guard.py"
# Denies are logged to the repository the hook runs in; run the table outside any repository.
_SCRATCH = tempfile.mkdtemp()
_ENV = {**os.environ, "CLAUDE_PROJECT_DIR": _SCRATCH}

CASES = [
    # (command, expect_denied)
    ("git reset --hard", True),
    ("git reset --hard HEAD~1", True),
    ("git reset --soft HEAD~1", False),
    ("git checkout -- src/app.py", True),
    ("git checkout .", True),
    ("git checkout -b feature", False),
    ("git restore .", True),
    ("git restore --worktree src/app.py", True),
    ("git restore --staged src/app.py", False),
    ("git branch -D old", True),
    ("git branch -d old", False),
    ("git clean -fd", True),
    ("git clean -n", False),
    ("git push --force origin feature", True),
    ("git push --force=feature origin", True),
    ("git push --force-with-lease origin feature", False),
    ("git status && git reset --hard", True),
    ('git commit -m "never run git reset --hard"', False),
    ("git push origin --delete old", True),
    ("git push --delete origin old", True),
    ("git push -d origin old", True),
    ("git push -ud origin old", True),
    ("git push origin :old", True),
    ("git push origin :refs/heads/old", True),
    ("git push origin old", False),
    ("git push origin HEAD:old", False),
    ("git push origin :", False),
    ('git commit -m "git push origin --delete old"', False),
    ("git -C . push --delete origin old", True),
    ("git push origin old && git branch -d old", False),
    ("GIT_TRACE=1 git push origin --delete old", True),
    ("git status;git push origin --delete old", True),
    ("git push origin old;git branch -d old", False),
    ("git push -odeploy origin main", False),
    ("git push --push-option :old origin main", False),
    ("git push --del origin old", True),
    ("git push origin a#b --delete old", True),
    # global options before the subcommand, and the short force flag
    ("git -C . push --force origin feature", True),
    ("git --git-dir=.git push --force origin feature", True),
    ("git -C . reset --hard", True),
    ("git -c core.x=1 checkout -- src/app.py", True),
    ("git -C . branch -D old", True),
    ("git -C . clean -fd", True),
    ("git push -f origin feature", True),
    ("git push -uf origin feature", True),
    ("git -C . push -f origin feature", True),
    ("git push -o f origin feature", False),
    ("git -C . push origin feature", False),
]


def denied(command: str, tool_name: str = "Bash") -> bool:
    payload = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, text=True, capture_output=True,
        cwd=_SCRATCH, env=_ENV,
    )
    if proc.returncode != 0:
        raise AssertionError(f"hook exited {proc.returncode}: {proc.stderr.strip()}")
    return '"permissionDecision": "deny"' in proc.stdout


def deny_reason(command: str) -> str:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    out = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, text=True, capture_output=True,
        cwd=_SCRATCH, env=_ENV,
    ).stdout
    return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out else ""


GATE_LOG = Path(".kerbtap") / "gate-log" / "events.jsonl"


def check_deny_logged(command, gate, old_folder=False):
    """GEL-001: a deny inside a repository writes one deny line naming the gate."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
        if old_folder:
            (Path(tmp) / ".governor").mkdir()
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": tmp})
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input=payload, text=True, capture_output=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": tmp},
        )
        log = Path(tmp) / (".governor" / GATE_LOG.relative_to(".kerbtap") if old_folder else GATE_LOG)
        lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        stray = old_folder and (Path(tmp) / ".kerbtap").exists()
    ok = (
        not stray
        and '"permissionDecision": "deny"' in proc.stdout
        and len(lines) == 1 and lines[0]["gate"] == gate and lines[0]["decision"] == "deny"
    )
    return None if ok else f"FAIL: check_deny_logged\n  stdout={proc.stdout!r} log={lines}"


def check_deny_logged_to_old_folder(command, gate):
    """SF-001: in a repository that has `.governor/`, the deny line goes there, and no `.kerbtap/` is made."""
    problem = check_deny_logged(command, gate, old_folder=True)
    return problem.replace("check_deny_logged", "check_deny_logged_to_old_folder", 1) if problem else None


def main() -> None:
    failures = []
    for command, expect in CASES:
        got = denied(command)
        if got != expect:
            failures.append(f"FAIL: {command!r} expected denied={expect}, got {got}")
        else:
            print(f"pass: {command!r}")
    if denied("git reset --hard", tool_name="Edit"):
        failures.append("FAIL: a non-Bash tool call was denied")
    else:
        print("pass: non-Bash tool call ignored")
    reason = deny_reason("git push --force origin feature")
    if "rewrites pushed history" in reason and "let the user decide" in reason:
        print("pass: force-push reason names the non-rewriting path")
    else:
        failures.append(f"FAIL: force-push reason is {reason!r}")
    reason = deny_reason("git push origin --delete old")
    if "remote branch delete" in reason:
        print("pass: remote delete reason names a remote branch delete")
    else:
        failures.append(f"FAIL: remote delete reason is {reason!r}")
    problem = check_deny_logged("git reset --hard", "destructive_git_guard")
    if problem:
        failures.append(problem)
    else:
        print("pass: check_deny_logged")
    problem = check_deny_logged_to_old_folder("git reset --hard", "destructive_git_guard")
    if problem:
        failures.append(problem)
    else:
        print("pass: check_deny_logged_to_old_folder")
    total = len(CASES) + 4 + 1
    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"\n{len(failures)}/{total} case(s) failed", file=sys.stderr)
        sys.exit(1)
    print(f"\nall {total} cases passed")


if __name__ == "__main__":
    main()
