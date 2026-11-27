#!/usr/bin/env python3
"""Integration test for doctrine_context.py (DOC-001).

Drives the real hook as a subprocess. ``CLAUDE_CONFIG_DIR`` points at a temporary directory in every case, so
the machine's own ``~/.claude/CLAUDE.md`` never decides a result.

Run: ``python3 test_doctrine_context.py`` (exit 0 = all pass).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "doctrine_context.py"
DOCTRINE = "# Test doctrine heading\n\nFirst rule.\n\n---\n\nLast rule.\n"


def run(stdin: str, plugin_root: Path | None, config_dir: Path):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_CONFIG_DIR")}
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin, text=True, capture_output=True, env=env)


def context(proc) -> str | None:
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def setup(tmp: Path, user_claude: str | None) -> tuple[Path, Path]:
    plugin, config = tmp / "plugin", tmp / "config"
    plugin.mkdir()
    config.mkdir()
    (plugin / "CLAUDE.md").write_text(DOCTRINE, encoding="utf-8")
    if user_claude is not None:
        (config / "CLAUDE.md").write_text(user_claude, encoding="utf-8")
    return plugin, config


def test_injects_when_no_user_claude_md(tmp: Path) -> str | None:
    plugin, config = setup(tmp, None)
    p = run("{}", plugin, config)
    return None if p.returncode == 0 and context(p) == DOCTRINE else f"rc={p.returncode} out={p.stdout!r}"


def test_injects_when_user_claude_md_is_other_text(tmp: Path) -> str | None:
    plugin, config = setup(tmp, "# My notes\n\nNot the doctrine. Mentions # Test doctrine heading in a sentence.\n")
    p = run("{}", plugin, config)
    return None if p.returncode == 0 and context(p) == DOCTRINE else f"rc={p.returncode} out={p.stdout!r}"


def test_silent_when_user_claude_md_holds_doctrine(tmp: Path) -> str | None:
    plugin, config = setup(tmp, "intro\n# Test doctrine heading\nrest\n")
    p = run("{}", plugin, config)
    return None if p.returncode == 0 and p.stdout == "" else f"rc={p.returncode} out={p.stdout!r}"


def test_silent_without_plugin_root(tmp: Path) -> str | None:
    _, config = setup(tmp, None)
    p = run("{}", None, config)
    return None if p.returncode == 0 and p.stdout == "" else f"rc={p.returncode} out={p.stdout!r}"


def test_silent_when_doctrine_missing(tmp: Path) -> str | None:
    plugin, config = setup(tmp, None)
    (plugin / "CLAUDE.md").unlink()
    p = run("{}", plugin, config)
    return None if p.returncode == 0 and p.stdout == "" else f"rc={p.returncode} out={p.stdout!r}"


def test_malformed_payload_exits_zero(tmp: Path) -> str | None:
    plugin, config = setup(tmp, None)
    p = run("{not json", plugin, config)
    return None if p.returncode == 0 and context(p) == DOCTRINE else f"rc={p.returncode} out={p.stdout!r}"


TESTS = [
    test_injects_when_no_user_claude_md,
    test_injects_when_user_claude_md_is_other_text,
    test_silent_when_user_claude_md_holds_doctrine,
    test_silent_without_plugin_root,
    test_silent_when_doctrine_missing,
    test_malformed_payload_exits_zero,
]


def main() -> None:
    failures = []
    for test in TESTS:
        with tempfile.TemporaryDirectory() as tmp:
            result = test(Path(tmp))
        if result is None:
            print(f"pass: {test.__name__}")
        else:
            failures.append(f"FAIL: {test.__name__}: {result}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"\n{len(failures)}/{len(TESTS)} case(s) failed", file=sys.stderr)
        sys.exit(1)
    print(f"\nall {len(TESTS)} cases passed")


if __name__ == "__main__":
    main()
