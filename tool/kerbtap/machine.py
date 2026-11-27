"""``kerbtap install-machine`` — move a machine to the ``kerbtap`` plugin.

The machine tier is the ``kerbtap`` plugin (and ``kerbtap-research`` where ``pwm`` is installed). A machine
that still holds the copy-based install of earlier releases would run every hook twice, so this command is the
cutover and nothing else:

1. Preflight: ``claude`` is on ``PATH``, the hook interpreter is new enough, ``~/.claude`` and the legacy files
   are not reached through a symlink, and ``~/.claude/settings.json`` can be merged. Nothing changes on failure.
2. Install the plugin from the marketplace at user scope (``claude plugin marketplace add``,
   ``claude plugin install``). A failed command stops the cutover; the legacy install is still intact.
3. Remove every kerbtap-owned entry from ``~/.claude/settings.json`` (the previous file is kept beside it),
   by :mod:`kerbtap.settings_merge`. A settings entry that runs a kerbtap hook with a command kerbtap did not
   write stops the cutover in step 1.
4. Move each legacy file (``legacy_machine_files.json``: what any earlier release copied into ``~/.claude/``) to
   ``~/.claude/governor-legacy-<UTC time>/``. Files are moved, never deleted, and a file outside that set is not
   touched, so a user edit survives.

A second run changes nothing. ``--dry-run`` prints the plan and runs no ``claude`` command. Nothing is written
outside ``<home>/.claude/`` except by ``claude`` itself, which runs with ``HOME`` set to ``<home>``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from kerbtap.init import artifact_root
from kerbtap.hook_python import hook_python_problem
from kerbtap.settings_merge import SettingsMergeError, load_settings, merge_hooks, write_settings

logger = logging.getLogger("kerbtap.machine")

DEFAULT_MARKETPLACE = "kerbtap/kerbtap"
MARKETPLACE_NAME = "kerbtap"
OLD_MARKETPLACE_NAME = "governor"
PLUGIN = "kerbtap@kerbtap"
RESEARCH_PLUGIN = "kerbtap-research@kerbtap"
LEGACY_FILES_PATH = Path(__file__).resolve().parent / "data" / "legacy_machine_files.json"
CLAUDE_TIMEOUT_SECONDS = 600


class MachineInstallError(Exception):
    """The cutover could not be done; the message names the step. Earlier steps are listed in it."""


@dataclass
class MachineResult:
    home: Path
    dry_run: bool = False
    commands: list[str] = field(default_factory=list)  # plugin commands run (planned, in a dry run)
    already_installed: list[str] = field(default_factory=list)  # plugin steps skipped because they were done
    wiring_removed: bool = False  # settings.json held kerbtap entries (would hold, in a dry run)
    settings_backup: Path | None = None
    legacy_dir: Path | None = None
    moved: list[str] = field(default_factory=list)  # legacy files moved (to move, in a dry run)


def machine_hook_command(filename: str) -> str:
    """The exact command the copy-based install wrote for a machine hook — the ownership key for the merge."""
    return f'python3 "$HOME/.claude/hooks/{filename}"'


def legacy_files() -> list[str]:
    """Paths relative to ``~/.claude/`` that an earlier release copied there."""
    try:
        data = json.loads(LEGACY_FILES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MachineInstallError(f"cannot read the legacy file list {LEGACY_FILES_PATH}: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(p, str) for p in data):
        raise MachineInstallError(f"{LEGACY_FILES_PATH} must be a JSON list of paths")
    return data


def _legacy_hook_names(legacy: list[str]) -> set[str]:
    return {
        rel[len("hooks/"):] for rel in legacy
        if rel.startswith("hooks/") and rel.count("/") == 1 and rel.endswith(".py")
        and not rel.rsplit("/", 1)[1].startswith("test_")
    }


def _claude(claude: str, args: list[str], home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run([claude, *args], capture_output=True, text=True, env=env,
                          timeout=CLAUDE_TIMEOUT_SECONDS, check=False)


_URL_CREDENTIALS = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")


def _redact(text: str) -> str:
    """``text`` without the user and password part of any URL in it (a marketplace source may carry a token)."""
    return _URL_CREDENTIALS.sub(r"\1", text)


def _json_list(claude: str, args: list[str], home: Path) -> list:
    """The JSON list a ``claude ... --json`` query prints. A failed query or output that is no list stops the cutover."""
    shown = "claude " + " ".join(args)
    try:
        proc = _claude(claude, args, home)
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.error("`%s` failed: %s", shown, exc)
        raise MachineInstallError(f"`{shown}` failed ({type(exc).__name__}). Nothing was changed.") from exc
    if not isinstance(data, list):
        logger.error("`%s` exited %s and printed no JSON list", shown, proc.returncode)
        raise MachineInstallError(f"`{shown}` exited {proc.returncode} or printed no JSON list. Nothing was changed.")
    return data


def _normal_source(source: str) -> str:
    """A marketplace source in one spelling: ``owner/repo`` for a GitHub repository, else the text without a ``.git`` end."""
    text = _redact(source).strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text[:-4] if text.endswith(".git") else text


def _check_marketplace_source(marketplaces: list, requested: str) -> bool:
    """Is a marketplace named ``kerbtap`` already registered? Stops when its source differs from ``requested``."""
    entry = next((m for m in marketplaces if isinstance(m, dict) and m.get("name") == MARKETPLACE_NAME), None)
    if entry is None:
        return False
    registered = [entry.get(k) for k in ("repo", "url", "path") if isinstance(entry.get(k), str)]
    if _normal_source(requested) in {_normal_source(r) for r in registered}:
        return True
    shown = _redact(", ".join(registered)) if registered else "unknown"
    raise MachineInstallError(
        f"a marketplace named {MARKETPLACE_NAME} is already registered from {shown}, not from {_redact(requested)}. "
        "Remove it with `claude plugin marketplace remove kerbtap` or pass its source with --marketplace. "
        "Nothing was changed."
    )


def _holds_wiring(settings: dict, owned_commands: set[str]) -> bool:
    """Does ``settings`` hold a hook whose command is exactly one kerbtap wrote? (merge_hooks adds an empty
    ``hooks`` object to any settings, so comparing rendered text would report wiring on a home that has none.)"""
    return any(
        isinstance(hook, dict) and hook.get("command") in owned_commands
        for entries in (settings.get("hooks") or {}).values()
        for entry in entries
        for hook in entry["hooks"]
    )


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _present_legacy(target: Path, legacy: list[str]) -> list[str]:
    """The legacy files that exist under ``target``.

    A file must stay inside ``target``, or inside the folder that a top-level ``hooks`` or ``agents`` link points
    to (the user's own setup). A symlinked folder below those, or a file that is a link to a place outside the
    allowed folders, is refused: the move would take a file from somewhere kerbtap never wrote to.
    """
    roots = [target.resolve()]
    for top in ("hooks", "agents"):
        link = target / top
        if link.is_symlink() and link.is_dir():
            roots.append(link.resolve())
    present = []
    for rel in legacy:
        path = target / rel
        if not (path.exists() or path.is_symlink()):
            continue
        if not _inside(path.parent.resolve(), roots):
            raise MachineInstallError(f"{path.parent} leads outside {target}; refusing to move files out of it")
        if path.is_symlink() and not _inside(path.resolve(), roots):
            raise MachineInstallError(f"{path} is a link to a place outside {target}; refusing to move it")
        present.append(rel)
    return present


def _run_plugin_command(claude: str, args: list[str], home: Path, result: MachineResult) -> None:
    shown = _redact("claude " + " ".join(args))
    result.commands.append(shown)
    try:
        proc = _claude(claude, args, home)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MachineInstallError(
            f"`{shown}` could not run: {_redact(str(exc))}. The legacy install is unchanged."
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise MachineInstallError(
            f"`{shown}` exited {proc.returncode}: {_redact(detail[-1]) if detail else 'no output'}. "
            "The legacy install is unchanged."
        )
    logger.info("Ran %s", shown)


def _move_legacy(target: Path, present: list[str], result: MachineResult) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    legacy_dir = target / f"governor-legacy-{stamp}"
    for rel in present:
        dest = legacy_dir / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target / rel), str(dest))
        except OSError as exc:
            raise MachineInstallError(
                f"cannot move {target / rel} to {dest}: {exc}. Moved so far, now in {legacy_dir}: "
                f"{', '.join(result.moved) or 'nothing'}"
            ) from exc
        result.moved.append(rel)
        result.legacy_dir = legacy_dir
        top = target / rel.split("/")[0]
        parent = (target / rel).parent
        while parent != top:  # drop the subfolders that the move emptied; rmdir only succeeds on an empty one
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    logger.info("Moved %d legacy file(s) to %s", len(result.moved), legacy_dir)


def default_marketplace() -> str:
    """The marketplace bundled in the wheel when there is one, else ``DEFAULT_MARKETPLACE``."""
    bundled = artifact_root(Path(__file__).resolve().parent)
    if bundled is not None and (bundled / ".claude-plugin" / "marketplace.json").is_file():
        return str(bundled)
    return DEFAULT_MARKETPLACE


def install_machine(home: Path, marketplace: str | None = None, dry_run: bool = False) -> MachineResult:
    """Cut the machine over to the plugin; see the module docstring."""
    marketplace = marketplace or default_marketplace()
    home = home.expanduser()
    problem = hook_python_problem()
    if problem:
        raise MachineInstallError(problem)
    claude = shutil.which("claude")
    if claude is None:
        raise MachineInstallError(
            "the `claude` command is not on PATH. The machine tier is the `kerbtap` plugin, and `claude` installs it."
        )
    target = home / ".claude"
    if target.is_symlink():
        raise MachineInstallError(f"{target} is a symlink; refusing to write through it")

    legacy = legacy_files()
    hook_names = _legacy_hook_names(legacy)
    settings_path = target / "settings.json"
    try:
        existing = load_settings(settings_path)
        cleaned = merge_hooks(
            existing, {}, owned_commands={machine_hook_command(n) for n in hook_names}, owned_files=hook_names
        )
    except SettingsMergeError as exc:
        raise MachineInstallError(f"cannot merge {settings_path}: {exc}") from exc
    present = _present_legacy(target, legacy) if target.exists() else []

    result = MachineResult(home=home, dry_run=dry_run)
    owned = {machine_hook_command(n) for n in hook_names}
    result.wiring_removed = _holds_wiring(existing, owned)
    plugins = [PLUGIN] + ([RESEARCH_PLUGIN] if shutil.which("pwm") else [])
    if dry_run:
        result.commands = [_redact(f"claude plugin marketplace add {marketplace}")] + [
            f"claude plugin install {p} --scope user" for p in plugins
        ]
        result.moved = present
        return result

    # Both queries, and the source check, come before the first change: a failure here changes nothing.
    marketplaces = _json_list(claude, ["plugin", "marketplace", "list", "--json"], home)
    if any(isinstance(m, dict) and m.get("name") == OLD_MARKETPLACE_NAME for m in marketplaces):
        logger.error("a marketplace named %s is registered; stopping before any change", OLD_MARKETPLACE_NAME)
        raise MachineInstallError(
            f"a marketplace named {OLD_MARKETPLACE_NAME} is registered from the earlier release. "
            f"Run `claude plugin marketplace remove {OLD_MARKETPLACE_NAME}`, then `kerbtap install-machine`. "
            "Nothing was changed."
        )
    registered = _check_marketplace_source(marketplaces, marketplace)
    enabled = {
        p.get("id"): bool(p.get("enabled")) for p in _json_list(claude, ["plugin", "list", "--json"], home)
        if isinstance(p, dict) and p.get("scope") == "user"
    }

    if registered:
        result.already_installed.append(f"marketplace {MARKETPLACE_NAME}")
    else:
        _run_plugin_command(claude, ["plugin", "marketplace", "add", marketplace], home, result)
    for plugin in plugins:
        if plugin not in enabled:
            _run_plugin_command(claude, ["plugin", "install", plugin, "--scope", "user"], home, result)
        elif not enabled[plugin]:
            # Installed but disabled: removing the legacy hooks now would leave no active hooks.
            _run_plugin_command(claude, ["plugin", "enable", plugin, "--scope", "user"], home, result)
        else:
            result.already_installed.append(plugin)

    ran = "; ".join(result.commands) or "none"
    try:
        # Read the file again: `claude plugin install` wrote `enabledPlugins` into it after the preflight read.
        fresh = load_settings(settings_path)
        final = merge_hooks(
            fresh, {}, owned_commands={machine_hook_command(n) for n in hook_names}, owned_files=hook_names
        )
        result.wiring_removed = _holds_wiring(fresh, owned)
        if result.wiring_removed:
            result.settings_backup = write_settings(settings_path, final)
    except (SettingsMergeError, OSError) as exc:
        raise MachineInstallError(
            f"cannot update {settings_path} (step: remove the old hook wiring): {exc}. "
            f"The plugin commands that already ran: {ran}. The legacy files were not moved."
        ) from exc
    if present:
        _move_legacy(target, present, result)
    return result
