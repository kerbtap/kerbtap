#!/usr/bin/env python3
"""Integration test for secret_exposure_guard.py.

Drives the actual hook as a subprocess exactly as the harness invokes it: a
JSON payload on stdin, the deny decision (or silence) read back from stdout.

Run: ``python3 global/hooks/test_secret_exposure_guard.py`` (exit 0 = all pass).
No pytest dependency — plain asserts so it runs anywhere the hook does.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "secret_exposure_guard.py"
# Denies are logged to the repository the hook runs in; run the table outside any repository.
_SCRATCH = tempfile.mkdtemp()
_ENV = {**os.environ, "CLAUDE_PROJECT_DIR": _SCRATCH}


def _run_hook(command):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, text=True, capture_output=True,
        cwd=_SCRATCH, env=_ENV,
    )
    return '"permissionDecision": "deny"' in proc.stdout

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


CASES = [
    # (command, expect_denied)
    ("env", True),
    ("printenv", True),
    # Audit H4: piped/sequenced bare env must still be caught.
    ("env | cat", True),
    ("printenv | grep TOKEN", True),
    ("env; true", True),
    ("env|cat", True),
    ("env > /tmp/dump.txt", True),
    ("true && env", True),
    # Legitimate uses stay allowed.
    ("printenv PATH", False),
    ("env FOO=bar mycmd", False),
    ("git commit -m 'set env for tests'", False),
    # Existing checks unchanged.
    ("cat .env", True),
    ("cat infra/compose/.env", True),
    ("cat README.md", False),
    ("source .env", True),
    ("echo $API_KEY", True),
    ("echo hello", False),
    # Env dump: redirect and path-prefix forms.
    ("env 2>/dev/null", True),
    ("/usr/bin/env > leak", True),
    # Readers: the full list, per command segment, templates allowed.
    ("head .env", True),
    ("grep KEY .env", True),
    ("ls; cat .env", True),
    ("true && tail config/.env.local", True),
    ("cat .env.example", False),
    ("git commit -m 'ls; cat .env'", False),
    ("cat <<EOF > notes.md\nsee .env\nEOF", False),
    # Secret echo: brace form, a later segment, and the literal form.
    ("echo ${DB_PASSWORD}", True),
    ("ls && echo $GH_TOKEN", True),
    ("echo '$API_KEY'", True),
    ("echo $HOME", False),
    # Code review pass 1: heredoc, templates, quoted prose, newlines, embedded vars.
    ("cat <<EOF > notes.md\nbody\nEOF\ntrue; cat .env", True),
    ("cat <<'EOF' > notes.md\ncat .env\nEOF", False),
    ("cat <<-EOF > notes.md\n\tenv | cat\n\tEOF\nls", False),
    ("cat .env.production.example", True),
    ("git commit -m 'note; env | cat'", False),
    ('git commit -m "line one\nline two; cat .env"', False),
    ("'env' | cat", True),
    ("true\nenv", True),
    ("echo $API_KEY.txt", True),
    ("env 2 >/dev/null", True),
    ("env FOO=1 >/dev/null", False),
    ("cat .env 'unbalanced", True),
    # Code review pass 2: quoted heredoc marker, grouped separators.
    ("echo '<<EOF'\ncat .env", True),
    ('echo "a\n<<EOF"\ncat .env', True),
    ("true;\nenv", True),
    ("true &&\ncat .env", True),
    ("env 2>&1 | cat", True),
]


def check_deny_logged_to_old_folder(command, gate):
    """SF-001: in a repository that has `.governor/`, the deny line goes there, and no `.kerbtap/` is made."""
    problem = check_deny_logged(command, gate, old_folder=True)
    return problem.replace("check_deny_logged", "check_deny_logged_to_old_folder", 1) if problem else None


def main():
    failures = []
    for command, expect_denied in CASES:
        denied = _run_hook(command)
        if denied != expect_denied:
            failures.append(
                f"FAIL: {command!r} — expected denied={expect_denied}, got {denied}"
            )
        else:
            print(f"pass: {command!r} → denied={denied}")

    problem = check_deny_logged("env", "secret_exposure_guard")
    if problem:
        failures.append(problem)
    else:
        print("pass: check_deny_logged")
    problem = check_deny_logged_to_old_folder("env", "secret_exposure_guard")
    if problem:
        failures.append(problem)
    else:
        print("pass: check_deny_logged_to_old_folder")

    total = len(CASES) + 1 + 1
    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"\n{len(failures)}/{total} case(s) failed", file=sys.stderr)
        sys.exit(1)
    print(f"\nall {total} cases passed")


if __name__ == "__main__":
    main()
