#!/usr/bin/env python3
"""Git pre-commit adapter: runs the commit gates for a commit made outside Claude Code.

The gates read a Claude Code PreToolUse payload. This adapter gives each one the commit
as that payload. A gate's bypass variable is passed on only when it is exactly `1` in the
environment, because the gates read the bypass from the command text. Its `_REASON` and
`_UNTIL` variables go with it when set: a bypass with no reason is refused.

Exit 0 when every gate allows the commit. Exit 1, with the reason on stderr, when one refuses.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

GATES = (
    ("precommit_review_guard.py", "PROJECT_REVIEWED"),
    ("precommit_doc_consistency_guard.py", "PROJECT_DOC_REVIEWED"),
)
GUARDS = Path(__file__).resolve().parent.parent / "hooks"
TIMEOUT_SECONDS = 30


def _run_gate(guard, bypass_name):
    command = "git commit"
    if os.environ.get(bypass_name) == "1":
        prefix = bypass_name + "=1 "
        for suffix in ("_REASON", "_UNTIL"):
            value = os.environ.get(bypass_name + suffix)
            if value is not None:
                prefix += "{}{}={} ".format(bypass_name, suffix, shlex.quote(value))
        command = prefix + command
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": os.getcwd()}
    try:
        proc = subprocess.run(
            [sys.executable, str(guard)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return "{} could not run: {}".format(guard.name, exc)
    if proc.returncode == 0 and not proc.stdout.strip():
        return None
    if proc.returncode == 0:
        try:
            output = json.loads(proc.stdout)
            decision = output.get("hookSpecificOutput", {})
            if decision.get("permissionDecision") == "deny":
                return decision.get("permissionDecisionReason", proc.stdout.strip())
            if "systemMessage" in output:
                print(output["systemMessage"], file=sys.stderr)
                return None
        except (ValueError, AttributeError):
            pass
        return proc.stdout.strip()
    return "{} exited {}: {}".format(guard.name, proc.returncode, (proc.stderr or proc.stdout).strip())


def main():
    refused = False
    for name, bypass_name in GATES:
        guard = GUARDS / name
        if not guard.is_file():
            continue
        reason = _run_gate(guard, bypass_name)
        if reason is not None:
            print(reason, file=sys.stderr)
            refused = True
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
