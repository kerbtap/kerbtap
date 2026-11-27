"""SURF-001: the tool and both hooks read one surface rule.

``GateSurface.guards`` (the Evidence Pack, ``governor review verify``) and the two hooks'
``_on_surface`` (the gates) must agree on every path. The hooks are loaded from their
repository files, never imported by name: they are repository content, not a package.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from kerbtap.collect.gate_surface import ACTIVE, GateSurface
from tests.conftest import require_source

HOOKS = Path(__file__).resolve().parents[2] / ".claude" / "hooks"
SUFFIXES = (".py",)

SURFACES = {
    "whole": ("",),
    "whole+hooks": ("", ".claude/hooks/"),
    "docs": ("docs/",),
}

# path -> expected per surface key.
TABLE = {
    "x.py": {"whole": True, "whole+hooks": True, "docs": False},
    "sub/x.py": {"whole": True, "whole+hooks": True, "docs": False},
    ".github/x.py": {"whole": False, "whole+hooks": False, "docs": False},
    ".claude/hooks/x.py": {"whole": False, "whole+hooks": True, "docs": False},
    "@x.py": {"whole": True, "whole+hooks": True, "docs": False},
    "+x.py": {"whole": True, "whole+hooks": True, "docs": False},
    "~x.py": {"whole": True, "whole+hooks": True, "docs": False},
    "éx.py": {"whole": True, "whole+hooks": True, "docs": False},
    "x.txt": {"whole": False, "whole+hooks": False, "docs": False},
    ".claude/hooks/x.txt": {"whole": False, "whole+hooks": False, "docs": False},
    "docs/x.py": {"whole": True, "whole+hooks": True, "docs": True},
    "docs/x.txt": {"whole": False, "whole+hooks": False, "docs": False},
}


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_parity_{name}", HOOKS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hooks() -> dict[str, ModuleType]:
    require_source(".claude/hooks/precommit_review_guard.py")
    return {name: _load(name) for name in ("precommit_review_guard",)}


@pytest.mark.parametrize("surface_key", sorted(SURFACES))
@pytest.mark.parametrize("path", sorted(TABLE))
def test_predicates_agree(path: str, surface_key: str, hooks: dict[str, ModuleType]) -> None:
    prefixes = SURFACES[surface_key]
    expected = TABLE[path][surface_key]
    surface = GateSurface(ACTIVE, prefixes=prefixes, suffixes=SUFFIXES)
    assert surface.guards(path) is expected
    for name, module in hooks.items():
        module.TRACKED_PATH_PREFIXES = prefixes
        module.TRACKED_SUFFIXES = SUFFIXES
        assert module._on_surface(path) is expected, name
