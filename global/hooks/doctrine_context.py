#!/usr/bin/env python3
"""SessionStart hook: deliver the plugin's global doctrine to the model.

A plugin's root ``CLAUDE.md`` is not loaded as context, so this hook emits ``${CLAUDE_PLUGIN_ROOT}/CLAUDE.md`` as
``additionalContext``. It stays silent when the user's own ``CLAUDE.md`` (in ``CLAUDE_CONFIG_DIR``, else
``~/.claude``) already holds the doctrine's first heading line as a line of its own, so the doctrine is not
delivered twice. Any unreadable file or bad payload means silence and exit 0: the hook never blocks a session.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def first_heading(text: str) -> str | None:
    """The first line that is a level-1 heading."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line.rstrip()
    return None


def user_holds(heading: str) -> bool:
    config = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    try:
        user_text = (Path(config) / "CLAUDE.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return heading in (line.rstrip() for line in user_text.splitlines())


def main() -> None:
    try:
        sys.stdin.read()  # the payload is not needed
    except (OSError, UnicodeDecodeError):
        pass
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        sys.exit(0)
    try:
        doctrine = (Path(root) / "CLAUDE.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        sys.exit(0)
    heading = first_heading(doctrine)
    if heading is None or user_holds(heading):
        sys.exit(0)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": doctrine}}))
    sys.exit(0)


if __name__ == "__main__":
    main()
