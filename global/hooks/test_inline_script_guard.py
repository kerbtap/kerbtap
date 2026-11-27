#!/usr/bin/env python3
"""Integration test for inline_script_guard.py.

Drives the real hook as a subprocess with a PreToolUse payload on stdin and reads
the decision back from stdout. Each deny is paired with a near miss it must let through.

Run: ``python3 global/hooks/test_inline_script_guard.py`` (exit 0 = all pass).
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "inline_script_guard.py"
# Denies are logged to the repository the hook runs in; run the table outside any repository.
_SCRATCH = tempfile.mkdtemp()
_ENV = {**os.environ, "CLAUDE_PROJECT_DIR": _SCRATCH}

LONG = "x" * 151
CASES = [
    # (command, expect_denied)
    (f"python3 -c '{LONG}'", True),
    (f'python -c "{LONG}"', True),
    (f"node -e '{LONG}'", True),
    ("python3 -c 'print(1)\nprint(2)'", True),
    ("python3 -c 'print(1)'", False),
    ("node -e 'console.log(1)'", False),
    ("python3 script.py", False),
    ("echo hello", False),
    # long heredoc into python3
    (f"python3 - <<'EOF'\n{LONG}\nEOF", True),
    # short heredoc into python3
    ("python3 <<'EOF'\nprint(1)\nprint(2)\nEOF", False),
    # long pipe into python3
    (f"echo '{LONG}' | python3 -", True),
    # 150-character pipe into python3: the whole command is longer than 150
    (f"echo '{'x' * 150}' | python3 -", False),
    # long heredoc piped into python3
    (f"cat <<'EOF' | python3\n{LONG}\nEOF", True),
    # short -c match must not hide a long heredoc later in the command
    (f"python3 -c 'import sys' && python3 - <<'EOF'\n{LONG}\nEOF", True),
    # a `;` inside quotes does not end the producer command
    (f"echo 'a;{LONG}' | python3 -", True),
    # a delimiter that is not a plain word
    (f"python3 <<'E-O.F'\n{LONG}\nE-O.F", True),
    # an indented line is not the delimiter of a plain `<<`
    (f"python3 <<EOF\nprint(1)\n  EOF\n{LONG}\nEOF", True),
    # a trailing comment does not hide the pipe
    (f"echo '{LONG}' | python3 - # note", True),
    # a redirection after the interpreter does not hide the pipe
    (f"echo '{LONG}' | python3 - > /tmp/result", True),
    # `python3` after a redirection is a file name, not a command
    (f"cat > python3 <<'EOF'\n{LONG}\nEOF", False),
    # long heredoc into cat
    (f"cat > out.txt <<'EOF'\n{LONG}\nEOF", False),
]

GATE_LOG = Path(".kerbtap") / "gate-log" / "events.jsonl"


def _run(command, tool_name="Bash", cwd=None):
    payload = {"tool_name": tool_name, "tool_input": {"command": command}}
    env = _ENV
    if cwd:
        payload["cwd"] = cwd
        env = {**os.environ, "CLAUDE_PROJECT_DIR": cwd}
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), text=True,
        capture_output=True, cwd=_SCRATCH, env=env,
    )


def check_deny_logged(old_folder=False):
    """GEL-001: a deny inside a repository writes one deny line naming the gate."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
        if old_folder:
            (Path(tmp) / ".governor").mkdir()
        proc = _run(f"python3 -c '{LONG}'", cwd=tmp)
        log = Path(tmp) / (".governor" / GATE_LOG.relative_to(".kerbtap") if old_folder else GATE_LOG)
        lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        stray = old_folder and (Path(tmp) / ".kerbtap").exists()
    ok = (
        not stray
        and '"permissionDecision": "deny"' in proc.stdout
        and len(lines) == 1 and lines[0]["gate"] == "inline_script_guard"
        and lines[0]["decision"] == "deny" and lines[0]["reason_code"] == "inline-script"
    )
    return None if ok else f"FAIL: check_deny_logged\n  stdout={proc.stdout!r} log={lines}"


def check_deny_logged_to_old_folder():
    """SF-001: in a repository that has `.governor/`, the deny line goes there, and no `.kerbtap/` is made."""
    problem = check_deny_logged(old_folder=True)
    return problem.replace("check_deny_logged", "check_deny_logged_to_old_folder", 1) if problem else None


def main():
    failures = []
    for command, expect in CASES:
        got = '"permissionDecision": "deny"' in _run(command).stdout
        if got != expect:
            failures.append(f"FAIL: {command!r} expected denied={expect}, got {got}")
        else:
            print(f"pass: {command[:40]!r}")
    if '"permissionDecision": "deny"' in _run(f"python3 -c '{LONG}'", tool_name="Edit").stdout:
        failures.append("FAIL: a non-Bash tool call was denied")
    else:
        print("pass: non-Bash tool call ignored")
    problem = check_deny_logged()
    if problem:
        failures.append(problem)
    else:
        print("pass: check_deny_logged")
    problem = check_deny_logged_to_old_folder()
    if problem:
        failures.append(problem)
    else:
        print("pass: check_deny_logged_to_old_folder")
    total = len(CASES) + 2 + 1
    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"\n{len(failures)}/{total} case(s) failed", file=sys.stderr)
        sys.exit(1)
    print(f"\nall {total} cases passed")


if __name__ == "__main__":
    main()
