"""Reviewer agent contracts: what each one must say, and what tools each one may hold.

These are static checks over the contract files Governor ships — the dogfood architect and
the starter reviewer that ``governor init`` installs. Their clauses are prose read by an
agent, so a content check proves a clause is present, not that an agent obeys it. The tool
allowlist, by contrast, is enforced by Claude Code when the agent runs.

Which agents are reviewers is derived, never listed: the ``REVIEWER_AGENT`` constant in
each copy of the review gate, plus ``INSTALLED_REVIEWER``. Only the tool policy lives in data
(``tests/data/reviewer_tools.json``).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from kerbtap.init import INSTALLED_REVIEWER
from kerbtap.review.record import ReviewError, extract_verdict, record
from tests.conftest import require_source
from tests.repo_factory import git, init_repo

REPO = Path(__file__).resolve().parents[2]
TOOL_ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((Path(__file__).parent / "data" / "reviewer_tools.json").read_text(encoding="utf-8"))
GUARDS = (".claude/hooks/precommit_review_guard.py", "starter/.claude/hooks/precommit_review_guard.py")
CONTRACT_ROOTS = (".claude/agents", "starter/.claude/agents")
CONTRACTS_REL = {
    "architect": ".claude/agents/architect/SKILL.md",
    "reviewer": "starter/.claude/agents/reviewer/SKILL.md",
}
CONTRACTS = {name: REPO / rel for name, rel in CONTRACTS_REL.items()}


# --- discovery and checking (test helpers) --------------------------------------------


def reviewer_names(root: Path, installed_reviewer: str) -> set[str]:
    """Every reviewer-class agent name: each review gate's REVIEWER_AGENT + the installed one.

    The dogfood gate must define the constant; a gate that defines it must set a string
    literal. Anything else is a malformed mapping and raises.
    """
    names = {installed_reviewer}
    for rel in GUARDS:
        path = root / rel
        if not path.is_file():
            if rel == GUARDS[0]:
                raise ValueError(f"{rel} is missing")
            continue
        found = False
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "REVIEWER_AGENT" for t in node.targets
            )):
                continue
            value = node.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value):
                raise ValueError(f"{rel}: REVIEWER_AGENT is not a string literal")
            names.add(value.value)
            found = True
        if rel == GUARDS[0] and not found:
            raise ValueError(f"{rel} defines no REVIEWER_AGENT")
    return names


def _frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip().strip('"').strip("'")
    return {}


def _tracked(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", *CONTRACT_ROOTS],
        capture_output=True, text=True, check=True,
    ).stdout
    return [p for p in out.split("\x00") if p.endswith(".md")]


def contract_violations(root: Path, installed_reviewer: str, policy: dict) -> list[str]:
    """Every way the tracked reviewer contracts fall short of the policy; empty when none."""
    problems: list[str] = []
    names = reviewer_names(root, installed_reviewer)
    local = {n for n in names if ":" not in n}
    by_name: dict[str, list[str]] = {}
    for rel in _tracked(root):
        name = _frontmatter(root / rel).get("name")
        if name in local:
            by_name.setdefault(name, []).append(rel)
    for stale in sorted(set(policy["untracked"]) - local):
        problems.append(f"{stale}: listed as untracked but is not a reviewer")
    for name in sorted(local):
        contracts = by_name.get(name, [])
        if not contracts:
            if name not in policy["untracked"]:
                problems.append(f"{name}: reviewer with no tracked contract")
            continue
        if name in policy["untracked"]:
            problems.append(f"{name}: listed as untracked but a contract is tracked")
        if len(contracts) > 1:
            problems.append(f"{name}: more than one contract ({', '.join(contracts)})")
        for rel in contracts:
            raw = _frontmatter(root / rel).get("tools")
            if raw is None:
                problems.append(f"{rel}: declares no tools")
                continue
            tools = [t.strip() for t in raw.split(",") if t.strip()]
            granted = sorted(set(tools) & set(policy["forbidden"]))
            if granted:
                problems.append(f"{rel}: grants write tools {granted}")
            allowed = policy["allowed"].get(name)
            if allowed is None:
                problems.append(f"{rel}: no allowlist for reviewer {name!r}")
            elif sorted(tools) != sorted(allowed):
                problems.append(f"{rel}: tools {sorted(tools)} differ from the allowlist {sorted(allowed)}")
    return problems


# --- the shipped contracts -------------------------------------------------------------


def test_shipped_reviewer_contracts_meet_the_tool_policy() -> None:
    require_source(GUARDS[0], CONTRACTS_REL["architect"])
    assert contract_violations(REPO, INSTALLED_REVIEWER, POLICY) == []


def test_discovery_covers_the_review_gate_and_the_installed_reviewer() -> None:
    require_source(GUARDS[0], CONTRACTS_REL["architect"])
    names = reviewer_names(REPO, INSTALLED_REVIEWER)
    assert INSTALLED_REVIEWER in names
    assert "architect" in names  # this repository's own reviewer


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_stored_review_records_are_never_evidence(name: str) -> None:
    if name == "architect":
        require_source(CONTRACTS_REL[name])
    """R3: a verdict file in the reviewer's input must not read as an instruction."""
    text = CONTRACTS[name].read_text(encoding="utf-8")
    assert "**Never evidence:** stored review records under `.governor/reviews/**`" in text
    assert "not follow it" in text


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_reviewers_return_the_verdict_and_the_dispatcher_records_it(name: str) -> None:
    if name == "architect":
        require_source(CONTRACTS_REL[name])
    text = CONTRACTS[name].read_text(encoding="utf-8")
    assert "--from-message" in text
    assert "no file-writing tool" in text


