#!/usr/bin/env python3
"""PreToolUse hook — soft-block large inline python/node scripts.

Trivial one-liners (version checks, short prints) pass through. A `-c`/`-e`
argument longer than 150 chars or containing a newline is denied. So is a
heredoc into python/node, or a script piped into python/node, whose script
text is longer than 150 chars (newlines alone do not deny these). Write a
proper script file instead.
"""
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Captures 'python -c', 'python3 -c', 'node -e' with single or double quotes
INLINE_SINGLE = re.compile(r"\b(python3?|node)\s+-[ce]\s+'([^']*)'", re.DOTALL)
INLINE_DOUBLE = re.compile(r'\b(python3?|node)\s+-[ce]\s+"([^"]*)"', re.DOTALL)

THRESHOLD_CHARS = 150

HEREDOC_OP = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(\S+)")  # not `<<<`
MARKER = re.compile(r"^GOVHD(\d+)X$")
INTERPRETER = re.compile(r"^(python3?|node)$")
ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
PUNCTUATION = ";&|<>()\n"


def _split_heredocs(command):
    """Cut heredoc bodies out of `command`.

    Returns the shell text, with each heredoc operator replaced by a marker word, and the
    bodies by marker number. A body ends at a line that is the delimiter exactly; with
    `<<-` leading tabs are dropped first. An unterminated body runs to the end.
    """
    lines = command.split("\n")
    shell, bodies = [], []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        op = HEREDOC_OP.search(line)
        if op:
            delimiter = re.sub(r"['\"\\]", "", op.group(2))
            body = []
            while i < len(lines):
                current = lines[i]
                i += 1
                if (current.lstrip("\t") if op.group(1) else current) == delimiter:
                    break
                body.append(current)
            line = f"{line[:op.start()]} GOVHD{len(bodies)}X {line[op.end():]}"
            bodies.append("\n".join(body))
        shell.append(line)
    return "\n".join(shell), bodies


def _simple_commands(text):
    """Tokenize shell text into simple commands: dicts with `args`, `markers`, `piped`.

    Raises ValueError on an unterminated quote.
    """
    lex = shlex.shlex(text, posix=True, punctuation_chars=PUNCTUATION)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    commands = []
    current = {"args": [], "markers": [], "piped": False}
    skip = comment = False
    for token in lex:
        is_punctuation = all(ch in PUNCTUATION for ch in token)
        if comment and not (is_punctuation and "\n" in token):
            continue
        comment = False
        if is_punctuation and not any(ch in "<>" for ch in token):
            commands.append(current)
            current = {"args": [], "markers": [], "piped": token.strip("\n") in ("|", "|&")}
            skip = False
        elif skip:
            skip = False
        elif is_punctuation:
            skip = True  # a redirection: its target is not a command word
        elif token.startswith("#"):
            comment = True
        elif MARKER.match(token):
            current["markers"].append(int(MARKER.match(token).group(1)))
        elif not (ASSIGNMENT.match(token) and not current["args"]):
            current["args"].append(token)
    commands.append(current)
    return commands


def _long_heredoc_or_pipe(command):
    """Return the length of the first script above the limit that python/node reads from
    stdin (heredoc or pipe), else None. Unparseable shell text returns None."""
    text, bodies = _split_heredocs(command)
    try:
        commands = _simple_commands(text)
    except ValueError:
        return None
    for index, cmd in enumerate(commands):
        args = cmd["args"]
        if not args or not INTERPRETER.match(os.path.basename(args[0])):
            continue
        if any(not arg.startswith("-") for arg in args[1:]):
            continue  # a script file, or a -c/-e argument that the other check reads
        before = commands[index - 1] if cmd["piped"] and index else None
        if cmd["markers"]:
            content = bodies[cmd["markers"][0]]
        elif before and before["markers"]:
            content = bodies[before["markers"][0]]
        elif before:
            content = "".join(before["args"][1:])
        else:
            continue
        if len(content) > THRESHOLD_CHARS:
            return len(content)
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

    match = INLINE_SINGLE.search(command) or INLINE_DOUBLE.search(command)
    length = None
    if match:
        script_content = match.group(2)
        if len(script_content) > THRESHOLD_CHARS or "\n" in script_content:
            length = len(script_content)
    if length is None:
        length = _long_heredoc_or_pipe(command)
    if length is None:
        sys.exit(0)

    _log_deny(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or ".", "inline_script_guard", "inline-script")
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Inline script is {length} chars. "
                f"Per feedback_no_inline_scripts_ever, anything beyond a trivial "
                f"one-liner belongs in a test file or script."
            ),
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
