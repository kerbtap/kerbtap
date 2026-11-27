#!/usr/bin/env python3
"""PreToolUse hook — deny pushes that land on main.

Per feedback_never_build_on_main: ALL work — features, bug fixes, docs —
goes via branch + PR; nothing is pushed directly to main (post-MVP,
2026-06-12). The guard denies any push whose target resolves to main:

- explicit: ``git push origin main``, ``git push origin HEAD:main``,
  any ``<ref>:main`` refspec (security audit M4 — the old regex matched
  only the literal ``origin main`` form);
- implicit: a plain ``git push`` while on main, or from a checkout whose
  upstream is ``<remote>/main``.

Runs in ``permissionDecision: "deny"`` mode — ``"ask"`` is silently
auto-approved when ``skipAutoPermissionPrompt: true`` is set in the user's
settings, which made the previous soft-block a no-op. Server-side branch
protection is the real control; this hook is the local fast-fail.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Characters shlex groups into operator tokens when punctuation_chars=True.
_PUNCTUATION = set("();<>|&")


def run(cmd, cwd):
    try:
        return subprocess.check_output(
            cmd, cwd=cwd, stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _is_separator(token: str) -> bool:
    return bool(token) and all(c in _PUNCTUATION for c in token)


def _tokenize(command: str) -> list[str]:
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return []


def _push_target(tokens: list[str]) -> str | None:
    """Classify each ``git push`` in the command.

    Returns ``"explicit-main"`` when a refspec targets main, ``"implicit"``
    when a push carries no refspec (target decided by branch/upstream), and
    ``None`` when no push is present or every push targets another ref.
    """
    implicit = False
    for i, token in enumerate(tokens):
        if token != "git" or tokens[i + 1 : i + 2] != ["push"]:
            continue
        positionals = []
        for arg in tokens[i + 2 :]:
            if _is_separator(arg):
                break
            if arg.startswith("-"):
                continue
            positionals.append(arg)
        # First positional is the remote; the rest are refspecs.
        refspecs = positionals[1:]
        if any(r == "main" or r.endswith(":main") for r in refspecs):
            return "explicit-main"
        if not refspecs:
            implicit = True
    return "implicit" if implicit else None


def _state_folder(root):
    """The state folder name below `root` (SF-001): `.governor` when it exists, else `.kerbtap`."""
    old = Path(root) / ".governor"
    return ".governor" if old.exists() or old.is_symlink() else ".kerbtap"


def _log_deny(directory, gate, reason_code):
    """Append one deny line to `<main worktree>/<state folder>/gate-log/events.jsonl` (GEL-001).

    Same line format as `record_event` in the project gates' `commit_scope.py`. The log is
    a local file that any local user can edit. Never raises: the deny does not depend on it.
    """

    def git(*args):
        return subprocess.check_output(
            ["git", *args], cwd=directory, stderr=subprocess.DEVNULL, text=True, timeout=2
        ).strip()

    try:
        common = Path(git("rev-parse", "--git-common-dir"))
        root = (common if common.is_absolute() else Path(directory) / common).resolve().parent
        try:
            branch, head = git("rev-parse", "--abbrev-ref", "HEAD"), git("rev-parse", "HEAD")
        except (subprocess.SubprocessError, OSError, ValueError):
            branch = head = None
        line = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "gate": gate,
            "decision": "deny",
            "reason_code": reason_code,
            "bypass_form": None,
            "bypass_reason": None,
            "expires": None,
            "branch": branch,
            "head": head,
        }
        log = root / _state_folder(root) / "gate-log" / "events.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        ignore = log.parent / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
    except (subprocess.SubprocessError, OSError, ValueError, TypeError):
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    command = data.get("tool_input", {}).get("command", "")
    if "push" not in command:
        sys.exit(0)

    target = _push_target(_tokenize(command))
    if target is None:
        sys.exit(0)

    # The payload's cwd is where the push runs; CLAUDE_PROJECT_DIR stays the
    # main checkout when the session works in a worktree.
    project_dir = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or "."

    if target == "implicit":
        # Only a problem if the push would land on main via branch or upstream.
        branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], project_dir)
        upstream = run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            project_dir,
        )
        if branch != "main" and not upstream.endswith("/main"):
            sys.exit(0)

    _log_deny(project_dir, "main_branch_push_guard", "push-to-main")
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Push targets main. Per feedback_never_build_on_main: ALL work "
                "(features, bug fixes, docs) goes via branch + PR — a direct "
                "push to main is a policy violation. Push a feature branch and "
                "open a PR instead."
            ),
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
