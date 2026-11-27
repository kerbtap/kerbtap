"""Merge kerbtap's hook wiring into an existing Claude Code settings file.

Installing over a repository or a machine that already has ``settings.json`` used to
replace the file, losing whatever the user had wired. The merge instead:

* removes every hook entry kerbtap **owns** — one whose ``command`` is, byte for byte, a
  command kerbtap writes for one of its hook files (including gates a given install
  drops, so a stale entry does not linger);
* **refuses** when any other entry still names one of kerbtap's hook files — a command
  kerbtap did not write runs a kerbtap gate, and silently adding a second wiring beside
  it would run the gate twice or hide the user's variant;
* adds the rendered entries, joining an existing matcher group where one matches;
* keeps every other entry, event and top-level key as it was (a key kerbtap renders is
  added only when the file lacks it).

The result is deterministic, and merging it again changes nothing. When the file's content
changes, the previous file is kept beside it as ``settings.json.governor-backup-<UTC time>``.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_HOOK_REF_RE = re.compile(r"""(?:^|[\s"'/=])\.claude/hooks/([A-Za-z0-9_]+\.py)(?![\w.])""")


class SettingsMergeError(Exception):
    """The settings file cannot be merged safely; nothing was written."""


def load_settings(path: Path) -> dict:
    """The settings object at ``path`` (``{}`` when absent), validated for merging."""
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise SettingsMergeError(f"{path} is not a regular file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SettingsMergeError(f"{path} is not readable JSON: {exc}") from exc
    _validate(data, str(path))
    return data


def _validate(data: object, where: str) -> None:
    if not isinstance(data, dict):
        raise SettingsMergeError(f"{where}: the settings file must be a JSON object")
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsMergeError(f"{where}: `hooks` must be an object")
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            raise SettingsMergeError(f"{where}: `hooks.{event}` must be a list")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                raise SettingsMergeError(f"{where}: every `hooks.{event}` entry needs a `hooks` list")
            if not all(isinstance(hook, dict) for hook in entry["hooks"]):
                raise SettingsMergeError(f"{where}: every hook in `hooks.{event}` must be an object")


def merge_hooks(existing: dict, rendered: dict, owned_commands: set[str], owned_files: set[str]) -> dict:
    """``existing`` with kerbtap's entries replaced by ``rendered``'s; see the module docstring."""
    _validate(existing, "existing settings")
    _validate(rendered, "rendered settings")
    result = copy.deepcopy(existing)
    hooks: dict = result.setdefault("hooks", {})

    foreign: list[str] = []
    for event, entries in hooks.items():
        kept_entries = []
        for entry in entries:
            before = entry["hooks"]
            after = [h for h in before if h.get("command") not in owned_commands]
            for hook in after:
                command = hook.get("command")
                if isinstance(command, str) and set(_HOOK_REF_RE.findall(command)) & owned_files:
                    foreign.append(f"{event}: {command}")
            if before and not after:
                continue  # an entry that only held kerbtap's hooks goes with them
            entry["hooks"] = after
            kept_entries.append(entry)
        hooks[event] = kept_entries
    if foreign:
        listing = "\n  ".join(foreign)
        raise SettingsMergeError(
            "these settings entries run a kerbtap hook with a command kerbtap does not write; "
            f"remove or rewrite them by hand, then re-run:\n  {listing}"
        )

    for event, entries in rendered.get("hooks", {}).items():
        target = hooks.setdefault(event, [])
        for entry in entries:
            matcher = entry.get("matcher")
            group = next((e for e in target if e.get("matcher") == matcher), None)
            if group is None:
                target.append(copy.deepcopy(entry))
            else:
                group["hooks"].extend(copy.deepcopy(entry["hooks"]))

    for key, value in rendered.items():
        if key != "hooks" and key not in result:
            result[key] = copy.deepcopy(value)
    return result


def render_text(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_settings(path: Path, data: dict) -> Path | None:
    """Write ``data`` to ``path``; keep the previous file as a backup when it changes.

    Returns the backup path, or ``None`` when nothing needed backing up.
    """
    text = render_text(data)
    backup = None
    if path.exists():
        old = path.read_bytes()
        if old == text.encode("utf-8"):
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.name}.governor-backup-{stamp}")
        backup.write_bytes(old)
        logger.info("Previous %s kept at %s", path.name, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return backup
