"""``kerbtap doctor`` classifies each gate by what the wired command does on two probes (DR-001..006)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from kerbtap import git_hook
from kerbtap.cli import main
from kerbtap.doctor import run_doctor
from kerbtap.init import init
from tests.repo_factory import init_repo

SOURCE_REPO = Path(__file__).resolve().parents[2]
REVIEW, DOC = "precommit_review_guard", "precommit_doc_consistency_guard"
ALLOW_SCRIPT = "import sys\nsys.exit(0)\n"


def _install(tmp_path: Path, *, adapter: bool = False, code: bool = False, docs: bool = True) -> Path:
    repo = init_repo(tmp_path / "carbonLedger")
    if docs:
        (repo / "docs").mkdir()
    if code:
        (repo / "src").mkdir()
    init(repo=repo, source_repo=SOURCE_REPO, run_tests=False, git_hook=False)
    if adapter:
        git_hook.install(repo)
    return repo


def _results(repo: Path) -> tuple[dict[str, str], int]:
    lines, code = run_doctor(repo)
    return {line.split(": ", 1)[0]: line.split(": ", 1)[1] for line in lines}, code


def _hook(repo: Path, gate: str) -> Path:
    return repo / ".claude" / "hooks" / f"{gate}.py"


def _allowing_hook(path: Path) -> None:
    kept = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if re.match(r"(TRACKED_PATH_PREFIXES|TRACKED_SUFFIXES)\s*=", line)
    ]
    path.write_text("\n".join(kept) + "\n" + ALLOW_SCRIPT, encoding="utf-8")


def _snapshot(repo: Path) -> dict[str, str]:
    return {
        str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def test_healthy_install_fires(tmp_path: Path) -> None:
    results, code = _results(_install(tmp_path))
    assert results == {
        REVIEW: "fires", DOC: "fires (shadow)", "git adapter": "not installed",
    }
    assert code == 0


def test_code_only_install_skips_review_gate(tmp_path: Path) -> None:
    results, code = _results(_install(tmp_path, code=True, docs=False))
    assert results[REVIEW] == "not installed"
    assert code == 0


def test_doctor_lists_no_dispatch_gate(tmp_path: Path) -> None:
    results, _ = _results(_install(tmp_path, code=True))
    assert not [name for name in results if name.startswith("review_dispatch_guard")]


def test_subdirectory_resolves_to_repo_root(tmp_path: Path) -> None:
    repo = _install(tmp_path)
    results, code = _results(repo / "docs")
    assert [results[g] for g in (REVIEW, DOC)] == ["fires", "fires (shadow)"]
    assert code == 0


def test_no_install_fails(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "bare")
    lines, code = run_doctor(repo)
    assert lines == [f"no kerbtap install found at {repo.resolve()}"]
    assert code == 1
    lines, code = run_doctor(tmp_path)
    assert lines == [f"no git repository at {tmp_path}"]
    assert code == 1


def test_unwired_gate(tmp_path: Path) -> None:
    repo = _install(tmp_path)
    settings = repo / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    for entry in data["hooks"]["PreToolUse"]:
        entry["hooks"] = [h for h in entry["hooks"] if f"{REVIEW}.py" not in h["command"]]
    settings.write_text(json.dumps(data), encoding="utf-8")
    results, code = _results(repo)
    assert results[REVIEW] == "not wired"
    assert results[DOC] == "fires (shadow)"
    assert code == 1


def test_missing_engine_is_stuck(tmp_path: Path) -> None:
    """The review gate denies, and the doc gate warns, on every command: both are `stuck`."""
    repo = _install(tmp_path)
    (repo / ".claude" / "hooks" / "commit_scope.py").unlink()
    results, code = _results(repo)
    assert results[REVIEW] == "stuck"
    assert results[DOC] == "stuck (shadow)"
    assert code == 1


def test_syntax_error_is_crashed(tmp_path: Path) -> None:
    repo = _install(tmp_path)
    _hook(repo, DOC).write_text("def broken(:\n", encoding="utf-8")
    results, _ = _results(repo)
    assert results[DOC] == "crashed"
    assert results[REVIEW] == "fires"


def test_allowing_hook_is_silent(tmp_path: Path) -> None:
    repo = _install(tmp_path)
    _allowing_hook(_hook(repo, REVIEW))
    results, code = _results(repo)
    assert results[REVIEW] == "silent"
    assert code == 1


def test_shadow_is_marked(tmp_path: Path) -> None:
    repo = _install(tmp_path)
    (repo / ".governor").mkdir()
    (repo / ".governor" / "gate-mode.json").write_text(json.dumps({DOC: "shadow"}), encoding="utf-8")
    lines, code = run_doctor(repo)
    assert f"{DOC}: fires (shadow)" in lines
    assert f"{REVIEW}: fires" in lines
    assert code == 0


def test_doctor_reports_doc_gate_shadow(tmp_path: Path) -> None:
    """DG-003: a fresh install, no mode file: the doc gate is `fires (shadow)` and the exit code is 0."""
    lines, code = run_doctor(_install(tmp_path))
    assert f"{DOC}: fires (shadow)" in lines
    assert f"{REVIEW}: fires" in lines
    assert code == 0


def test_doctor_labels_shadow_from_the_probe(tmp_path: Path) -> None:
    """A doc gate that denies the probe, with no mode file, is `fires` with no `(shadow)` label."""
    repo = _install(tmp_path)
    _hook(repo, DOC).write_text(
        'TRACKED_PATHS = frozenset({"CLAUDE.md"})\n'
        "import json, sys\n"
        "command = json.load(sys.stdin)['tool_input']['command']\n"
        "if 'commit' in command:\n"
        "    print(json.dumps({'hookSpecificOutput': {'permissionDecision': 'deny'}}))\n",
        encoding="utf-8",
    )
    lines, code = run_doctor(repo)
    assert f"{DOC}: fires" in lines
    assert code == 0


def test_old_shadow_prefix_is_marked(tmp_path: Path) -> None:
    """DR-008: a gate installed before the rename prints `GOVERNOR SHADOW`; doctor still marks it."""
    repo = _install(tmp_path)
    _hook(repo, DOC).write_text(
        'TRACKED_PATHS = frozenset({"CLAUDE.md"})\n'
        "import json, sys\n"
        "command = json.load(sys.stdin)['tool_input']['command']\n"
        "if 'commit' in command:\n"
        "    print(json.dumps({'systemMessage': 'GOVERNOR SHADOW (precommit_doc_consistency_guard would deny): x'}))\n",
        encoding="utf-8",
    )
    lines, code = run_doctor(repo)
    assert any(line.startswith(f"{DOC}: ") and line.endswith("(shadow)") for line in lines)
    assert code == 0


def test_repo_unchanged(tmp_path: Path) -> None:
    repo = _install(tmp_path, adapter=True)
    before = _snapshot(repo)
    _, code = run_doctor(repo)
    assert code == 0
    assert _snapshot(repo) == before


def test_git_adapter_fires(tmp_path: Path) -> None:
    results, code = _results(_install(tmp_path, adapter=True))
    assert results["git adapter"] == "fires"
    assert code == 0


def test_git_adapter_silent(tmp_path: Path) -> None:
    repo = _install(tmp_path, adapter=True)
    (repo / ".claude" / "git-hooks" / "pre_commit.py").write_text(ALLOW_SCRIPT, encoding="utf-8")
    results, code = _results(repo)
    assert results["git adapter"] == "silent"
    assert code == 1


def test_runs_wired_command(tmp_path: Path) -> None:
    repo = _install(tmp_path)
    copy = repo / ".claude" / "hooks" / "review_copy.py"
    copy.write_text(_hook(repo, REVIEW).read_text(encoding="utf-8"), encoding="utf-8")
    _allowing_hook(copy)
    settings = repo / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    for entry in data["hooks"]["PreToolUse"]:
        for hook in entry["hooks"]:
            if f"{REVIEW}.py" in hook["command"]:
                hook["command"] = f'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/review_copy.py" # {REVIEW}.py'
    settings.write_text(json.dumps(data), encoding="utf-8")
    results, _ = _results(repo)
    assert results[REVIEW] == "silent"
    assert _hook(repo, REVIEW).read_text(encoding="utf-8") != ALLOW_SCRIPT


def test_cli_exit_code_and_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _install(tmp_path)
    assert main(["doctor", str(repo)]) == 0
    assert f"{REVIEW}: fires" in capsys.readouterr().out
