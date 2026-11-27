"""Installing over existing settings keeps what the user wired (research round R10).

``governor init --force`` used to replace ``.claude/settings.json`` outright, and the machine
tier was never written at all. Both now merge: Governor's own entries are replaced, anything
else is kept, and a command Governor did not write that runs a Governor hook stops the install.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kerbtap.collect.enforcement_layer import MATCHED, observe
from kerbtap.init import InitError, hook_command, init, project_hook_files
from kerbtap.machine import machine_hook_command
from kerbtap.settings_merge import (
    SettingsMergeError,
    load_settings,
    merge_hooks,
    render_text,
    write_settings,
)
from tests.repo_factory import git, init_repo

REPO = Path(__file__).resolve().parents[2]

GATE = "precommit_review_guard.py"
OWNED = {GATE, "mangled_text_guard.py"}
OWNED_COMMANDS = {hook_command(name) for name in OWNED}
RENDERED = {
    "_comment": "governor",
    "permissions": {"deny": []},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": hook_command(GATE), "timeout": 10}]}
        ]
    },
}


def _merge(existing: dict) -> dict:
    return merge_hooks(existing, RENDERED, OWNED_COMMANDS, OWNED)


class TestMerge:
    def test_user_hooks_events_and_keys_survive(self) -> None:
        existing = {
            "permissions": {"deny": ["Bash(rm:*)"]},
            "model": "opus",
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 my_guard.py"}]},
                    {"matcher": "Edit", "hooks": [{"type": "command", "command": "python3 fmt.py"}]},
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "python3 done.py"}]}],
            },
        }
        merged = _merge(existing)
        assert merged["permissions"] == {"deny": ["Bash(rm:*)"]}  # not replaced by the rendered empty one
        assert merged["model"] == "opus"
        assert merged["_comment"] == "governor"  # a key the file lacked is added
        bash = next(e for e in merged["hooks"]["PreToolUse"] if e["matcher"] == "Bash")
        assert [h["command"] for h in bash["hooks"]] == ["python3 my_guard.py", hook_command(GATE)]
        assert merged["hooks"]["Stop"][0]["hooks"][0]["command"] == "python3 done.py"
        assert any(e.get("matcher") == "Edit" for e in merged["hooks"]["PreToolUse"])

    def test_merging_twice_changes_nothing(self) -> None:
        once = _merge({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "python3 my_guard.py"}]}]}})
        assert _merge(once) == once

    def test_stale_owned_entry_is_replaced_not_duplicated(self) -> None:
        existing = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": hook_command(GATE), "timeout": 99}]}]}}
        merged = _merge(existing)
        entries = merged["hooks"]["PreToolUse"]
        assert len(entries) == 1
        assert entries[0]["hooks"] == [{"type": "command", "command": hook_command(GATE), "timeout": 10}]

    def test_owned_entry_for_a_gate_no_longer_rendered_is_removed(self) -> None:
        existing = {"hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [
            {"type": "command", "command": hook_command("mangled_text_guard.py"), "timeout": 5}]}]}}
        assert _merge(existing)["hooks"]["PostToolUse"] == []

    def test_an_entry_that_held_only_our_hooks_goes_with_them(self) -> None:
        existing = {"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": hook_command(GATE)}]}],
            "Stop": [{"hooks": []}],
        }}
        merged = _merge(existing)
        assert len(merged["hooks"]["PreToolUse"]) == 1  # the rendered one only
        assert merged["hooks"]["Stop"] == [{"hooks": []}]  # an empty entry the user had stays

    def test_a_foreign_command_running_our_hook_stops_the_merge(self) -> None:
        for command in (
            f'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/{GATE}" --flag',
            f"uv run .claude/hooks/{GATE}",
            f"python3 /abs/repo/.claude/hooks/{GATE}",
        ):
            existing = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": command}]}]}}
            with pytest.raises(SettingsMergeError, match="does not write"):
                _merge(existing)

    def test_a_command_naming_someone_elses_hook_is_kept(self) -> None:
        existing = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "python3 .claude/hooks/my_team_guard.py"}]}]}}
        assert _merge(existing)["hooks"]["PreToolUse"][0]["hooks"][0]["command"].endswith("my_team_guard.py")

    @pytest.mark.parametrize(
        "existing",
        [
            pytest.param([], id="not-an-object"),
            pytest.param({"hooks": []}, id="hooks-not-an-object"),
            pytest.param({"hooks": {"PreToolUse": {}}}, id="event-not-a-list"),
            pytest.param({"hooks": {"PreToolUse": [{"matcher": "Bash"}]}}, id="entry-without-hooks"),
            pytest.param({"hooks": {"PreToolUse": [{"hooks": ["x"]}]}}, id="hook-not-an-object"),
        ],
    )
    def test_malformed_settings_are_refused(self, existing: object) -> None:
        with pytest.raises(SettingsMergeError):
            merge_hooks(existing, RENDERED, OWNED_COMMANDS, OWNED)  # type: ignore[arg-type]

    def test_unreadable_file_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text("{ not json")
        with pytest.raises(SettingsMergeError, match="not readable JSON"):
            load_settings(path)

    def test_backup_only_when_the_content_changes(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text(render_text({"model": "opus"}))
        backup = write_settings(path, {"model": "sonnet"})
        assert backup is not None and json.loads(backup.read_text())["model"] == "opus"
        assert write_settings(path, {"model": "sonnet"}) is None


class TestProjectInstall:
    def _repo(self, tmp_path: Path) -> Path:
        repo = init_repo(tmp_path / "shop")
        (repo / "docs").mkdir()
        init(repo, REPO, run_tests=False)
        return repo

    def test_force_keeps_user_settings_and_rewires_governor(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        settings = repo / ".claude/settings.json"
        data = json.loads(settings.read_text())
        data["permissions"] = {"deny": ["Bash(curl:*)"]}
        data["hooks"]["PreToolUse"].append(
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 .claude/hooks/my_team_guard.py"}]}
        )
        settings.write_text(render_text(data))

        result = init(repo, REPO, force=True, run_tests=False)
        assert result.settings_merged is True
        assert result.settings_backup is not None and result.settings_backup.exists()
        merged = json.loads(settings.read_text())
        assert merged["permissions"] == {"deny": ["Bash(curl:*)"]}
        commands = [h["command"] for e in merged["hooks"]["PreToolUse"] for h in e["hooks"]]
        assert "python3 .claude/hooks/my_team_guard.py" in commands
        assert commands.count(hook_command("precommit_review_guard.py")) == 1
        # The layer check still reads every gate as wired exactly as the installer wires it.
        states = {row["path"].rsplit("/", 1)[-1]: row["state"] for row in observe(repo, REPO, "0.1.0")["gates"]}
        assert set(states.values()) == {MATCHED}

    def test_second_force_install_changes_nothing(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        init(repo, REPO, force=True, run_tests=False)
        before = (repo / ".claude/settings.json").read_bytes()
        result = init(repo, REPO, force=True, run_tests=False)
        assert (repo / ".claude/settings.json").read_bytes() == before
        assert result.settings_backup is None

    def test_malformed_settings_stop_the_install_before_anything_is_written(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        gate = repo / ".claude/hooks/precommit_review_guard.py"
        gate.write_text("# edited by the user\n")
        (repo / ".claude/settings.json").write_text("{ not json")
        with pytest.raises(InitError, match="cannot merge"):
            init(repo, REPO, force=True, run_tests=False)
        assert gate.read_text() == "# edited by the user\n"

    def test_project_ownership_covers_every_installable_hook(self) -> None:
        files = project_hook_files(REPO)
        assert {"precommit_review_guard.py", "fact_claim_guard.py"} <= files
        assert not any(name.startswith("test_") for name in files)


def test_the_wiring_spec_matches_the_hooks_it_ships(tmp_path: Path) -> None:
    """Every machine gate is wired, and every wired command names a shipped hook."""
    spec = json.loads((REPO / "global/settings.hooks.json").read_text())
    wired = {
        h["command"] for entries in spec["hooks"].values() for e in entries for h in e["hooks"]
    }
    shipped = {p.name for p in (REPO / "global/hooks").glob("*.py") if not p.name.startswith("test_")}
    assert wired == {machine_hook_command(name) for name in shipped}
