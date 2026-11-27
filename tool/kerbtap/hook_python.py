"""The interpreter the installed hooks will run under, checked before installing them.

Both tiers wire each hook as ``python3 <hook>``, resolved on ``PATH`` when Claude Code
runs it. A hook that cannot start under that interpreter raises, and a PreToolUse hook
that raises does not block the tool call — the gate stops gating and nothing says so.
``init`` and ``install-machine`` therefore refuse to install onto a ``python3`` older
than :data:`HOOK_PYTHON_FLOOR`, or onto a machine with none.

What this does not reach: a Claude Code process whose ``PATH`` differs from the shell
that ran the install (an app started from the desktop, for instance) can still resolve
an older ``python3``. ``scripts/check_hooks_start.py`` proves the hooks start on the
floor itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess

# The lowest Python every shipped hook starts and passes its tests on; CI runs the
# hook sweep on it.
HOOK_PYTHON_FLOOR = (3, 9)


def hook_python_problem(path: str | None = None) -> str | None:
    """Why the ``python3`` on ``path`` (default: this process's ``PATH``) cannot run the hooks, or ``None``."""
    search = path if path is not None else os.environ.get("PATH", "")
    exe = shutil.which("python3", path=search)
    floor = ".".join(map(str, HOOK_PYTHON_FLOOR))
    if exe is None:
        return f"no python3 on PATH; the hooks run as python3 <hook> and need Python {floor}+"
    try:
        out = subprocess.run(
            [exe, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        version = tuple(int(part) for part in out.split("."))
    except (subprocess.SubprocessError, OSError, ValueError):
        return f"{exe} did not report its version; the hooks need Python {floor}+"
    if version < HOOK_PYTHON_FLOOR:
        return (
            f"{exe} is Python {out}; the hooks run as python3 <hook> and need Python "
            f"{floor}+. Put a newer python3 first on PATH, then re-run"
        )
    return None
