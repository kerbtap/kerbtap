"""``kerbtap install-machine`` is the cutover to the plugin (CUT-001 to CUT-003).

A fake ``claude`` and a fake ``pwm`` on ``PATH`` and a scratch home stand in for the real ones: no test installs a
plugin or touches the real ``~/.claude``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from kerbtap.machine import (
    DEFAULT_MARKETPLACE,
    LEGACY_FILES_PATH,
    MachineInstallError,
    install_machine,
    legacy_files,
    machine_hook_command,
)
from kerbtap.settings_merge import render_text
from tests.conftest import require_source

REPO = Path(__file__).resolve().parents[2]
TOOL_ROOT = REPO / "tool"

FAKE_CLAUDE = """#!%(python)s
# Fake claude: records every call. The two list queries print FAKE_MARKETS and FAKE_PLUGINS (default: an empty
# list); FAKE_LIST_RC fails `plugin list --json`. `plugin install` and `plugin enable` fail when FAKE_INSTALL_RC or
# FAKE_ENABLE_RC is set, else they write `enabledPlugins` into $HOME/.claude/settings.json, as the real command does.
# `plugin marketplace add` fails with FAKE_ADD_RC, and echoes its source.
import json, os, sys
args = sys.argv[1:]
cmd = " ".join(args)
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(cmd + "\\n")
env = os.environ.get


def write_enabled(plugin):
    path = os.path.join(env("HOME"), ".claude", "settings.json")
    data = json.load(open(path)) if os.path.exists(path) else {}
    data.setdefault("enabledPlugins", {})[plugin] = True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(data, open(path, "w"), indent=2)


if cmd == "plugin marketplace list --json":
    sys.stdout.write(env("FAKE_MARKETS", "[]"))
elif cmd == "plugin list --json":
    if env("FAKE_LIST_RC"):
        sys.exit(int(env("FAKE_LIST_RC")))
    sys.stdout.write(env("FAKE_PLUGINS", "[]"))
elif cmd.startswith("plugin marketplace add") and env("FAKE_ADD_RC"):
    sys.stderr.write("cannot add " + args[-1] + "\\n")
    sys.exit(int(env("FAKE_ADD_RC")))
elif cmd.startswith("plugin install ") or cmd.startswith("plugin enable "):
    rc = env("FAKE_INSTALL_RC") if cmd.startswith("plugin install") else env("FAKE_ENABLE_RC")
    if rc:
        sys.stderr.write("refused\\n")
        sys.exit(int(rc))
    write_enabled(args[1 + 1])
