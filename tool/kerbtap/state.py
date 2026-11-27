"""The state folder rule (SF-001): ``.governor/`` when it exists, ``.kerbtap/`` otherwise.

An install made before the rename keeps its state in ``.governor/``; a new install gets
``.kerbtap/``. Nothing is moved or copied. The hooks carry the same rule as a small function
of their own, because they are standard-library only and cannot import this tool.
"""

from __future__ import annotations

from pathlib import Path

OLD_NAME = ".governor"
NEW_NAME = ".kerbtap"


def state_name(root: Path) -> str:
    """``.governor`` when ``root/.governor`` exists (even when ``.kerbtap`` does too), else ``.kerbtap``."""
    return OLD_NAME if (root / OLD_NAME).exists() or (root / OLD_NAME).is_symlink() else NEW_NAME


def state_rel(root: Path, *parts: str) -> Path:
    """Path of ``parts`` below the state folder, relative to ``root``."""
    return Path(state_name(root), *parts)
