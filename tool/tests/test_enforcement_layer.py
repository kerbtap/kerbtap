"""The installed enforcement layer, per gate: matched / modified / unwired / absent / indeterminate.

Every case starts from a real ``governor init`` install, so "matched" is proven against the
installer's own rendering rather than a hand-written expectation.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from kerbtap.assemble import build_bundle
from kerbtap.build import render_pack
from kerbtap.collect import github as github_mod
from kerbtap.collect.enforcement_layer import (
    ABSENT,
    INDETERMINATE,
    MATCHED,
    MODIFIED,
    UNWIRED,
    actual_wiring,
    observe,
)
from kerbtap.init import init
from tests.conftest import require_source
from tests.repo_factory import commit_files, git, init_repo

SOURCE_REPO = Path(__file__).resolve().parents[2]
HOOKS = ".claude/hooks"


def _installed(tmp_path: Path, *, docs: bool = True, code: bool = True, prefix: str | None = None) -> Path:
    repo = init_repo(tmp_path / "target")
    if docs:
        (repo / "docs").mkdir()
        (repo / "docs/readme.md").write_text("docs\n")
    if code:
        (repo / "src").mkdir()
        (repo / "src/app.py").write_text("x = 1\n")
    init(repo, SOURCE_REPO, prefix=prefix, run_tests=False, git_hook=False)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "install")
    return repo


def _states(repo: Path, source: Path = SOURCE_REPO) -> dict[str, str]:
    record = observe(repo, source, "0.1.0")
    return {row["path"].rsplit("/", 1)[-1]: row["state"] for row in record["gates"]}


def _settings(repo: Path) -> dict:
    return json.loads((repo / ".claude/settings.json").read_text())


def _write_settings(repo: Path, data: dict) -> None:
    (repo / ".claude/settings.json").write_text(json.dumps(data, indent=2))


def test_fresh_install_is_matched_everywhere(tmp_path: Path) -> None:
    repo = _installed(tmp_path)
    states = _states(repo)
    assert states == {
        "commit_scope.py": MATCHED,
        "fact_claim_guard.py": MATCHED,
        "mangled_text_guard.py": MATCHED,
        "precommit_doc_consistency_guard.py": MATCHED,
        "precommit_review_guard.py": MATCHED,
    }


def test_custom_prefix_install_is_matched(tmp_path: Path) -> None:
    repo = _installed(tmp_path, prefix="ACME")
    assert set(_states(repo).values()) == {MATCHED}


def test_one_byte_change_is_modified(tmp_path: Path) -> None:
    repo = _installed(tmp_path)
    path = repo / HOOKS / "precommit_review_guard.py"
    data = path.read_bytes()
    path.write_bytes(data[:-1] + (b" " if data[-1:] != b" " else b"\n"))
    states = _states(repo)
    assert states["precommit_review_guard.py"] == MODIFIED
    assert states["precommit_doc_consistency_guard.py"] == MATCHED


def test_modified_library_is_modified(tmp_path: Path) -> None:
    repo = _installed(tmp_path)
    (repo / HOOKS / "commit_scope.py").write_text("# gutted\n")
    assert _states(repo)["commit_scope.py"] == MODIFIED


def test_missing_gate_is_absent(tmp_path: Path) -> None:
    repo = _installed(tmp_path)
    (repo / HOOKS / "fact_claim_guard.py").unlink()
    assert _states(repo)["fact_claim_guard.py"] == ABSENT


def test_unwired_gate(tmp_path: Path) -> None:
    repo = _installed(tmp_path)
    data = _settings(repo)
    for entry in data["hooks"]["PreToolUse"]:
        entry["hooks"] = [h for h in entry["hooks"] if "precommit_doc_consistency_guard" not in h["command"]]
    _write_settings(repo, data)
    assert _states(repo)["precommit_doc_consistency_guard.py"] == UNWIRED


def test_no_settings_file_leaves_every_gate_unwired(tmp_path: Path) -> None:
    repo = _installed(tmp_path)
    (repo / ".claude/settings.json").unlink()
    states = _states(repo)
    assert {v for k, v in states.items() if k != "commit_scope.py"} == {UNWIRED}
    assert states["commit_scope.py"] == MATCHED


def test_duplicate_wiring_is_indeterminate(tmp_path: Path) -> None:
    repo = _installed(tmp_path)
    data = _settings(repo)
    bash = next(e for e in data["hooks"]["PreToolUse"] if e["matcher"] == "Bash")
    review = next(h for h in bash["hooks"] if "precommit_review_guard" in h["command"])
    bash["hooks"].append(dict(review))
    _write_settings(repo, data)
    assert _states(repo)["precommit_review_guard.py"] == INDETERMINATE


def test_stale_extra_wiring_is_indeterminate(tmp_path: Path) -> None:
    repo = _installed(tmp_path)
    data = _settings(repo)
    data["hooks"]["PreToolUse"].append({
        "matcher": "Edit",
        "hooks": [{"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/precommit_review_guard.py"'}],
    })
    _write_settings(repo, data)
    assert _states(repo)["precommit_review_guard.py"] == INDETERMINATE


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"command": "echo .claude/hooks/precommit_review_guard.py"}, id="command-only-mentions-file"),
        pytest.param({"timeout": 1}, id="different-timeout"),
        pytest.param({"type": "prompt"}, id="different-type"),
    ],
)
def test_entry_that_names_the_gate_but_differs_is_indeterminate(tmp_path: Path, change: dict) -> None:
    repo = _installed(tmp_path)
    data = _settings(repo)
    bash = next(e for e in data["hooks"]["PreToolUse"] if e["matcher"] == "Bash")
    review = next(h for h in bash["hooks"] if "precommit_review_guard" in h["command"])
    review.update(change)
    _write_settings(repo, data)
    assert _states(repo)["precommit_review_guard.py"] == INDETERMINATE


def test_source_side_uncommitted_changes_are_flagged(tmp_path: Path) -> None:
    require_source(".claude/hooks")
    source = tmp_path / "governor-src"
    for rel in ("starter/.claude", ".claude/hooks"):
        shutil.copytree(SOURCE_REPO / rel, source / rel, ignore=shutil.ignore_patterns("__pycache__"))
    (source / "tool/kerbtap").mkdir(parents=True)
    shutil.copy2(SOURCE_REPO / "tool/kerbtap/init.py", source / "tool/kerbtap/init.py")
    git(source, "init", "-q")
    git(source, "add", "-A")
    git(source, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "src")
    repo = _installed(tmp_path)
    assert observe(repo, source, "0.1.0")["governor_uncommitted_changes"] is False
    with (source / "tool/kerbtap/init.py").open("a") as handle:
        handle.write("# local edit to the renderer\n")
    assert observe(repo, source, "0.1.0")["governor_uncommitted_changes"] is True
    git(source, "checkout", "--", "tool/kerbtap/init.py")
    (source / "starter/.claude/hooks/mangled_text_guard.py").write_text("# local edit\n")
    record = observe(repo, source, "0.1.0")
    assert record["governor_uncommitted_changes"] is True
    rows = {row["path"].rsplit("/", 1)[-1]: row["state"] for row in record["gates"]}
    assert rows["mangled_text_guard.py"] == MODIFIED
    report = render_pack(build_bundle(repo, source_repo=source))["report.md"]
    assert "its hook sources or installer have uncommitted changes" in report


def test_malformed_settings_make_gates_indeterminate(tmp_path: Path) -> None:
    repo = _installed(tmp_path)
    (repo / ".claude/settings.json").write_text("{ not json")
    states = _states(repo)
    assert {v for k, v in states.items() if k != "commit_scope.py"} == {INDETERMINATE}


def test_non_governor_hooks_are_ignored(tmp_path: Path) -> None:
    repo = _installed(tmp_path)
    data = _settings(repo)
    data["hooks"]["PreToolUse"].append(
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 .claude/hooks/my_team_guard.py"}]}
    )
    data["permissions"] = {"deny": ["Bash(rm:*)"]}
    _write_settings(repo, data)
    (repo / HOOKS / "my_team_guard.py").write_text("print('hi')\n")
    states = _states(repo)
    assert "my_team_guard.py" not in states
    assert set(states.values()) == {MATCHED}


def test_dropped_gate_is_not_reported_and_a_stray_copy_is_indeterminate(tmp_path: Path) -> None:
    repo = _installed(tmp_path, docs=False)
    states = _states(repo)
    assert "precommit_review_guard.py" not in states  # a code-only install drops it
    (repo / HOOKS / "precommit_review_guard.py").write_text("# copied by hand\n")
    assert _states(repo)["precommit_review_guard.py"] == INDETERMINATE


def test_symlinked_gate_is_indeterminate(tmp_path: Path) -> None:
    repo = _installed(tmp_path)
    gate = repo / HOOKS / "mangled_text_guard.py"
    real = tmp_path / "elsewhere.py"
    real.write_text(gate.read_text())
    gate.unlink()
    os.symlink(real, gate)
    assert _states(repo)["mangled_text_guard.py"] == INDETERMINATE


def test_rendering_failure_is_indeterminate_and_path_free(tmp_path: Path) -> None:
    repo = _installed(tmp_path)
    record = observe(repo, tmp_path / "no-governor-here", "0.1.0")
    assert {row["state"] for row in record["gates"]} == {INDETERMINATE}
    assert str(tmp_path) not in json.dumps(record)


def test_repository_without_governor_is_not_installed(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "plain")
    commit_files(repo, "x", {"a.md": "a\n"})
    record = observe(repo, SOURCE_REPO, "0.1.0")
    assert record["installed"] is False
    assert record["gates"] == []


def test_uncommitted_claude_changes_are_flagged(tmp_path: Path) -> None:
    repo = _installed(tmp_path)
    assert observe(repo, SOURCE_REPO, "0.1.0")["claude_dir_uncommitted_changes"] is False
    (repo / HOOKS / "precommit_review_guard.py").write_text("# edited\n")
    assert observe(repo, SOURCE_REPO, "0.1.0")["claude_dir_uncommitted_changes"] is True


def test_actual_wiring_rejects_wrong_shapes() -> None:
    assert actual_wiring(b"[]") is None
    assert actual_wiring(b'{"hooks": {"PreToolUse": {}}}') is None
    assert actual_wiring(b'{"hooks": {"PreToolUse": [{"matcher": 3, "hooks": []}]}}') is None
    assert actual_wiring(b"\xff") is None
    assert actual_wiring(b"{}") == {}


def test_pack_reports_the_layer_within_its_wording_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_mod, "gh_available", lambda: False)
    repo = _installed(tmp_path)
    (repo / HOOKS / "precommit_review_guard.py").write_text("# gutted\n")
    rendered = render_pack(build_bundle(repo, source_repo=SOURCE_REPO))
    report = rendered["report.md"]
    assert "## Installed enforcement layer (observed at assessment time)" in report
    assert "| `.claude/hooks/precommit_review_guard.py` | gate | PreToolUse / Bash | modified |" in report
    assert "`.claude/` has uncommitted changes" in report
    lowered = report.lower()
    for word in ("authentic", "unmodified historically", "continuously enforced"):
        assert word not in lowered
    manifest = json.loads(rendered["manifest.json"])
    layer = manifest["enforcement_layer"]
    assert layer["installed"] is True
    assert {row["state"] for row in layer["gates"]} == {MATCHED, MODIFIED}