"""


def _script(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A scratch home and a bin directory holding a fake python3 and a fake claude; PATH holds only that bin."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _script(bin_dir / "python3", "#!/bin/sh\necho 3.12\n")
    _script(bin_dir / "claude", FAKE_CLAUDE % {"python": sys.executable})
    log = tmp_path / "claude.log"
    for name in ("FAKE_INSTALL_RC", "FAKE_ENABLE_RC", "FAKE_ADD_RC", "FAKE_LIST_RC", "FAKE_MARKETS", "FAKE_PLUGINS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("FAKE_LOG", str(log))
    home = tmp_path / "home"
    home.mkdir()
    return {"home": home, "bin": bin_dir, "log": log}


def calls(world: dict) -> list[str]:
    return world["log"].read_text().splitlines() if world["log"].exists() else []


def snapshot(home: Path) -> dict:
    return {str(p.relative_to(home)): p.read_bytes() for p in sorted(home.rglob("*")) if p.is_file()}


KERBTAP_MARKET = {"name": "kerbtap", "source": "github", "repo": DEFAULT_MARKETPLACE}
USER_ENTRY = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/my_guard.py"}]}


def legacy_home(home: Path) -> None:
    """What the copy-based install left: the wiring, the copied files, plus a user hook entry, hook and agent."""
    claude = home / ".claude"
    spec = json.loads((REPO / "global/settings.hooks.json").read_text())
    spec["hooks"]["PreToolUse"].append(USER_ENTRY)
    spec["model"] = "opus"
    claude.mkdir()
    (claude / "settings.json").write_text(render_text(spec))
    for rel in legacy_files():
        path = claude / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"legacy copy of {rel}\n")
    (claude / "hooks" / "my_hook.py").write_text("# the user's own hook\n")
    (claude / "agents").mkdir(exist_ok=True)
    (claude / "agents" / "mine.md").write_text("the user's own agent\n")


def test_installs_the_plugin(world: dict) -> None:
    install_machine(world["home"])
    recorded = calls(world)
    assert f"plugin marketplace add {DEFAULT_MARKETPLACE}" in recorded
    assert "plugin install kerbtap@kerbtap --scope user" in recorded


def test_marketplace_override(world: dict) -> None:
    install_machine(world["home"], marketplace="/some/checkout")
    assert "plugin marketplace add /some/checkout" in calls(world)


def test_research_plugin_follows_pwm(world: dict) -> None:
    install_machine(world["home"])
    assert not any("kerbtap-research" in c for c in calls(world))
    world["log"].unlink()
    _script(world["bin"] / "pwm", "#!/bin/sh\nexit 0\n")
    install_machine(world["home"])
    assert "plugin install kerbtap-research@kerbtap --scope user" in calls(world)


def test_without_claude_nothing_changes(world: dict) -> None:
    legacy_home(world["home"])
    (world["bin"] / "claude").unlink()
    before = snapshot(world["home"])
    with pytest.raises(MachineInstallError, match="claude"):
        install_machine(world["home"])
    assert snapshot(world["home"]) == before


def test_failed_plugin_install_keeps_legacy(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_home(world["home"])
    monkeypatch.setenv("FAKE_INSTALL_RC", "1")
    before = snapshot(world["home"])
    with pytest.raises(MachineInstallError, match=r"plugin install kerbtap@kerbtap --scope user.*exited 1"):
        install_machine(world["home"])
    assert snapshot(world["home"]) == before


def test_legacy_home_is_cut_over(world: dict) -> None:
    home = world["home"]
    legacy_home(home)
    (home / ".claude" / "hooks" / "secret_exposure_guard.py").write_text("# the owner's edit\n")
    result = install_machine(home)
    claude = home / ".claude"

    settings = json.loads((claude / "settings.json").read_text())
    commands = [h["command"] for entries in settings["hooks"].values() for e in entries for h in e["hooks"]]
    assert commands == ["python3 ~/my_guard.py"]
    assert settings["model"] == "opus"
    assert result.settings_backup is not None and result.settings_backup.is_file()

    legacy = legacy_files()
    assert sorted(result.moved) == sorted(legacy)
    for rel in legacy:
        assert not (claude / rel).exists(), rel
        assert (result.legacy_dir / rel).is_file(), rel
    assert (result.legacy_dir / "hooks/secret_exposure_guard.py").read_text() == "# the owner's edit\n"
    assert (claude / "hooks" / "my_hook.py").read_text() == "# the user's own hook\n"
    assert (claude / "agents" / "mine.md").read_text() == "the user's own agent\n"
    assert not (claude / "agents" / "fullstack-guy").exists(), "an emptied legacy subfolder is removed"
    assert machine_hook_command("destructive_git_guard.py") not in commands


def test_foreign_entry_stops_before_anything(world: dict) -> None:
    home = world["home"]
    legacy_home(home)
    settings = json.loads((home / ".claude/settings.json").read_text())
    settings["hooks"]["PreToolUse"].append(
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/main_branch_push_guard.py"}]}
    )
    (home / ".claude/settings.json").write_text(render_text(settings))
    before = snapshot(home)
    with pytest.raises(MachineInstallError, match="does not write"):
        install_machine(home)
    assert snapshot(home) == before
    assert calls(world) == []


def test_second_run_changes_nothing(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    home = world["home"]
    legacy_home(home)
    install_machine(home)
    monkeypatch.setenv("FAKE_MARKETS", json.dumps([KERBTAP_MARKET]))
    monkeypatch.setenv("FAKE_PLUGINS", json.dumps([{"id": "kerbtap@kerbtap", "scope": "user", "enabled": True}]))
    world["log"].unlink()
    before = snapshot(home)
    folders = sorted(p.name for p in (home / ".claude").iterdir())
    result = install_machine(home)
    assert result.moved == [] and result.settings_backup is None and result.legacy_dir is None
    assert result.commands == [] and not result.wiring_removed
    assert snapshot(home) == before
    assert sorted(p.name for p in (home / ".claude").iterdir()) == folders
    assert not any(c.startswith(("plugin install", "plugin marketplace add")) for c in calls(world))


def test_a_disabled_plugin_is_enabled_before_the_legacy_hooks_go(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_home(world["home"])
    monkeypatch.setenv("FAKE_MARKETS", json.dumps([KERBTAP_MARKET]))
    monkeypatch.setenv("FAKE_PLUGINS", json.dumps([{"id": "kerbtap@kerbtap", "scope": "user", "enabled": False}]))
    result = install_machine(world["home"])
    assert "plugin enable kerbtap@kerbtap --scope user" in calls(world)
    assert "claude plugin enable kerbtap@kerbtap --scope user" in result.commands
    assert not any(c.startswith("plugin install kerbtap@kerbtap") for c in calls(world))
    assert result.moved, "the legacy files are moved once the plugin is enabled"


def test_a_failed_enable_keeps_legacy(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_home(world["home"])
    monkeypatch.setenv("FAKE_PLUGINS", json.dumps([{"id": "kerbtap@kerbtap", "scope": "user", "enabled": False}]))
    monkeypatch.setenv("FAKE_ENABLE_RC", "1")
    before = snapshot(world["home"])
    with pytest.raises(MachineInstallError, match=r"plugin enable kerbtap@kerbtap.*exited 1"):
        install_machine(world["home"])
    assert snapshot(world["home"]) == before


def test_the_plugin_install_entry_in_settings_survives_the_wiring_removal(world: dict) -> None:
    home = world["home"]
    legacy_home(home)
    _script(world["bin"] / "pwm", "#!/bin/sh\nexit 0\n")
    install_machine(home)
    settings = json.loads((home / ".claude/settings.json").read_text())
    assert settings["enabledPlugins"] == {"kerbtap@kerbtap": True, "kerbtap-research@kerbtap": True}
    commands = [h["command"] for entries in settings["hooks"].values() for e in entries for h in e["hooks"]]
    assert commands == ["python3 ~/my_guard.py"]


def test_the_enable_entry_in_settings_survives_too(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_home(world["home"])
    monkeypatch.setenv("FAKE_MARKETS", json.dumps([KERBTAP_MARKET]))
    monkeypatch.setenv("FAKE_PLUGINS", json.dumps([{"id": "kerbtap@kerbtap", "scope": "user", "enabled": False}]))
    install_machine(world["home"])
    settings = json.loads((world["home"] / ".claude/settings.json").read_text())
    assert settings["enabledPlugins"] == {"kerbtap@kerbtap": True}
    assert machine_hook_command("destructive_git_guard.py") not in json.dumps(settings)


def test_a_marketplace_from_another_source_stops_before_any_change(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy_home(world["home"])
    monkeypatch.setenv("FAKE_MARKETS", json.dumps([{"name": "kerbtap", "source": "github", "repo": "someone/else"}]))
    before = snapshot(world["home"])
    with pytest.raises(MachineInstallError, match=rf"already registered from someone/else, not from {re.escape(DEFAULT_MARKETPLACE)}"):
        install_machine(world["home"])
    assert snapshot(world["home"]) == before
    assert not any(c.startswith(("plugin install", "plugin marketplace add", "plugin enable")) for c in calls(world))


def test_the_same_source_in_another_spelling_is_accepted(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_MARKETS", json.dumps([{"name": "kerbtap", "source": "git",
                                                      "url": f"https://github.com/{DEFAULT_MARKETPLACE}.git"}]))
    result = install_machine(world["home"])
    assert "marketplace kerbtap" in result.already_installed


@pytest.mark.parametrize("query", ["plugin list --json", "plugin marketplace list --json"])
def test_a_failed_query_stops_before_any_change(world: dict, monkeypatch: pytest.MonkeyPatch, query: str) -> None:
    legacy_home(world["home"])
    if query == "plugin list --json":
        monkeypatch.setenv("FAKE_LIST_RC", "1")
        monkeypatch.setenv("FAKE_MARKETS", json.dumps([KERBTAP_MARKET]))
        monkeypatch.setenv("FAKE_PLUGINS", json.dumps([{"id": "kerbtap@kerbtap", "scope": "user", "enabled": False}]))
    else:
        monkeypatch.setenv("FAKE_MARKETS", "not json")
    before = snapshot(world["home"])
    with pytest.raises(MachineInstallError, match=r"Nothing was changed"):
        install_machine(world["home"])
    assert snapshot(world["home"]) == before
    assert not any(c.startswith(("plugin install", "plugin marketplace add", "plugin enable")) for c in calls(world))


SECRET = "s3cr3t-token"
CREDENTIAL_SOURCE = f"https://user:{SECRET}@example.invalid/governor.git"


def test_a_source_credential_is_not_shown(world: dict, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG", logger="kerbtap.machine")
    result = install_machine(world["home"], marketplace=CREDENTIAL_SOURCE)
    assert f"plugin marketplace add {CREDENTIAL_SOURCE}" in calls(world), "the real call keeps the full source"
    assert SECRET not in " ".join(result.commands) and "https://example.invalid/governor.git" in " ".join(result.commands)
    assert SECRET not in caplog.text


def test_a_source_credential_is_not_in_the_error_or_a_dry_run(world: dict, monkeypatch: pytest.MonkeyPatch,
                                                              caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG", logger="kerbtap.machine")
    assert SECRET not in " ".join(install_machine(world["home"], marketplace=CREDENTIAL_SOURCE, dry_run=True).commands)
    monkeypatch.setenv("FAKE_ADD_RC", "1")
    with pytest.raises(MachineInstallError) as failed:
        install_machine(world["home"], marketplace=CREDENTIAL_SOURCE)
    monkeypatch.delenv("FAKE_ADD_RC")
    monkeypatch.setenv("FAKE_MARKETS", json.dumps([{"name": "kerbtap", "source": "git", "url": "https://example.invalid/other.git"}]))
    with pytest.raises(MachineInstallError) as mismatch:
        install_machine(world["home"], marketplace=CREDENTIAL_SOURCE)
    assert SECRET not in str(failed.value) and "cannot add https://example.invalid/governor.git" in str(failed.value)
    assert SECRET not in str(mismatch.value) and "https://example.invalid/governor.git" in str(mismatch.value)
    assert SECRET not in caplog.text


def test_a_failed_settings_write_is_a_clear_error_and_moves_nothing(world: dict) -> None:
    home = world["home"]
    legacy_home(home)
    claude = home / ".claude"
    claude.chmod(0o500)  # the backup file cannot be created beside settings.json
    try:
        with pytest.raises(MachineInstallError, match=r"cannot update .*settings\.json.*already ran: claude plugin") as err:
            install_machine(home)
    finally:
        claude.chmod(0o700)
    assert "legacy files were not moved" in str(err.value)
    assert (claude / "hooks" / "destructive_git_guard.py").is_file()
    assert not list(claude.glob("governor-legacy-*"))


def test_an_empty_home_has_no_wiring_to_remove(world: dict) -> None:
    home = world["home"]
    plan = install_machine(home, dry_run=True)
    assert not plan.wiring_removed and plan.moved == []
    assert not (home / ".claude").exists()

    result = install_machine(home)
    assert not result.wiring_removed and result.settings_backup is None and result.moved == []
    settings = json.loads((home / ".claude/settings.json").read_text())
    assert settings == {"enabledPlugins": {"kerbtap@kerbtap": True}}, "only what the fake claude wrote"
    assert not list((home / ".claude").glob("settings.json.governor-backup-*"))


def test_a_home_with_settings_but_no_governor_wiring_is_left_alone(world: dict) -> None:
    claude = world["home"] / ".claude"
    claude.mkdir()
    text = json.dumps({"model": "opus", "hooks": {"PreToolUse": [USER_ENTRY]}}, indent=4)  # not Governor's formatting
    (claude / "settings.json").write_text(text)
    plan = install_machine(world["home"], dry_run=True)
    assert not plan.wiring_removed
    result = install_machine(world["home"])
    assert not result.wiring_removed and result.settings_backup is None
    settings = json.loads((claude / "settings.json").read_text())
    assert settings["hooks"] == {"PreToolUse": [USER_ENTRY]} and settings["model"] == "opus"
    assert not list(claude.glob("settings.json.governor-backup-*"))


def test_dry_run_changes_nothing(world: dict) -> None:
    home = world["home"]
    legacy_home(home)
    before = snapshot(home)
    result = install_machine(home, dry_run=True)
    assert snapshot(home) == before
    assert calls(world) == []
    assert result.dry_run and result.wiring_removed and sorted(result.moved) == sorted(legacy_files())
    assert "claude plugin install kerbtap@kerbtap --scope user" in result.commands


def test_symlinked_top_level_folders_are_the_users_setup(world: dict, tmp_path: Path) -> None:
    """The owner's machine: ~/.claude/agents and ~/.claude/hooks are links into a config repository."""
    home = world["home"]
    claude = home / ".claude"
    claude.mkdir()
    config = tmp_path / "config"
    (config / "agents" / "docker-expert").mkdir(parents=True)
    (config / "hooks").mkdir()
    (config / "agents" / "worker.md").write_text("legacy worker\n")
    (config / "agents" / "docker-expert" / "SKILL.md").write_text("legacy docker\n")
    (config / "agents" / "mine.md").write_text("the user's own agent\n")
    (config / "hooks" / "destructive_git_guard.py").write_text("legacy hook\n")
    (claude / "agents").symlink_to(config / "agents", target_is_directory=True)
    (claude / "hooks").symlink_to(config / "hooks", target_is_directory=True)

    result = install_machine(home)

    assert sorted(result.moved) == ["agents/docker-expert/SKILL.md", "agents/worker.md", "hooks/destructive_git_guard.py"]
    assert (result.legacy_dir / "agents/worker.md").read_text() == "legacy worker\n"
    assert (result.legacy_dir / "agents/docker-expert/SKILL.md").read_text() == "legacy docker\n"
    assert (claude / "agents").is_symlink() and (claude / "hooks").is_symlink(), "the links stay"
    assert (config / "agents" / "mine.md").read_text() == "the user's own agent\n"
    assert not (config / "agents" / "worker.md").exists() and not (config / "agents" / "docker-expert").exists()


