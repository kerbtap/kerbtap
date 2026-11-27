#!/usr/bin/env python3
"""PreToolUse hook — block destructive git commands that can lose user work.

Uses shlex tokenization so dangerous patterns inside quoted arguments
(e.g. commit messages) don't false-match.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


SEPARATORS = {"&&", "||", ";", "|"}
GLOBAL_OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree"}
GLOBAL_OPTS_JOINED = ("--git-dir=", "--work-tree=")


def git_invocations(tokens: list) -> list:
    """(subcommand, arguments) of each `git` call, past its global options (HOOK-009), to its end (HOOK-010)."""
    found = []
    for i, t in enumerate(tokens):
        if t != "git":
            continue
        j = i + 1
        while j < len(tokens):
            o = tokens[j]
            if o in GLOBAL_OPTS_WITH_ARG:
                j += 2
            elif o == "--no-pager" or o.startswith(GLOBAL_OPTS_JOINED):
                j += 1
            else:
                break
        if j < len(tokens):
            args = []
            for a in tokens[j + 1 :]:
                if a in SEPARATORS:
                    break
                args.append(a)
            found.append((tokens[j], args))
    return found


PUSH_OPTS_WITH_VALUE = {
    "--push-option",
    "--repo",
    "--receive-pack",
    "--exec",
    "--recurse-submodules",
}


def forces_push(args: list) -> bool:
    """--force, --force=<ref>, or -f (alone or grouped). --force-with-lease is not matched."""
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("--"):
            if a == "--force" or a.startswith("--force="):
                return True
            if a in PUSH_OPTS_WITH_VALUE:
                skip = True
        elif a.startswith("-") and len(a) > 1:
            for c in a[1:]:
                if c == "f":
                    return True
                if c == "o":
                    skip = a.endswith("o")
                    break
    return False


def deletes_remote_branch(args: list) -> bool:
    """HOOK-008: --delete (or a prefix), -d (alone or grouped), or :<name>."""
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("--"):
            if len(a) >= 4 and "--delete".startswith(a):
                return True
            if a in PUSH_OPTS_WITH_VALUE:
                skip = True
        elif a.startswith("-") and len(a) > 1:
            for c in a[1:]:
                if c == "d":
                    return True
                if c == "o":
                    skip = a.endswith("o")
                    break
        elif a.startswith(":") and len(a) > 1:
            return True
    return False


def check(tokens: list) -> str | None:
    """Return a reason string if command is destructive, else None."""
    for sub, args in git_invocations(tokens):
        if sub == "reset" and "--hard" in args:
            return "git reset --hard discards all uncommitted changes"
        if sub == "checkout" and args[:1] == ["--"]:
            return "git checkout -- <path> discards uncommitted edits"
        if sub == "checkout" and args[:1] == ["."]:
            return "git checkout . discards uncommitted edits"
        if sub == "restore" and args[:1] == ["."]:
            return "git restore . discards uncommitted edits"
        if sub == "restore" and "--worktree" in args:
            return "git restore --worktree discards uncommitted edits"
        if sub == "branch" and "-D" in args:
            return "git branch -D force-deletes a branch"
        if sub == "clean" and args[:1] and args[0].startswith("-") and "f" in args[0]:
            return "git clean -f deletes untracked files permanently"
        # --force-with-lease is deliberately not matched: a feature-branch
        # rewrite does not justify a hard gate. Reaching for it only because
        # this pattern misses it is still the route this hook bars.
        if sub == "push" and forces_push(args):
            return (
                "git push --force rewrites pushed history. Take the "
                "non-rewriting path (merge origin/main, do not rebase a "
                "pushed branch); if a rewrite is genuinely required, stop "
                "and let the user decide and run it"
            )
        if sub == "push" and deletes_remote_branch(args):
            return (
                "git push --delete / -d / :<branch> is a remote branch "
                "delete and removes the shared copy"
            )
    return None


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
    if not command:
        sys.exit(0)

    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        # Malformed shell — let it through rather than blocking by accident
        sys.exit(0)

    reason = check(tokens)
    if reason is None:
        sys.exit(0)

    _log_deny(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or ".", "destructive_git_guard", "destructive-git")
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Destructive git command blocked: {reason}. "
                f"If you need this, ask the user to run it manually."
            ),
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