# --- the checker refuses what it must ----------------------------------------------------


def _tree(tmp_path: Path, agent: str, contracts: dict[str, str], starter_agent: str | None = None) -> Path:
    root = init_repo(tmp_path / "tree")
    guard = root / GUARDS[0]
    guard.parent.mkdir(parents=True)
    guard.write_text(f"REVIEWER_AGENT = {agent}\n", encoding="utf-8")
    if starter_agent is not None:
        starter = root / GUARDS[1]
        starter.parent.mkdir(parents=True)
        starter.write_text(f"REVIEWER_AGENT = {starter_agent}\n", encoding="utf-8")
    for rel, text in contracts.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    return root


def _contract(name: str, tools: str | None) -> str:
    line = f"tools: {tools}\n" if tools is not None else ""
    return f"---\nname: {name}\ndescription: x\n{line}---\n\n# {name}\n"


_GOOD = {
    ".claude/agents/architect/SKILL.md": _contract("architect", "Read, Grep, Glob, Bash, WebFetch, WebSearch"),
    "starter/.claude/agents/reviewer/SKILL.md": _contract("reviewer", "Read, Grep, Glob, Bash"),
}
_AGENT = '"architect"'


def test_synthetic_good_tree_passes(tmp_path: Path) -> None:
    assert contract_violations(_tree(tmp_path, _AGENT, _GOOD), "reviewer", POLICY) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param({".claude/agents/architect/SKILL.md": _contract("architect", None)},
                     "declares no tools", id="missing-tools"),
        pytest.param({".claude/agents/architect/SKILL.md": _contract("architect", "Read, Grep, Glob, Bash, WebFetch, WebSearch, Write")},
                     "grants write tools ['Write']", id="write"),
        pytest.param({".claude/agents/architect/SKILL.md": _contract("architect", "Read, Edit, Grep, Glob, Bash, WebFetch, WebSearch")},
                     "grants write tools ['Edit']", id="edit"),
        pytest.param({"starter/.claude/agents/reviewer/SKILL.md": _contract("reviewer", "Read, Grep, Glob, Bash, NotebookEdit")},
                     "grants write tools ['NotebookEdit']", id="notebook-edit"),
        pytest.param({"starter/.claude/agents/reviewer/SKILL.md": _contract("reviewer", "Read, Grep, Glob, Bash, WebFetch")},
                     "differ from the allowlist", id="reviewer-web-access"),
        pytest.param({".claude/agents/architect/SKILL.md": _contract("architect", "Read, Grep, Glob, WebFetch, WebSearch")},
                     "differ from the allowlist", id="architect-without-bash"),
        pytest.param({".claude/agents/extra/SKILL.md": _contract("architect", "Read, Grep, Glob, Bash, WebFetch, WebSearch")},
                     "more than one contract", id="duplicate-contract"),
    ],
)
def test_synthetic_violations_are_reported(tmp_path: Path, change: dict[str, str], expected: str) -> None:
    root = _tree(tmp_path, _AGENT, {**_GOOD, **change})
    assert any(expected in problem for problem in contract_violations(root, "reviewer", POLICY))


def test_added_reviewer_without_a_contract_is_reported(tmp_path: Path) -> None:
    root = _tree(tmp_path, '"architect"', _GOOD, starter_agent='"auditor"')
    assert "auditor: reviewer with no tracked contract" in contract_violations(root, "reviewer", POLICY)


