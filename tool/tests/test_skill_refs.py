"""Every `/skill` a shipped skill tells the reader to run exists in that skill's own tier.

Dogfood O53: the starter's sprint-start said "run the `/ticket` ritual", and the starter
shipped no ticket skill, so every installed repository was told to run a skill it did not
have. Each tier is checked on its own — a skill that exists only in another tier does not
satisfy a reference — and an installed tree is checked too, since that is what a user gets.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from kerbtap.init import init
from tests.conftest import require_source

REPO = Path(__file__).resolve().parents[2]
ALLOW = json.loads((Path(__file__).parent / "data" / "skill_ref_allowlist.json").read_text(encoding="utf-8"))
_SPAN_RE = re.compile(r"`([^`\n]+)`")
_REF_RE = re.compile(r"^/([a-z][a-z0-9-]*(?::[a-z0-9-]+)?)(?:\s|$)")


def _tracked_skills(root: Path, tier: str) -> dict[str, Path]:
    """Skill name -> SKILL.md, for files git tracks under ``tier``."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", tier],
        capture_output=True, text=True, check=True,
    ).stdout
    skills = {}
    for rel in filter(None, out.split("\x00")):
        path = Path(rel)
        if path.name == "SKILL.md" and len(path.relative_to(tier).parts) == 2:
            skills[path.parent.name] = root / path
    return skills


def _disk_skills(skills_dir: Path) -> dict[str, Path]:
    return {p.parent.name: p for p in sorted(skills_dir.glob("*/SKILL.md"))}


def dangling(skills: dict[str, Path], allow: dict) -> list[str]:
    """``skill: /name`` for every referenced slash command the tier cannot satisfy."""
    allowed = set(allow["harness"]) | set(allow["plugin"])
    problems = []
    for owner, path in sorted(skills.items()):
        for span in _SPAN_RE.findall(path.read_text(encoding="utf-8")):
            match = _REF_RE.match(span)
            if match and match.group(1) not in skills and match.group(1) not in allowed:
                problems.append(f"{owner}: /{match.group(1)}")
    return sorted(set(problems))


@pytest.mark.parametrize("tier", [".claude/skills", "starter/.claude/skills"])
def test_source_tiers_have_no_dangling_references(tier: str) -> None:
    if tier == ".claude/skills":
        require_source(tier)
    skills = _tracked_skills(REPO, tier)
    assert skills, f"no tracked skills under {tier} — the check would pass over nothing"
    assert dangling(skills, ALLOW) == []


def test_starter_ships_the_ticket_skill_sprint_start_names() -> None:
    skills = _tracked_skills(REPO, "starter/.claude/skills")
    assert "ticket" in skills
    assert "`/ticket`" in skills["sprint-start"].read_text(encoding="utf-8")


def test_installed_tree_has_no_dangling_references(tmp_path: Path) -> None:
    target = tmp_path / "shop"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    (target / "docs").mkdir()
    init(target, REPO, run_tests=False)
    skills = _disk_skills(target / ".claude" / "skills")
    assert {"sprint-start", "ticket"} <= set(skills)
    assert dangling(skills, ALLOW) == []


def test_vendored_ticket_skill_carries_no_governor_specifics() -> None:
    text = (REPO / "starter/.claude/skills/ticket/SKILL.md").read_text(encoding="utf-8")
    for word in (r"GOV-", r"\bCodex\b", r"docs/business-plan", r"PREFER_DOGFOOD", r"\barchitect\b"):
        assert not re.search(word, text), word


# --- the checker refuses what it must --------------------------------------------------


def _tier(root: Path, skills: dict[str, str]) -> dict[str, Path]:
    found = {}
    for name, body in skills.items():
        path = root / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\n---\n\n{body}\n", encoding="utf-8")
        found[name] = path
    return found


def test_dangling_reference_is_reported(tmp_path: Path) -> None:
    skills = _tier(tmp_path, {"start": "Then run the `/ticket` ritual."})
    assert dangling(skills, ALLOW) == ["start: /ticket"]


def test_reference_satisfied_only_by_another_tier_is_reported(tmp_path: Path) -> None:
    starter = _tier(tmp_path / "starter", {"start": "Run `/ticket PROJ-1`."})
    _tier(tmp_path / "dogfood", {"ticket": "The ritual."})
    assert dangling(starter, ALLOW) == ["start: /ticket"]


def test_reference_to_a_skill_in_the_same_tier_passes(tmp_path: Path) -> None:
    skills = _tier(tmp_path, {"start": "Run `/ticket PROJ-1`.", "ticket": "The ritual."})
    assert dangling(skills, ALLOW) == []


def test_allowlisted_harness_command_passes(tmp_path: Path) -> None:
    skills = _tier(tmp_path, {"start": "Run `/compact` when the context is long, then `/help`."})
    assert dangling(skills, ALLOW) == []


def test_plugin_command_passes_only_when_allowlisted(tmp_path: Path) -> None:
    skills = _tier(tmp_path, {"start": "Hand it to `/codex:rescue`."})
    assert dangling(skills, ALLOW) == ["start: /codex:rescue"]
    assert dangling(skills, {"harness": [], "plugin": ["codex:rescue"]}) == []


def test_paths_and_split_spans_are_not_references(tmp_path: Path) -> None:
    # A reference is a whole span starting `/name` followed by a space or its end; a path
    # continues with `/`, and text between two spans is never a span.
    body = "Tracker `cloudId`/workspace `<ID>`; the file `/etc/hosts` is a path, `/Users/x` too."
    skills = _tier(tmp_path, {"start": body})
    assert dangling(skills, ALLOW) == []
