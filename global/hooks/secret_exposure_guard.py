#!/usr/bin/env python3
"""PreToolUse hook — deny commands likely to print a secret to stdout.

The command is split into segments at shell operators and newlines outside
quotes; heredoc bodies are removed first, because a body is data. Each segment
is checked by three rules:

- **Env dump** — ``env`` or ``printenv`` (any path prefix) with no argument
  besides redirects: ``env | cat``, ``env 2>/dev/null``, ``/usr/bin/env > x``.
- **Env file reader** — a segment starting with a listed reader that reads a
  ``.env``-shaped path. ``.env.example``, ``.env.sample`` and ``.env.template``
  are allowed.
- **Secret echo** — ``echo`` with a ``$NAME`` or ``${NAME}`` whose name ends in
  KEY, TOKEN, SECRET, PASSWORD or PASSWD, also inside a word
  (``$API_KEY.txt``). shlex yields the same token for ``'$API_KEY'``, so the
  literal form is denied too; reword the text.

Quoted prose stays one token, so ``git commit -m 'ls; cat .env'`` passes.
Unbalanced quoting falls back to a whitespace split, noted on stderr.

Deliberately not attempted: indirection (``bash -c env``, ``eval``, ``xargs``,
``$(...)``), dump forms that need option semantics (``env FOO=1``,
``env -u PATH``, ``printenv -0``), and readers outside the list (``sed``,
``awk``, ``cut``, ``base64``). The reader list is a partial denylist.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SENSITIVE_SUFFIXES = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")

READERS = frozenset(
    {
        "cat", "source", ".", "less", "more", "head", "tail", "bat",
        "nl", "tac", "od", "xxd", "strings", "grep",
    }
)

TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})

DUMPERS = frozenset({"env", "printenv"})

# Characters shlex groups into operator tokens. A group without < or > ends a segment.
OPERATOR_CHARS = "();<>|&\n"

HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")

VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")


def is_env_path(token: str) -> bool:
    """True if `token` looks like a path to a real .env file, not a template."""
    name = token.split("/")[-1]
    return name.startswith(".env") and name not in TEMPLATES


def _heredoc_start(line: str, quote: str | None) -> tuple[re.Match | None, str | None]:
    """Find a `<<MARK` outside quotes in `line`; return it and the quote state at line end."""
    found = None
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = None
        elif c == "\\":
            i += 1
        elif c in "'\"":
            quote = c
        elif found is None and line.startswith("<<", i) and not line.startswith("<<<", i):
            found = HEREDOC.match(line, i)
        i += 1
    return found, quote


def strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies and their terminator lines; keep every command line."""
    out: list[str] = []
    marker, tabs, quote = None, False, None
    for line in cmd.split("\n"):
        if marker is not None:
            if (line.lstrip("\t") if tabs else line) == marker:
                marker = None
            continue
        out.append(line)
        found, quote = _heredoc_start(line, quote)
        if found:
            tabs, marker = found.group(1) == "-", found.group(3)
    return "\n".join(out)


def tokens(cmd: str) -> list[str]:
    """Tokenize with shell operators and newlines as separate tokens, quotes honoured."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=OPERATOR_CHARS)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        print("secret_exposure_guard: unbalanced quoting; checked by whitespace split", file=sys.stderr)
        return [t for line in cmd.split("\n") for t in (line.split() + ["\n"])]


def segments(cmd: str) -> list[list[str]]:
    """Split `cmd` into command segments; redirect operators stay in the segment."""
    out: list[list[str]] = []
    cur: list[str] = []
    for token in tokens(strip_heredocs(cmd)):
        if _is_separator(token):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(token)
    if cur:
        out.append(cur)
    return out


def _is_separator(token: str) -> bool:
    """An operator token, grouped or not (`;`, `&&`, `;\\n`), that is not a redirect."""
    return bool(token) and set(token) <= set(OPERATOR_CHARS) and not set(token) & set("<>")


def _is_redirect(token: str) -> bool:
    return bool(token) and set(token) <= set("<>&")


def _bare(segment: list[str]) -> bool:
    """True if `segment` has no argument after its first word, redirects aside."""
    rest = segment[1:]
    i = 0
    while i < len(rest):
        if _is_redirect(rest[i]):
            i += 2  # the operator and its target
        elif rest[i].isdigit() and i + 1 < len(rest) and _is_redirect(rest[i + 1]):
            i += 1  # a file descriptor before a redirect
        else:
            return False
    return True


def check(cmd: str) -> str | None:
    """Return a reason string if `cmd` looks like a secret disclosure, else None."""
    for segment in segments(cmd):
        first, rest = segment[0], segment[1:]
        if first.split("/")[-1] in DUMPERS and _bare(segment):
            return "env/printenv dump (pipe, redirect, or command-separator form)"
        if first in READERS and any(is_env_path(t) for t in rest):
            return f"'{first}' reading a .env-shaped path prints secrets to stdout"
        if first == "echo":
            for tok in rest:
                for name in VAR.findall(tok):
                    if name.endswith(SENSITIVE_SUFFIXES):
                        return f"echo of ${{{name}}}-shaped variable prints a secret to stdout"
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


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    command = data.get("tool_input", {}).get("command", "")
    if not command:
        sys.exit(0)

    reason = check(command)
    if reason is None:
        sys.exit(0)

    _log_deny(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or ".", "secret_exposure_guard", "secret-disclosure")
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Blocked: {reason}. This command would print a secret to a "
                "shared terminal. Run a narrower, single-credential read "
                "instead, or reword the text if this was a commit message "
                "or other prose."
            ),
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