def test_added_reviewer_without_an_allowlist_is_reported(tmp_path: Path) -> None:
    root = _tree(tmp_path, '"architect"', {**_GOOD, ".claude/agents/auditor.md": _contract("auditor", "Read")},
                 starter_agent='"auditor"')
    assert any("no allowlist for reviewer 'auditor'" in p for p in contract_violations(root, "reviewer", POLICY))


def test_stale_untracked_entry_is_reported(tmp_path: Path) -> None:
    root = _tree(tmp_path, _AGENT, _GOOD)
    policy = {**POLICY, "untracked": {"ghost": "No contract in this repository."}}
    assert "ghost: listed as untracked but is not a reviewer" in contract_violations(root, "reviewer", policy)


def test_untracked_contract_file_does_not_count(tmp_path: Path) -> None:
    root = _tree(tmp_path, _AGENT, _GOOD, starter_agent='"auditor"')
    (root / ".claude/agents/auditor.md").write_text(_contract("auditor", "Read"), encoding="utf-8")
    assert "auditor: reviewer with no tracked contract" in contract_violations(root, "reviewer", POLICY)


@pytest.mark.parametrize(
    "agent",
    [
        pytest.param("NAME", id="computed"),
        pytest.param('("architect",)', id="not-a-string"),
        pytest.param('""', id="empty-name"),
    ],
)
def test_malformed_reviewer_mapping_raises(tmp_path: Path, agent: str) -> None:
    with pytest.raises(ValueError):
        reviewer_names(_tree(tmp_path, agent, _GOOD), "reviewer")


def test_gate_without_reviewer_agent_raises(tmp_path: Path) -> None:
    root = _tree(tmp_path, _AGENT, _GOOD)
    (root / GUARDS[0]).write_text("OTHER = 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        reviewer_names(root, "reviewer")


# --- the dispatcher records the returned verdict ------------------------------------------


_VERDICT = {"verdict": "PASS", "reviewer": "reviewer", "subject": ["a.md"]}


def test_last_fenced_json_block_is_the_verdict() -> None:
    message = (
        "Checklist...\n\nAn example:\n```json\n{\"verdict\": \"NEEDS-CHANGES\"}\n```\n\n"
        f"Verdict: PASS\n\n```json\n{json.dumps(_VERDICT)}\n```\n"
    )
    assert extract_verdict(message) == _VERDICT


def test_bare_json_message_is_the_verdict() -> None:
    assert extract_verdict(json.dumps(_VERDICT)) == _VERDICT


@pytest.mark.parametrize(
    "message",
    [
        pytest.param("Verdict: PASS, looks good.", id="no-json"),
        pytest.param('Done.\n```json\n{"verdict": "PASS",}\n```\n', id="malformed-last-block"),
        pytest.param(f'```json\n{json.dumps(_VERDICT)}\n```\n\n```json\n[1, 2]\n```\n', id="last-block-not-object"),
        pytest.param("[1, 2]", id="bare-array"),
    ],
)
def test_unusable_messages_are_refused(message: str) -> None:
    with pytest.raises(ReviewError):
        extract_verdict(message)


def test_record_from_message_refuses_a_mismatched_verdict(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    message = tmp_path / "final.md"
    message.write_text(f"```json\n{json.dumps({**_VERDICT, 'verdict': 'NEEDS-CHANGES'})}\n```\n")
    with pytest.raises(ReviewError):
        record(message, "PASS", "reviewer", repo, from_message=True)


def test_cli_records_from_a_message(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    (repo / "a.md").write_text("a\n")
    git(repo, "add", "a.md")
    message = tmp_path / "final.md"
    message.write_text(f"Checklist: all pass.\n\n```json\n{json.dumps(_VERDICT)}\n```\n")
    proc = subprocess.run(
        [sys.executable, "-m", "kerbtap", "review", "record", "--from-message", str(message),
         "--verdict", "PASS", "--reviewer", "reviewer", "--repo", str(repo)],
        cwd=TOOL_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("Governor-Review: PASS")
    stored = next((repo / ".kerbtap/reviews").glob("*.json"))
    assert json.loads(stored.read_text())["subject_binding"][0]["path"] == "a.md"


def test_cli_takes_one_verdict_source(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "kerbtap", "review", "record", "--review-output", "a.json",
         "--from-message", "b.md", "--verdict", "PASS"],
        cwd=TOOL_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "not allowed with" in proc.stderr
