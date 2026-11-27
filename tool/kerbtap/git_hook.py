"""Install, remove and report the git ``pre-commit`` hook that runs the commit gates.

The hook runs ``.claude/git-hooks/pre_commit.py``, then any hook that was there before.
An existing ``pre-commit`` is renamed to ``pre-commit.governor-chained`` and run after the
adapter; it is never overwritten.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger("kerbtap.git_hook")

HOOK_NAME = "pre-commit"
CHAINED_NAME = "pre-commit.governor-chained"
START_MARKER = "# >>> governor pre-commit >>>"
END_MARKER = "# <<< governor pre-commit <<<"
CHAINED_PREFIX = "# governor-chained: "

_TEMPLATE = """#!/bin/sh
{start}
{chained_prefix}{chained}
if [ -f .claude/git-hooks/pre_commit.py ]; then
  python3 .claude/git-hooks/pre_commit.py || exit 1
fi
chained="$(dirname "$0")/{chained_name}"
if [ -x "$chained" ]; then
  exec "$chained" "$@"
fi
exit 0
{end}
"""


class GitHookError(Exception):
    """The hook cannot be installed, removed or read; the message names the file."""


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise GitHookError(f"cannot run git: {exc}") from exc
    if proc.returncode != 0:
        raise GitHookError(f"{repo} is not a git work tree: {proc.stderr.strip()}")
    return proc.stdout.strip()


def hooks_dir(repo: Path) -> Path:
    top = Path(_git(repo, "rev-parse", "--show-toplevel"))
    return (top / _git(top, "rev-parse", "--git-path", "hooks")).resolve()


def _is_governor_hook(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    return len(lines) > 1 and lines[1] == START_MARKER


def _chained_value(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(CHAINED_PREFIX):
            return line[len(CHAINED_PREFIX):].strip()
    return "no"


def install(repo: Path) -> Path:
    hook = hooks_dir(repo) / HOOK_NAME
    chained = hook.with_name(CHAINED_NAME)
    present = os.path.lexists(hook)
    if present and not hook.is_symlink() and not hook.is_file():
        raise GitHookError(f"{hook} is not a regular file; nothing was written.")
    renamed = False
    try:
        if present and not hook.is_symlink() and _is_governor_hook(hook):
            was_chained = _chained_value(hook)
        elif present:
            if os.path.lexists(chained):
                raise GitHookError(
                    f"{chained} already exists next to {hook}; nothing was written. "
                    "Move one of the two files and run again."
                )
            os.rename(hook, chained)
            renamed = True
            was_chained = "yes"
        else:
            was_chained = "no"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(
            _TEMPLATE.format(
                start=START_MARKER,
                end=END_MARKER,
                chained_prefix=CHAINED_PREFIX,
                chained=was_chained,
                chained_name=CHAINED_NAME,
            ),
            encoding="utf-8",
        )
        hook.chmod(0o755)
    except OSError as exc:
        if renamed:
            try:
                os.rename(chained, hook)
            except OSError as undo_exc:
                logger.error("cannot restore %s from %s: %s", hook, chained, undo_exc)
                raise GitHookError(
                    f"cannot write {hook}: {exc}; the old hook is still at {chained}"
                ) from exc
        logger.error("cannot install git hook %s: %s", hook, exc)
        raise GitHookError(f"cannot write {hook}: {exc}") from exc
    logger.info("installed git hook %s (chained: %s)", hook, was_chained)
    return hook


def remove(repo: Path) -> Path:
    hook = hooks_dir(repo) / HOOK_NAME
    if hook.is_symlink() or not hook.is_file() or not _is_governor_hook(hook):
        raise GitHookError(f"{hook} is not a kerbtap hook; nothing was changed.")
    chained = hook.with_name(CHAINED_NAME)
    try:
        if _chained_value(hook) == "yes":
            if not os.path.lexists(chained):
                raise GitHookError(f"{chained} is missing; {hook} was left in place.")
            os.rename(chained, hook)
            logger.info("removed git hook; restored %s (chained: yes)", hook)
        else:
            hook.unlink()
            logger.info("removed git hook %s (chained: no)", hook)
    except OSError as exc:
        logger.error("cannot remove git hook %s: %s", hook, exc)
        raise GitHookError(f"cannot remove {hook}: {exc}") from exc
    return hook


def status(repo: Path) -> tuple[bool, Path]:
    hook = hooks_dir(repo) / HOOK_NAME
    installed = hook.is_file() and _is_governor_hook(hook)
    logger.info("git hook %s: %s", hook, "installed" if installed else "absent")
    return installed, hook
