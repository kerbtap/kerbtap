"""Tests for ``kerbtap init``.

The install is only worth shipping if it leaves a repo with gates that actually fire,
so these tests assert on the installed artefact — no TODO markers, renamed bypasses,
surfaces pointed at paths the repo has — and then drive the installed hook to confirm
it denies. A test that only checks files were copied would pass on a broken install.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kerbtap.init import InitError, derive_prefix, detect_surfaces, hook_command, init
from tests.conftest import require_source

SOURCE_REPO = Path(__file__).resolve().parents[2]


GATE_FILES = (
    "commit_scope.py",
    "precommit_review_guard.py",
    "precommit_doc_consistency_guard.py",
    "test_commit_scope.py",
    "test_precommit_review_guard.py",
    "test_precommit_doc_consistency_guard.py",
)

BYPASS_RENAMES = (
    ("GOVERNOR_REVIEWED", "PROJECT_REVIEWED"),
    ("GOVERNOR_DOC_REVIEWED", "PROJECT_DOC_REVIEWED"),
    ("GOVERNOR_CLAIM_VERIFIED", "PROJECT_CLAIM_VERIFIED"),
    ("docs/business-plan/", "docs/deliverables/"),
    ("docs/product-vision-v0.2.md", "docs/vision.md"),
)


def _drifted(dogfood: Path, starter: Path) -> list[str]:
    """The files of ``GATE_FILES`` whose ``.claude/hooks/`` copy differs from the starter copy after the renames."""
    drifted = []
    for name in GATE_FILES:
        text = (dogfood / name).read_text(encoding="utf-8")
        for old, new in BYPASS_RENAMES:
            text = text.replace(old, new)
        if text != (starter / name).read_text(encoding="utf-8"):
            drifted.append(name)
    return drifted


def test_starter_hooks_do_not_drift() -> None:
    """Each of the six gate files in ``.claude/hooks/`` equals its starter copy after the three renames (GS-002)."""
    dogfood = SOURCE_REPO / ".claude" / "hooks"
    if not dogfood.is_dir():
        pytest.skip("no .claude/hooks/ in this checkout")
    drifted = _drifted(dogfood, SOURCE_REPO / "starter" / ".claude" / "hooks")
    assert not drifted, f".claude/hooks/ and starter/.claude/hooks/ differ: {drifted}"


def test_drift_check_names_the_drifted_file(tmp_path: Path) -> None:
    """A one-line change to one copy fails the check, and the check names that file."""
    dogfood = tmp_path / "dogfood"
    starter = tmp_path / "starter"
    for name in GATE_FILES:
        text = (SOURCE_REPO / "starter" / ".claude" / "hooks" / name).read_text(encoding="utf-8")
        for root in (dogfood, starter):
            root.mkdir(exist_ok=True)
            (root / name).write_text(text, encoding="utf-8")
    assert _drifted(dogfood, starter) == []
    changed = dogfood / "commit_scope.py"
    changed.write_text(changed.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
    assert _drifted(dogfood, starter) == ["commit_scope.py"]


def test_every_dogfood_hook_has_a_starter_counterpart() -> None:
    """A gate file this repo ships and ``starter/`` does not is never installed.

    ``init`` builds its work list by walking ``starter/.claude/hooks/*.py``, so a file
    only ``.claude/hooks/`` ships never reaches an install.
    """
    dogfood = SOURCE_REPO / ".claude" / "hooks"
    starter = SOURCE_REPO / "starter" / ".claude" / "hooks"
    if not dogfood.is_dir():
        pytest.skip("no .claude/hooks/ in this checkout")

    orphans = {
        path.name
        for path in sorted(dogfood.glob("*.py"))
        if not path.name.startswith("test_") and not (starter / path.name).is_file()
    }

    assert not orphans, f"shipped in .claude/hooks/ but absent from starter/.claude/hooks/: {sorted(orphans)}"


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: Path, name: str = "carbonLedger") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "T"], repo)
    return repo


def test_prefix_derived_from_directory_name(tmp_path: Path) -> None:
    assert derive_prefix(tmp_path / "carbonLedger") == "CARBONLEDGER"
    assert derive_prefix(tmp_path / "my-cool.repo") == "MY_COOL_REPO"
    assert derive_prefix(tmp_path / "2fast") == "P_2FAST"


def test_surfaces_detected_from_repo_contents(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    assert detect_surfaces(repo) == pytest.approx(detect_surfaces(repo))  # dataclass eq
    assert detect_surfaces(repo).docs is True
    assert detect_surfaces(repo).code is False

    (repo / "src").mkdir()
    assert detect_surfaces(repo).code is True


def test_empty_repo_defaults_to_docs_surface(tmp_path: Path) -> None:
    surfaces = detect_surfaces(_repo(tmp_path))
    assert surfaces.docs is True
    assert surfaces.code is False


def test_install_leaves_no_placeholders(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)

    leftovers = []
    for path in (repo / ".claude").rglob("*"):
        if not path.is_file() or path.suffix == ".pyc":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "<PROJECT>" in text or "TODO: adapt for" in text:
            leftovers.append(str(path.relative_to(repo)))
    assert leftovers == [], f"placeholders survived the install: {leftovers}"


def test_bypass_env_vars_are_renamed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    result = init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert result.prefix == "CARBONLEDGER"

    guard = (repo / ".claude/hooks/precommit_review_guard.py").read_text(encoding="utf-8")
    assert 'BYPASS_ENV = "CARBONLEDGER_REVIEWED"' in guard
    assert "PROJECT_REVIEWED" not in guard
    assert "GOVERNOR_REVIEWED" not in guard


def test_review_guard_is_deny_mode_not_ask(tmp_path: Path) -> None:
    """starter/ ships the "ask" variant, which is auto-approved under
    skipAutoPermissionPrompt. The install must prefer the hardened copy.

    The decision is emitted by ``commit_scope.deny``, so the guard file alone no
    longer carries the literal. Both halves are read together: the engine must be
    installed at all, and neither file may reach an install still spelling "ask".
    """
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    hooks = repo / ".claude/hooks"

    engine = hooks / "commit_scope.py"
    assert engine.is_file(), "the gates import commit_scope — an install without it dies on first commit"

    guard_text = (hooks / "precommit_review_guard.py").read_text(encoding="utf-8")
    engine_text = engine.read_text(encoding="utf-8")
    assert '"permissionDecision": "deny"' in engine_text
    assert '"permissionDecision": "ask"' not in guard_text + engine_text


def test_surfaces_are_pointed_at_paths_the_repo_has(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    guard = (repo / ".claude/hooks/precommit_review_guard.py").read_text(encoding="utf-8")
    assert 'TRACKED_PATH_PREFIXES = ("docs/")' in guard or 'TRACKED_PATH_PREFIXES = ("docs/",)' in guard
    assert "src/" not in guard.split("TRACKED_PATH_PREFIXES")[1].split("\n")[0]


def test_gate_without_a_surface_is_dropped_not_left_at_defaults(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "src").mkdir()
    result = init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert not (repo / ".claude/hooks/fact_claim_guard.py").exists()
    assert "fact_claim_guard.py" in result.dropped

    settings = json.loads((repo / ".claude/settings.json").read_text(encoding="utf-8"))
    wired = json.dumps(settings["hooks"])
    assert "fact_claim_guard" not in wired


def test_code_install_has_no_dispatch_gate(tmp_path: Path) -> None:
    """RH-002: a code repository gets neither the retired dispatch gate nor a settings entry for it."""
    repo = _repo(tmp_path)
    (repo / "src").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert not (repo / ".claude/hooks/review_dispatch_guard.py").exists()
    settings = (repo / ".claude/settings.json").read_text(encoding="utf-8")
    assert "review_dispatch_guard.py" not in settings
    assert "Task|Agent" not in settings


def _wire_retired_dispatch_gate(repo: Path, extra_task_hook: dict | None = None) -> None:
    """Rewrite the settings as an earlier `kerbtap init` wrote them, with the dispatch gate wired."""
    path = repo / ".claude/settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = {"type": "command", "command": hook_command("review_dispatch_guard.py"), "timeout": 10}
    pre = data["hooks"]["PreToolUse"]
    next(b for b in pre if b["matcher"] == "Bash")["hooks"].append(dict(entry))
    task_hooks = [dict(entry)] + ([extra_task_hook] if extra_task_hook else [])
    pre.append({"matcher": "Task|Agent", "hooks": task_hooks})
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_reinit_strips_retired_dispatch_wiring(tmp_path: Path) -> None:
    """RH-001: a re-run of init unwires the retired dispatch gate and keeps the other gates."""
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    _wire_retired_dispatch_gate(repo)
    assert "review_dispatch_guard.py" in (repo / ".claude/settings.json").read_text(encoding="utf-8")

    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, force=True)
    text = (repo / ".claude/settings.json").read_text(encoding="utf-8")
    assert "review_dispatch_guard.py" not in text
    assert "Task|Agent" not in text
    assert "precommit_review_guard.py" in text
    assert "precommit_doc_consistency_guard.py" in text


def test_reinit_keeps_user_hook_beside_retired_one(tmp_path: Path) -> None:
    """RH-001: a hook kerbtap does not write survives, in the group that held the retired entry."""
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    mine = {"type": "command", "command": "python3 scripts/my_agent_check.py", "timeout": 5}
    _wire_retired_dispatch_gate(repo, extra_task_hook=mine)

    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, force=True)
    data = json.loads((repo / ".claude/settings.json").read_text(encoding="utf-8"))
    assert "review_dispatch_guard.py" not in json.dumps(data)
    group = next(b for b in data["hooks"]["PreToolUse"] if b["matcher"] == "Task|Agent")
    assert group["hooks"] == [mine]


def test_reinit_keeps_custom_command_for_retired_hook(tmp_path: Path) -> None:
    """RH-001: a different command that names the retired file stays; only the generated entry goes."""
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    mine = {"type": "command", "command": "python3 .claude/hooks/review_dispatch_guard.py --mine", "timeout": 5}
    _wire_retired_dispatch_gate(repo, extra_task_hook=mine)

    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, force=True)
    data = json.loads((repo / ".claude/settings.json").read_text(encoding="utf-8"))
    commands = [h["command"] for b in data["hooks"]["PreToolUse"] for h in b["hooks"]]
    assert hook_command("review_dispatch_guard.py") not in commands
    assert mine["command"] in commands


def test_settings_json_is_valid_and_only_wires_installed_gates(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    settings = json.loads((repo / ".claude/settings.json").read_text(encoding="utf-8"))
    for event in settings["hooks"].values():
        for block in event:
            for hook in block["hooks"]:
                name = hook["command"].split("hooks/")[1].rstrip('\\"')
                assert (repo / ".claude/hooks" / name).is_file(), f"wired but not installed: {name}"


def test_sources_ledger_created_for_docs_repo(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    ledger = repo / "docs/sources-ledger.md"
    assert ledger.is_file()
    assert "| figure | claim |" in ledger.read_text(encoding="utf-8")


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_rerun_without_force_merges_and_rewrites_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    before = _snapshot(repo / ".claude")
    result = init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert result.overwritten == []
    assert "hooks/precommit_review_guard.py" in result.unchanged
    assert result.installed == []
    assert "settings.json" in result.unchanged
    assert _snapshot(repo / ".claude") == before


def test_existing_claude_dir_is_merged_without_force(tmp_path: Path) -> None:
    """A `.claude/` kerbtap never wrote is joined, not refused, and its own files survive."""
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    own = repo / ".claude/hooks/my_team_guard.py"
    own.parent.mkdir(parents=True)
    own.write_text("# the team's own guard\n", encoding="utf-8")
    note = repo / ".claude/rules/house_style.md"
    note.parent.mkdir(parents=True)
    note.write_text("house style\n", encoding="utf-8")

    result = init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert own.read_text(encoding="utf-8") == "# the team's own guard\n"
    assert note.read_text(encoding="utf-8") == "house style\n"
    assert "hooks/precommit_review_guard.py" in result.installed
    assert (repo / ".claude/hooks/precommit_review_guard.py").is_file()
    assert result.overwritten == []


def test_a_differing_scaffold_file_stops_the_install_with_nothing_written(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    gate = repo / ".claude/hooks/precommit_review_guard.py"
    gate.write_text("# edited by the user\n", encoding="utf-8")
    (repo / ".claude/hooks/fact_claim_guard.py").unlink()
    before = _snapshot(repo / ".claude")

    with pytest.raises(InitError, match=r"differ from kerbtap.s copies") as exc:
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert "hooks/precommit_review_guard.py" in str(exc.value)
    assert "hooks/fact_claim_guard.py" not in str(exc.value)  # absent is not differing
    assert _snapshot(repo / ".claude") == before  # not even the missing gate was written


def test_force_overwrites_exactly_the_differing_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    gate = repo / ".claude/hooks/precommit_review_guard.py"
    gate.write_text("# edited by the user\n", encoding="utf-8")

    result = init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, force=True)
    assert result.overwritten == ["hooks/precommit_review_guard.py"]
    assert gate.read_text(encoding="utf-8") != "# edited by the user\n"


def test_bad_source_repo_is_a_clear_error(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(InitError, match="scaffold not found"):
        init(repo=repo, source_repo=tmp_path / "not-governor", run_tests=False)


def test_installed_review_guard_actually_denies(tmp_path: Path) -> None:
    """The point of the whole thing: after init, the gate fires on a real commit."""
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)

    deliverable = repo / "docs/market.md"
    deliverable.write_text("draft\n", encoding="utf-8")
    _git(["add", "docs/market.md"], repo)

    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "git commit -m wip"}, "cwd": str(repo)}
    )
    proc = subprocess.run(
        [sys.executable, str(repo / ".claude/hooks/precommit_review_guard.py")],
        input=payload,
        text=True,
        capture_output=True,
        env={"CLAUDE_PROJECT_DIR": str(repo), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert '"permissionDecision": "deny"' in proc.stdout
    assert "CARBONLEDGER_REVIEWED" in proc.stdout


def test_installed_hook_tests_pass(tmp_path: Path) -> None:
    """init runs the gate tests itself; this asserts that run is green on a fresh install."""
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    result = init(repo=repo, source_repo=SOURCE_REPO, run_tests=True)
    assert result.tests_run > 0
    assert result.tests_failed == []


def test_installed_hook_tests_pass_on_a_code_repo(tmp_path: Path) -> None:
    """A code-only repo has no .md on the reviewed surface, so the ported suite's
    fixtures have to move with it. This caught a real break."""
    repo = _repo(tmp_path, name="codeApp")
    (repo / "src").mkdir()
    result = init(repo=repo, source_repo=SOURCE_REPO, run_tests=True, review_code=True)
    assert result.tests_run > 0
    assert result.tests_failed == []


def test_installed_hook_tests_pass_on_a_mixed_repo(tmp_path: Path) -> None:
    repo = _repo(tmp_path, name="mixedApp")
    (repo / "docs").mkdir()
    (repo / "src").mkdir()
    result = init(repo=repo, source_repo=SOURCE_REPO, run_tests=True)
    assert result.tests_run > 0
    assert result.tests_failed == []


def test_installed_ste_linter_is_unchanged_and_fires(tmp_path: Path) -> None:
    """The copy loop rewrites text, and the vendored STE skill must install unchanged.

    Byte-equality against ``starter/`` proves the rename and TODO passes left it alone;
    it does not prove ``starter/`` still matches upstream. The selftest and a hard
    violation on stdin prove the installed linter runs and fails the run.
    """
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    source = SOURCE_REPO / "starter" / ".claude" / "skills" / "asd-ste100"
    installed = repo / ".claude" / "skills" / "asd-ste100"
    files = sorted(p.relative_to(source) for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    assert files, "no vendored STE files — the check would pass over nothing"
    for rel in files:
        assert (installed / rel).read_bytes() == (source / rel).read_bytes(), rel

    linter = installed / "scripts" / "ste-lint.py"
    selftest = subprocess.run([sys.executable, str(linter), "--selftest"], capture_output=True, text=True)
    assert selftest.returncode == 0, selftest.stdout + selftest.stderr
    hard = subprocess.run([sys.executable, str(linter)], input="Open the file; read it.", capture_output=True, text=True)
    assert hard.returncode == 1, hard.stdout
    assert "semicolon" in hard.stdout


# --- What the install actually says and installs -----------------------------------
#
# The cases below were each written against an observed defect in a real `kerbtap init`
# result. They assert on the installed tree because that is the only place these were
# visible: `starter/` was correct on the vocabulary leak, and `_strip_todos` was correct
# on the banner truncation.


def _code_repo(tmp_path: Path, *, commit: bool = True, name: str = "carbonLedger") -> Path:
    """A repo with a code surface. `commit=False` leaves it unborn (no HEAD)."""
    repo = _repo(tmp_path, name)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    if commit:
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "initial"], repo)
    return repo


def _hook_reason(repo: Path, hook: str, payload: dict) -> str:
    """Drive an installed hook with a real PreToolUse payload; return its reason text."""
    proc = subprocess.run(
        [sys.executable, str(repo / ".claude" / "hooks" / hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"CLAUDE_PROJECT_DIR": str(repo), "PATH": os.environ.get("PATH", "")},
    )
    if not proc.stdout.strip():
        return ""
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


VENDOR_VOCABULARY = ("business-plan", "architect", "kerbtap's", "docs/business-plan")


def test_denial_text_carries_no_vendor_vocabulary(tmp_path: Path) -> None:
    """A user's own file must not be described in this project's domain terms.

    The observed defect: `Commit touches 1 business-plan deliverable(s): src/calc.py`,
    which is the first sentence the product says to a new user.
    """
    repo = _code_repo(tmp_path)
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, review_code=True)
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    _git(["add", "-A"], repo)

    reason = _hook_reason(
        repo,
        "precommit_review_guard.py",
        {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": "git commit -m wip"}},
    )
    assert reason, "the review guard did not fire on a staged code file"
    leaked = [word for word in VENDOR_VOCABULARY if word in reason]
    assert leaked == [], f"vendor vocabulary reached the user's denial text: {leaked}"
    assert "guarded file" in reason
    assert "reviewer agent" in reason


def test_hook_table_matches_the_gates_actually_installed(tmp_path: Path) -> None:
    """The table is generated, so it cannot document a gate that was dropped.

    Observed: a code-only install documented `fact_claim_guard.py`, which `init` had
    dropped; and said
    "Soft-blocks" for a gate installed in deny mode.
    """
    repo = _code_repo(tmp_path)
    result = init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, review_code=True)
    table = (repo / "CLAUDE.md").read_text(encoding="utf-8")

    assert "fact_claim_guard.py" in result.dropped
    assert "fact_claim_guard.py" not in table
    assert "Soft-blocks" not in table
    assert "Task|Agent" not in table
    assert "review_dispatch_guard.py" not in table
    for gate in ("precommit_review_guard.py", "precommit_doc_consistency_guard.py"):
        assert f"`{gate}`" in table, f"{gate} installed but missing from the table"
    assert "rename env var" not in table


def test_fact_claim_guard_wired_for_multiedit(tmp_path: Path) -> None:
    """FACT-003: the guard is wired, documented and shipped with the MultiEdit matcher."""
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)

    def matchers(settings_path: Path) -> list[str]:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        return [
            block["matcher"]
            for block in settings["hooks"]["PreToolUse"]
            if any("fact_claim_guard.py" in h["command"] for h in block["hooks"])
        ]

    assert matchers(repo / ".claude/settings.json") == ["Edit|Write|MultiEdit"]
    assert matchers(SOURCE_REPO / "starter/.claude/settings.json") == ["Edit|Write|MultiEdit"]
    table = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "PreToolUse / `Edit|Write|MultiEdit`" in table
    row = next(ln for ln in table.splitlines() if ln.startswith("| `fact_claim_guard.py`"))
    assert "`Edit|Write|MultiEdit`" in row


def test_mcp_config_is_opt_in(tmp_path: Path) -> None:
    """A third-party executable dependency is not installed by default."""
    plain = _code_repo(tmp_path, name="plainRepo")
    init(repo=plain, source_repo=SOURCE_REPO, run_tests=False)
    assert not (plain / ".mcp.json").exists()

    opted = _code_repo(tmp_path, name="optedRepo")
    init(repo=opted, source_repo=SOURCE_REPO, run_tests=False, with_code_review_graph=True)
    assert (opted / ".mcp.json").exists()
    assert "code-review-graph" in (opted / ".mcp.json").read_text(encoding="utf-8")


def test_no_template_banners_survive_and_adaptable_assets_say_so(tmp_path: Path) -> None:
    """Banners: gone, except an adapt-or-delete notice where the asset needs adapting."""
    repo = _code_repo(tmp_path)
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)

    banners = [
        str(path.relative_to(repo))
        for path in (repo / ".claude").rglob("*")
        if path.is_file() and path.suffix != ".pyc"
        and "STARTER TEMPLATE" in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert banners == [], f"template banners reached the installed tree: {banners}"

    for skill in ("sprint-start", "verify-claims", "wrap-up"):
        text = (repo / ".claude" / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        assert "Adapt it to this repository or delete it" in text, skill


def test_installed_reviewer_contract_replaces_the_architect(tmp_path: Path) -> None:
    """The gates name an agent the repo actually has."""
    repo = _code_repo(tmp_path)
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, review_code=True)

    assert (repo / ".claude" / "agents" / "reviewer" / "SKILL.md").is_file()
    assert not (repo / ".claude" / "agents" / "architect").exists()
    guard = (repo / ".claude" / "hooks" / "precommit_review_guard.py").read_text(encoding="utf-8")
    assert '"reviewer"' in guard
    assert '"architect"' not in guard


def test_unborn_repository_is_bootstrap_not_deadlock(tmp_path: Path) -> None:
    """With no commit: the review gate still guards, because `git diff --cached` lists staged paths."""
    repo = _code_repo(tmp_path, commit=False)
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, review_code=True)
    _git(["add", "-A"], repo)

    commit = _hook_reason(
        repo,
        "precommit_review_guard.py",
        {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": "git commit -m first"}},
    )
    assert "guarded file" in commit, "the review guard stopped guarding the first commit"


# --- OpenSpec folder (INS-001 to INS-004) ---------------------------------------


def test_init_installs_openspec(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert (repo / "openspec/README.md").is_file()
    assert (repo / "openspec/specs/review/spec.md").is_file()


def test_init_openspec_differing_file_refuses(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    readme = repo / "openspec/README.md"
    readme.write_text("# mine\n", encoding="utf-8")
    (repo / "openspec/specs/review/spec.md").unlink()
    (repo / ".claude/hooks/fact_claim_guard.py").unlink()
    before = _snapshot(repo)

    with pytest.raises(InitError, match=r"differ from kerbtap.s copies") as exc:
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert "openspec/README.md" in str(exc.value)
    assert "openspec/specs/review/spec.md" not in str(exc.value)  # absent is not differing
    assert _snapshot(repo) == before  # nothing written, not even the missing files

    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, force=True)
    assert readme.read_bytes() == (SOURCE_REPO / "starter/openspec/README.md").read_bytes()


def _node_at_least(major: int, minor: int) -> bool:
    out = subprocess.run(["node", "--version"], capture_output=True, text=True, check=False).stdout
    try:
        got = tuple(int(x) for x in out.strip().lstrip("v").split(".")[:2])
    except ValueError:
        return False
    return got >= (major, minor)


def test_installed_repo_builds_review_brief(tmp_path: Path) -> None:
    import re
    import shutil

    if shutil.which("npx") is None or shutil.which("node") is None:
        pytest.skip("npx or node is absent; openspec validate cannot run")
    if not _node_at_least(22, 12):
        pytest.skip("Node older than 22.12; openspec validate cannot run")

    repo = _repo(tmp_path)
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    readme = (repo / "openspec/README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"`(openspec/changes/demo/[^`]+)`\n\n```markdown\n(.*?)```", readme, re.S)
    assert len(blocks) == 3, f"expected three example files, found {[b[0] for b in blocks]}"
    for rel, body in blocks:
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")

    out = tmp_path / "brief.md"
    proc = subprocess.run(
        [sys.executable, str(SOURCE_REPO / "global/review/review_brief.py"), "brief", "demo", "plan", "-o", str(out)],
        cwd=repo, capture_output=True, text=True, check=False, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "> Request: print a greeting when the program starts." in out.read_text(encoding="utf-8")


def test_planner_writes_openspec_change() -> None:
    text = (SOURCE_REPO / "global/agents/planner.md").read_text(encoding="utf-8")
    assert "openspec/changes/<ID>/" in text
    assert "`proposal.md` starts with `## Source`" in text
    assert "validate <ID> --strict" in text


def test_starter_review_spec_parity() -> None:
    require_source("openspec/specs/review/spec.md")
    assert (SOURCE_REPO / "starter/openspec/specs/review/spec.md").read_bytes() == (
        SOURCE_REPO / "openspec/specs/review/spec.md"
    ).read_bytes()


def test_init_openspec_symlink_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "openspec").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InitError, match="symbolic link") as exc:
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, force=True)
    assert str(repo / "openspec") in str(exc.value)
    assert list(outside.iterdir()) == []
    assert not (repo / ".claude").exists()


def test_init_openspec_directory_at_file_path_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "openspec/README.md").mkdir(parents=True)

    with pytest.raises(InitError, match="directory where kerbtap installs a file") as exc:
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, force=True)
    assert "openspec/README.md" in str(exc.value)
    assert not (repo / ".claude/settings.json").exists()


def test_init_openspec_file_at_directory_path_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "openspec").write_text("x", encoding="utf-8")

    with pytest.raises(InitError, match="file where kerbtap needs a directory"):
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert not (repo / ".claude/settings.json").exists()


def test_init_openspec_write_error_is_an_init_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    original = Path.write_bytes

    def failing(self: Path, data: bytes) -> int:
        if "openspec" in self.parts:
            raise OSError("disk full")
        return original(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing)
    with pytest.raises(InitError, match="disk full") as exc:
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert "openspec" in str(exc.value)
    assert str(repo / "openspec") in str(exc.value)


def test_init_claude_symlink_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".claude").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InitError, match="symbolic link") as exc:
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, force=True)
    assert str(repo / ".claude") in str(exc.value)
    assert list(outside.iterdir()) == []


def test_init_claude_directory_at_file_path_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".claude/hooks/fact_claim_guard.py").mkdir(parents=True)

    with pytest.raises(InitError, match="directory where kerbtap installs a file") as exc:
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, force=True)
    assert ".claude/hooks/fact_claim_guard.py" in str(exc.value)
    assert not (repo / ".claude/settings.json").exists()


def test_init_claude_write_error_is_an_init_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    original = Path.write_bytes

    def failing(self: Path, data: bytes) -> int:
        if ".claude" in self.parts:
            raise OSError("disk full")
        return original(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing)
    with pytest.raises(InitError, match="disk full") as exc:
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert str(repo / ".claude") in str(exc.value)


def test_init_settings_symlink_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    (repo / ".claude").mkdir()
    (repo / ".claude/settings.json").symlink_to(target)

    with pytest.raises(InitError, match="symbolic link") as exc:
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, force=True)
    assert str(repo / ".claude/settings.json") in str(exc.value)
    assert target.read_text(encoding="utf-8") == "{}"


def test_init_dangling_settings_symlink_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = tmp_path / "missing.json"
    (repo / ".claude").mkdir()
    (repo / ".claude/settings.json").symlink_to(target)

    with pytest.raises(InitError, match="symbolic link"):
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, force=True)
    assert not target.exists()


def test_init_dangling_claude_md_symlink_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = tmp_path / "missing-claude.md"
    (repo / "CLAUDE.md").symlink_to(target)

    with pytest.raises(InitError, match="symbolic link"):
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert not target.exists()


def test_init_dangling_pending_symlink_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = tmp_path / "missing-pending.md"
    (repo / "CLAUDE.md").write_text("# Mine\n", encoding="utf-8")
    (repo / ".governor/pending").mkdir(parents=True)
    (repo / ".governor/pending/claude-md.md").symlink_to(target)

    with pytest.raises(InitError, match="symbolic link"):
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert not target.exists()


def test_init_dangling_ledger_symlink_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = tmp_path / "missing-ledger.md"
    (repo / "docs").mkdir()
    (repo / "docs/sources-ledger.md").symlink_to(target)

    with pytest.raises(InitError, match="symbolic link"):
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert not target.exists()


def test_init_write_oserror_is_init_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    original = Path.write_text

    def fail_on_ledger(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == "sources-ledger.md":
            raise OSError("disk full")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_on_ledger)

    with pytest.raises(InitError, match="disk full") as exc:
        init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    assert str(repo / "docs/sources-ledger.md") in str(exc.value)


def test_code_review_is_opt_in(tmp_path: Path) -> None:
    mixed = _repo(tmp_path, name="mixedDefault")
    (mixed / "docs").mkdir()
    (mixed / "src").mkdir()
    init(repo=mixed, source_repo=SOURCE_REPO, run_tests=False)
    guard = (mixed / ".claude/hooks/precommit_review_guard.py").read_text(encoding="utf-8")
    assert 'TRACKED_PATH_PREFIXES = ("docs/",)' in guard
    assert ".py" not in guard.split("TRACKED_SUFFIXES = ")[1].split("\n")[0]

    code = _repo(tmp_path, name="codeDefault")
    (code / "src").mkdir()
    result = init(repo=code, source_repo=SOURCE_REPO, run_tests=False)
    assert not (code / ".claude/hooks/precommit_review_guard.py").exists()
    assert "precommit_review_guard.py" in result.dropped
    assert "precommit_review_guard" not in (code / ".claude/settings.json").read_text(encoding="utf-8")

    opted = _repo(tmp_path, name="codeOptIn")
    (opted / "src").mkdir()
    init(repo=opted, source_repo=SOURCE_REPO, run_tests=False, review_code=True)
    guard = (opted / ".claude/hooks/precommit_review_guard.py").read_text(encoding="utf-8")
    assert 'TRACKED_PATH_PREFIXES = ("src/", "tests/")' in guard
    assert 'TRACKED_SUFFIXES = (".py",)' in guard


def test_starter_ticket_has_no_cut_steps() -> None:
    text = (SOURCE_REPO / "starter/.claude/skills/ticket/SKILL.md").read_text(encoding="utf-8")
    for banned in (
        "split", "invariant", "rejection set", "## 2b", "specs/<KEY>", "sweep",
        "'s go", "hold every push", "then hold", "explicitly approves",
    ):
        assert banned not in text, banned


def test_rerun_keeps_an_installed_code_review_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path, name="codeUpgrade")
    (repo / "src").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, review_code=True)
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    guard = (repo / ".claude/hooks/precommit_review_guard.py").read_text(encoding="utf-8")
    assert 'TRACKED_SUFFIXES = (".py",)' in guard


def test_cli_review_code_flag_installs_the_code_review_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kerbtap import cli

    monkeypatch.setattr(cli, "_plugin_installed", lambda: True)  # never reach the machine tier

    repo = _repo(tmp_path, name="codeCli")
    (repo / "src").mkdir()
    rc = cli.main(["init", str(repo), "--source", str(SOURCE_REPO), "--no-tests", "--review-code"])
    assert rc == 0
    guard = (repo / ".claude/hooks/precommit_review_guard.py").read_text(encoding="utf-8")
    assert 'TRACKED_SUFFIXES = (".py",)' in guard


def test_claude_md_block_matches_a_code_review_install(tmp_path: Path) -> None:
    from kerbtap.init import render_claude_md_block

    repo = _repo(tmp_path, name="codeBlock")
    (repo / "src").mkdir()
    result = init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, review_code=True)
    assert render_claude_md_block(repo, SOURCE_REPO) == result.claude_md_block


def _commit_payload(repo: Path) -> str:
    return json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": str(repo)}
    )


def test_installed_doc_gate_warns_by_default(tmp_path: Path) -> None:
    """DG-001: the doc gate of a fresh install warns and does not block."""
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    hook = repo / ".claude/hooks/precommit_doc_consistency_guard.py"
    doc = next(
        line.strip().strip('",')
        for line in hook.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith('"') and line.strip().rstrip(",").endswith('.md"')
    )
    (repo / doc).parent.mkdir(parents=True, exist_ok=True)
    (repo / doc).write_text("draft\n", encoding="utf-8")
    _git(["add", doc], repo)

    proc = subprocess.run(
        [sys.executable, str(hook)], input=_commit_payload(repo), text=True, capture_output=True,
        env={"CLAUDE_PROJECT_DIR": str(repo), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert "permissionDecision" not in proc.stdout
    assert json.loads(proc.stdout)["systemMessage"].startswith("KERBTAP SHADOW")


def test_starter_doc_gate_does_not_block(tmp_path: Path) -> None:
    """DG-004: the starter template hook, with one tracked doc, warns and does not block."""
    import re
    import shutil

    starter = SOURCE_REPO / "starter" / ".claude" / "hooks"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy(starter / "commit_scope.py", hooks / "commit_scope.py")
    source = (starter / "precommit_doc_consistency_guard.py").read_text(encoding="utf-8")
    patched, count = re.subn(
        r"TRACKED_PATHS = frozenset\(\n.*?\n\)", 'TRACKED_PATHS = frozenset({"CLAUDE.md"})',
        source, count=1, flags=re.DOTALL,
    )
    assert count == 1
    (hooks / "precommit_doc_consistency_guard.py").write_text(patched, encoding="utf-8")
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").write_text("draft\n", encoding="utf-8")
    _git(["add", "CLAUDE.md"], repo)

    proc = subprocess.run(
        [sys.executable, str(hooks / "precommit_doc_consistency_guard.py")],
        input=_commit_payload(repo), text=True, capture_output=True,
        env={"CLAUDE_PROJECT_DIR": str(repo), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert "permissionDecision" not in proc.stdout
    assert "systemMessage" in proc.stdout


DOCTRINE_FILES = (
    "CLAUDE.md",
    ".claude/skills/ticket/SKILL.md",
    ".claude/skills/sprint-start/SKILL.md",
    ".claude/skills/wrap-up/SKILL.md",
    "USER-MANUAL.md",
)
BLOCKING_DOC_PASS_TEXT = (
    "Tier 1 #4",
    "Canonical living docs get a `/verify-claims doc-pass` before commit",
    "**Denies** `git commit` touching a canonical living doc",
    "a canonical living doc needs the",
    "The commit needs the `/verify-claims doc-pass` first",
    "a typo in `CLAUDE.md` still needs the doc-pass",
)


def test_doc_pass_is_not_a_hard_gate() -> None:
    """DG-005: no doctrine file calls the doc-pass a Tier 1 or blocking gate."""
    require_source(*DOCTRINE_FILES)
    for rel in DOCTRINE_FILES:
        text = (SOURCE_REPO / rel).read_text(encoding="utf-8")
        for phrase in BLOCKING_DOC_PASS_TEXT:
            assert phrase not in text, f"{rel} still holds {phrase!r}"
    rows = [
        line for line in (SOURCE_REPO / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| `precommit_doc_consistency_guard.py` |")
    ]
    assert len(rows) == 1
    assert rows[0].split("|")[3].strip().startswith("**Warns**")


_ALL_GATES = frozenset({
    "commit_scope.py", "fact_claim_guard.py", "mangled_text_guard.py",
    "precommit_doc_consistency_guard.py", "precommit_review_guard.py",
})
_ALL_FILES = _ALL_GATES | {
    "test_commit_scope.py", "test_fact_claim_guard.py",
    "test_precommit_doc_consistency_guard.py", "test_precommit_review_guard.py",
}
# Recorded from `render_hooks` on origin/main before the surface rename: (gates, dropped, files) for each case.
_TODAY = {
    "docs": (_ALL_GATES, [], _ALL_FILES),
    "code": (
        frozenset({"commit_scope.py", "mangled_text_guard.py", "precommit_doc_consistency_guard.py"}),
        ["fact_claim_guard.py", "precommit_review_guard.py", "test_fact_claim_guard.py", "test_precommit_review_guard.py"],
        frozenset({
            "commit_scope.py", "mangled_text_guard.py", "precommit_doc_consistency_guard.py",
            "test_commit_scope.py", "test_precommit_doc_consistency_guard.py",
        }),
    ),
    "code+review": (
        _ALL_GATES - {"fact_claim_guard.py"},
        ["fact_claim_guard.py", "test_fact_claim_guard.py"],
        _ALL_FILES - {"fact_claim_guard.py", "test_fact_claim_guard.py"},
    ),
    "both": (_ALL_GATES, [], _ALL_FILES),
    "both+review": (_ALL_GATES, [], _ALL_FILES),
}


def test_install_from_starter_alone_matches(tmp_path: Path) -> None:
    """A source tree with no ``.claude/`` renders today's gates, and the six gate files equal the dogfood copies (GS-001)."""
    from kerbtap.init import (
        Surfaces,
        _apply_surfaces,
        _rename_bypasses,
        _rewrite_test_fixtures,
        _strip_todos,
        expected_wiring,
        render_hooks,
    )

    dogfood = SOURCE_REPO / ".claude" / "hooks"
    if not dogfood.is_dir():
        pytest.skip("no .claude/hooks/ in this checkout")
    source = tmp_path / "source"
    shutil.copytree(SOURCE_REPO / "starter", source / "starter", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    assert not (source / ".claude").exists()

    cases = {
        "docs": Surfaces(docs=True, code=False),
        "code": Surfaces(docs=False, code=True),
        "code+review": Surfaces(docs=False, code=True, review_code=True),
        "both": Surfaces(docs=True, code=True),
        "both+review": Surfaces(docs=True, code=True, review_code=True),
    }
    assert set(cases) == set(_TODAY)
    for label, surfaces in cases.items():
        gates, dropped, files = _TODAY[label]
        alone = render_hooks(source, "demo", "DEMO", surfaces)
        assert alone.gates == gates, label
        assert alone.dropped == dropped, label
        assert set(alone.files) == files, label
        assert expected_wiring("DEMO", alone.gates) == expected_wiring("DEMO", set(gates)), label
        for name in GATE_FILES:
            if name not in files:
                continue
            text = (dogfood / name).read_text(encoding="utf-8")
            for governor_name, project_name in BYPASS_RENAMES:
                text = text.replace(governor_name, project_name)
            oracle = _strip_todos(_apply_surfaces(name, _rename_bypasses(text, "DEMO"), surfaces, "demo"))
            if name.startswith("test_"):
                oracle = _rewrite_test_fixtures(name, oracle, surfaces)
            assert alone.files[name] == oracle, f"{label}: {name} differs from the dogfood copy"


def _ignored(repo: Path, rel: str) -> bool:
    return subprocess.run(["git", "check-ignore", "-q", rel], cwd=repo).returncode == 0


def test_init_ignores_transient_state_new_install(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    lines = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".kerbtap/review-packages/" in lines
    assert ".kerbtap/telemetry/" in lines
    assert _ignored(repo, ".kerbtap/review-packages/x.json")
    assert _ignored(repo, ".kerbtap/telemetry/x.jsonl")
    assert not _ignored(repo, ".kerbtap/reviews/x.json")


def test_init_ignores_transient_state_old_install_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".governor").mkdir()
    (repo / ".gitignore").write_text("node_modules/\n*.log", encoding="utf-8")
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False)
    lines = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["node_modules/", "*.log"]
    assert lines.count(".governor/review-packages/") == 1
    assert lines.count(".governor/telemetry/") == 1
    assert not any(line.startswith(".kerbtap") for line in lines)
    assert _ignored(repo, ".governor/review-packages/x.json")
    assert _ignored(repo, ".governor/telemetry/x.jsonl")
    assert not _ignored(repo, ".governor/reviews/x.json")