def test_a_symlinked_folder_below_agents_is_refused(world: dict, tmp_path: Path) -> None:
    home = world["home"]
    claude = home / ".claude"
    config = tmp_path / "config"
    (config / "agents").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "SKILL.md").write_text("not under either root\n")
    (config / "agents" / "docker-expert").symlink_to(elsewhere, target_is_directory=True)
    claude.mkdir()
    (claude / "agents").symlink_to(config / "agents", target_is_directory=True)
    before = snapshot(home)
    with pytest.raises(MachineInstallError, match="leads outside"):
        install_machine(home)
    assert snapshot(home) == before and (elsewhere / "SKILL.md").is_file()
    assert calls(world) == []


def test_a_legacy_file_that_links_outside_is_refused(world: dict, tmp_path: Path) -> None:
    home = world["home"]
    claude = home / ".claude"
    (claude / "hooks").mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("# elsewhere\n")
    (claude / "hooks" / "destructive_git_guard.py").symlink_to(outside)
    with pytest.raises(MachineInstallError, match="link to a place outside"):
        install_machine(home)
    assert outside.read_text() == "# elsewhere\n" and (claude / "hooks" / "destructive_git_guard.py").is_symlink()


def test_a_symlinked_claude_folder_is_refused(world: dict, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (world["home"] / ".claude").symlink_to(real, target_is_directory=True)
    with pytest.raises(MachineInstallError, match="symlink"):
        install_machine(world["home"])


def run_cli(world: dict, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": str(world["bin"]), "FAKE_LOG": str(world["log"])}
    return subprocess.run([sys.executable, "-m", "kerbtap", "install-machine", *args],
                          cwd=TOOL_ROOT, capture_output=True, text=True, env=env)


def test_cli_cuts_over_and_reports(world: dict) -> None:
    legacy_home(world["home"])
    proc = run_cli(world, "--home", str(world["home"]))
    assert proc.returncode == 0, proc.stderr
    assert "runs from the kerbtap plugin" in proc.stdout
    assert "removed the old hook wiring" in proc.stdout


def test_cli_dry_run_prints_the_plan(world: dict) -> None:
    legacy_home(world["home"])
    proc = run_cli(world, "--home", str(world["home"]), "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "would run  claude plugin install kerbtap@kerbtap --scope user" in proc.stdout
    assert calls(world) == []


@pytest.mark.parametrize("flag", ["--force", "--source"])
def test_cli_rejects_the_removed_flags(world: dict, flag: str) -> None:
    proc = run_cli(world, "--home", str(world["home"]), flag)
    assert proc.returncode == 2
    assert f"{flag} is gone" in proc.stderr
    assert calls(world) == []


def test_cli_without_claude_exits_2(world: dict) -> None:
    (world["bin"] / "claude").unlink()
    proc = run_cli(world, "--home", str(world["home"]))
    assert proc.returncode == 2
    assert "claude" in proc.stderr


def test_legacy_set_matches_history() -> None:
    require_source(".claude/hooks")
    """CUT-003: the data file equals the set of global/hooks/*.py and global/agents/** paths in any commit."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    shallow = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--is-shallow-repository"],
                             capture_output=True, text=True)
    if shallow.returncode != 0 or shallow.stdout.strip() != "false":
        pytest.skip("history is not complete (not a git checkout, or a shallow clone), so the set cannot be derived")
    out = subprocess.run(
        ["git", "-C", str(REPO), "log", "--name-only", "--format=", "--", "global/hooks", "global/agents"],
        capture_output=True, text=True, check=True,
    ).stdout
    derived = set()
    for line in out.splitlines():
        path = line.strip()
        if path.startswith("global/hooks/") and path.endswith(".py") and path.count("/") == 2:
            derived.add(path[len("global/"):])
        elif path.startswith("global/agents/"):
            derived.add(path[len("global/"):])
    recorded = set(json.loads(LEGACY_FILES_PATH.read_text(encoding="utf-8")))
    assert derived == recorded, (
        f"in history but not in the data file: {sorted(derived - recorded)}; "
        f"in the data file but not in history: {sorted(recorded - derived)}. "
        f"Correct set:\n{json.dumps(sorted(derived), indent=2)}"
    )


def test_the_old_marketplace_stops_before_any_change(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """CUT-007: a marketplace named governor stops the command before the first change."""
    legacy_home(world["home"])
    monkeypatch.setenv("FAKE_MARKETS", json.dumps([{"name": "governor", "source": "github", "repo": DEFAULT_MARKETPLACE}]))
    before = snapshot(world["home"])
    proc = run_cli(world, "--home", str(world["home"]))
    assert proc.returncode == 2
    assert "claude plugin marketplace remove governor" in proc.stderr
    assert proc.stderr.index("marketplace remove governor") < proc.stderr.index("kerbtap install-machine")
    assert "Nothing was changed" in proc.stderr
    assert not any(c.startswith(("plugin marketplace add", "plugin install", "plugin enable")) for c in calls(world))
    assert snapshot(world["home"]) == before, "the legacy wiring and files stay as they were"
