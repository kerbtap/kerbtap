#!/usr/bin/env python3
"""Integration test for main_branch_push_guard.py.

Drives the actual hook as a subprocess: JSON payload on stdin,
``CLAUDE_PROJECT_DIR`` in the environment, deny decision (or silence) read
back from stdout.

Run: ``python3 global/hooks/test_main_branch_push_guard.py`` (exit 0 = all pass).
No pytest dependency — plain asserts so it runs anywhere the hook does.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "main_branch_push_guard.py"


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _run_hook(project_dir, command):
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": project_dir}
    )
    env = {**os.environ, "CLAUDE_PROJECT_DIR": project_dir}
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, text=True,
        capture_output=True, env=env,
    )
    return '"permissionDecision": "deny"' in proc.stdout


def _make_repo(tmp, branch, upstream_main=False):
    _git(["init", "-b", branch], tmp)
    (Path(tmp) / "a.txt").write_text("x\n")
    _git(["add", "a.txt"], tmp)
    _git(["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-m", "init"], tmp)
    if upstream_main:
        _git(["remote", "add", "origin", "https://example.invalid/r.git"], tmp)
        _git(["update-ref", "refs/remotes/origin/main", "HEAD"], tmp)
        _git(["config", f"branch.{branch}.remote", "origin"], tmp)
        _git(["config", f"branch.{branch}.merge", "refs/heads/main"], tmp)

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


# (description, current branch, upstream-is-main, command, expect_denied)
CASES = [
    ("explicit origin main → deny", "feature", False, "git push origin main", True),
    ("explicit HEAD:main → deny (audit M4)", "feature", False, "git push origin HEAD:main", True),
    ("explicit ref:main → deny", "feature", False, "git push origin fix:main", True),
    ("explicit with flags → deny", "feature", False, "git push --force-with-lease origin main", True),
    ("feature branch push → silent", "feature", False, "git push origin feature", False),
    ("main-lookalike ref → silent", "feature", False, "git push origin main-backup", False),
    ("plain push on main → deny", "main", False, "git push", True),
    ("plain push, upstream is main → deny (audit M4)", "feature", True, "git push", True),
    ("plain push on feature, no main upstream → silent", "feature", False, "git push", False),
    ("non-push command → silent", "main", False, "git status", False),
]


def _decision(command, cwd, project_dir, with_cwd=True):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    if with_cwd:
        payload["cwd"] = str(cwd)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project_dir)}
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), text=True,
        capture_output=True, env=env,
    )
    return '"permissionDecision": "deny"' in proc.stdout


def _worktree_cases():
    """A main checkout on main and a worktree on feature, both tracking origin.

    The payload's cwd decides; CLAUDE_PROJECT_DIR is only the fallback.
    """
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        origin, main, wt = tmp / "origin.git", tmp / "main", tmp / "wt"
        _git(["init", "--bare", "-b", "main", str(origin)], tmp)
        _git(["clone", str(origin), str(main)], tmp)
        _git(["-c", "user.name=t", "-c", "user.email=t@example.com",
              "commit", "--allow-empty", "-m", "init"], main)
        _git(["push", "-u", "origin", "main"], main)
        _git(["worktree", "add", "-b", "feature", str(wt)], main)
        _git(["push", "-u", "origin", "feature"], wt)
        cases = [
            ("bare push from feature worktree, project dir on main → silent",
             _decision("git push", wt, main), False),
            ("bare push from main checkout, project dir is worktree → deny",
             _decision("git push", main, wt), True),
            ("explicit HEAD:main from worktree → deny",
             _decision("git push origin HEAD:main", wt, main), True),
            ("no cwd in payload → falls back to project dir on main → deny",
             _decision("git push", wt, main, with_cwd=False), True),
        ]
        for desc, got, want in cases:
            results.append((desc, got == want, f"expected denied={want}, got {got}"))
    return results


def check_deny_logged_to_old_folder(command, gate):
    """SF-001: in a repository that has `.governor/`, the deny line goes there, and no `.kerbtap/` is made."""
    problem = check_deny_logged(command, gate, old_folder=True)
    return problem.replace("check_deny_logged", "check_deny_logged_to_old_folder", 1) if problem else None


def main():
    failures = []
    for desc, ok, detail in _worktree_cases():
        if ok:
            print(f"pass: {desc}")
        else:
            failures.append(f"FAIL: {desc}\n  {detail}")
    for desc, branch, upstream_main, command, expect_denied in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            _make_repo(tmp, branch, upstream_main)
            denied = _run_hook(tmp, command)
            if denied != expect_denied:
                failures.append(
                    f"FAIL: {desc}\n  expected denied={expect_denied}, got {denied}"
                )
            else:
                print(f"pass: {desc}")

    problem = check_deny_logged("git push origin main", "main_branch_push_guard")
    if problem:
        failures.append(problem)
    else:
        print("pass: check_deny_logged")
    problem = check_deny_logged_to_old_folder("git push origin main", "main_branch_push_guard")
    if problem:
        failures.append(problem)
    else:
        print("pass: check_deny_logged_to_old_folder")

    total = len(CASES) + 5 + 1
    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"\n{len(failures)}/{total} case(s) failed", file=sys.stderr)
        sys.exit(1)
    print(f"\nall {total} cases passed")


if __name__ == "__main__":
    main()
