"""Make the `governor` package importable when running pytest from `tool/` without install."""

from __future__ import annotations

import sys
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parent.parent
if str(_TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOL_ROOT))

_REPO_ROOT = Path(__file__).resolve().parents[2]


def require_source(*rel: str) -> None:
    """Skip when a file only the source repository has is absent (an export lacks it)."""
    import pytest

    for path in rel:
        if not (_REPO_ROOT / path).exists():
            pytest.skip(f"source-only: {path} is absent")
