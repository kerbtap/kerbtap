"""EPR-001: the pack describes the review gate as a bypass-prefix block."""

from __future__ import annotations

from pathlib import Path

from kerbtap import assemble
from kerbtap.collect.gate_surface import ACTIVE, GateSurface
from kerbtap.model import Commit
from kerbtap.build import control_map
from kerbtap.collect.governance import detect_source_systems
from tests.factories import make_bundle

_BYPASS = "until the operator types the bypass prefix"
_NO_CHECK = "does not check for a recorded review"


def test_source_system_evidence_names_the_bypass_prefix(tmp_path: Path) -> None:
    hook = tmp_path / ".claude" / "hooks" / "precommit_review_guard.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("# guard\n")
    systems = detect_source_systems(tmp_path, gh_available=False)
    gate = next(s for s in systems if s.name == "Pre-commit review gate")
    assert _BYPASS in gate.evidence
    assert _NO_CHECK in gate.evidence
    assert "without a recorded review" not in gate.evidence


def test_control_map_names_the_bypass_prefix() -> None:
    page = control_map.render(make_bundle())
    assert _NO_CHECK in page
    assert "bypass prefix" in page
    assert "without a recorded review" not in page
    assert "deny unreviewed" not in page


def test_policy_gate_name_names_the_bypass_prefix() -> None:
    commit = Commit("a" * 40, "A", "a@example.com", "2026-01-01T00:00:00+00:00", "s", "", ["b" * 40])
    surface = GateSurface(ACTIVE, prefixes=("docs/",), suffixes=(".md",))
    path = "docs/x.md"
    gate = assemble._review_gate(commit, surface, assemble._guarded_paths(commit, surface, (path,)), False)
    assert gate.applies
    assert "bypass prefix" in gate.gate
    assert _NO_CHECK in gate.gate
    assert "unreviewed" not in gate.gate
