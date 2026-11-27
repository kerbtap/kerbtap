"""The installed enforcement layer, observed at assessment time.

The pack already records which governance files exist. Existence alone says nothing about
whether a gate runs or whether it is the gate kerbtap ships: a hook runs only when
``.claude/settings.json`` wires it, and a wired hook may have been edited. This module
states, per kerbtap gate, one of:

* ``matched`` — wired with exactly the hook entries kerbtap's installer writes (event,
  matcher, command, type, timeout), and byte-identical to the copy kerbtap's installer
  renders for this repository today;
* ``modified`` — wired exactly as installed, but its content differs from that copy;
* ``unwired`` — present, but no settings entry names it;
* ``absent`` — part of today's install for this repository, and not present;
* ``indeterminate`` — the state cannot be read with certainty (unreadable file or
  settings; wiring that names the file but differs from the installer's entries — another
  command, type or timeout, or duplicate, partial or extra entries; a symlinked file; a
  file today's install would not include; or a failed rendering).

A hook library (``commit_scope.py``) is never wired; it is compared on content alone.

The comparison copy comes from :func:`kerbtap.init.render_hooks` — the installer's own
rendering path — with the bypass prefix read from the repository's own installed review
gate and its surfaces detected as the installer detects them today. Rows cover each
kerbtap gate or library that is present or part of today's install.

This is a file comparison of the working tree at one moment. It is not a statement about
any earlier moment, it does not show the files were never changed, it does not show a gate
ran for any particular change, and it is not the drift detection of the vision's Evidence
Verifier.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections import Counter
from pathlib import Path

from kerbtap.collect.gate_surface import parse_surface
from kerbtap.init import (
    GATE_DOCS,
    InitError,
    derive_prefix,
    detect_surfaces,
    expected_wiring,
    hook_entry_key,
    installed_review_code,
    render_hooks,
)

logger = logging.getLogger(__name__)

MATCHED = "matched"
MODIFIED = "modified"
UNWIRED = "unwired"
ABSENT = "absent"
INDETERMINATE = "indeterminate"

LIBRARIES = ("commit_scope.py",)  # imported by the commit gates; never wired itself
_HOOKS_REL = ".claude/hooks"
_SETTINGS_REL = ".claude/settings.json"
_HOOK_NAME_RE = re.compile(r"""(?:^|[\s"'/=])\.claude/hooks/([A-Za-z0-9_]+\.py)(?![\w.])""")
_EVENTS = ("PreToolUse", "PostToolUse")

SCOPE_NOTE = (
    "A file comparison of the working tree at assessment time. `matched` means the gate was wired "
    "with exactly the hook entries kerbtap's installer writes and was byte-identical to the copy "
    "kerbtap's installer renders for this repository today, using the repository's own installed "
    "bypass name; a hook library is compared on content alone. It is not a statement about any "
    "earlier moment, it does not show the files were never changed, it does not show a gate "
    "ran for any particular change, and it does not show a gate is enabled: settings outside the "
    "tracked `.claude/settings.json` are not read."
)


def _git_out(cwd: Path, *args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def actual_wiring(settings: bytes) -> dict[str, Counter] | None:
    """``{hook file: Counter(hook_entry_key)}`` for every hook entry naming the file.

    ``None`` when the settings file cannot be read with certainty.
    """
    try:
        data = json.loads(settings.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return None
    wiring: dict[str, Counter] = {}
    for event in _EVENTS:
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks", []), list):
                return None
            matcher = entry.get("matcher", "")
            if not isinstance(matcher, str):
                return None
            for hook in entry.get("hooks", []):
                command = hook.get("command") if isinstance(hook, dict) else None
                if not isinstance(command, str):
                    continue
                for name in set(_HOOK_NAME_RE.findall(command)):
                    wiring.setdefault(name, Counter())[hook_entry_key(event, matcher, hook)] += 1
    return wiring


def _source_dirty(source_repo: Path) -> bool | None:
    """Whether the files the comparison copy is rendered from differ from the source checkout's HEAD.

    That is the two hook trees and the installer that rewrites them.
    """
    out = _git_out(
        source_repo, "status", "--porcelain", "--", "starter/.claude/hooks", ".claude/hooks", "tool/kerbtap/init.py"
    )
    return None if out is None else bool(out)


def _prefix(repo: Path) -> str:
    """The bypass prefix this repository was installed with, else the installer's default."""
    try:
        source = (repo / _HOOKS_REL / "precommit_review_guard.py").read_bytes()
    except OSError:
        source = b""
    bypass = parse_surface(source).bypass_env if source else None
    if bypass and bypass.endswith("_REVIEWED") and len(bypass) > len("_REVIEWED"):
        return bypass[: -len("_REVIEWED")]
    return derive_prefix(repo)


def _row(name: str, kind: str, wiring: Counter, state: str, detail: str) -> dict[str, object]:
    return {
        "path": f"{_HOOKS_REL}/{name}",
        "kind": kind,
        "wiring": sorted({f"{event} / {matcher}" for event, matcher, _hook in wiring}),
        "state": state,
        "detail": detail,
    }


def observe(repo: Path, source_repo: Path, version: str) -> dict[str, object]:
    """The enforcement-layer record for ``repo``, compared with ``source_repo``'s installer."""
    repo = repo.resolve()
    hooks_dir = repo / _HOOKS_REL
    settings_path = repo / _SETTINGS_REL
    record: dict[str, object] = {
        "governor_version": version,
        "governor_commit": _git_out(source_repo, "rev-parse", "HEAD") or "unknown",
        # The comparison renders from the source checkout's working tree, not its HEAD.
        "governor_uncommitted_changes": _source_dirty(source_repo),
        "assessed_commit": _git_out(repo, "rev-parse", "--verify", "-q", "HEAD") or "none",
        "claude_dir_uncommitted_changes": bool(_git_out(repo, "status", "--porcelain", "--", ".claude")),
        "scope_note": SCOPE_NOTE,
        "gates": [],
    }
    if not hooks_dir.is_dir() and not settings_path.exists():
        record["installed"] = False
        return record
    record["installed"] = True

    wiring: dict[str, Counter] | None
    if settings_path.is_file() and not settings_path.is_symlink():
        try:
            wiring = actual_wiring(settings_path.read_bytes())
        except OSError:
            wiring = None
    elif settings_path.exists():
        wiring = None  # a directory or a symlink: not something to trust
    else:
        wiring = {}

    try:
        prefix = _prefix(repo)
        rendered = render_hooks(source_repo, repo.name, prefix, detect_surfaces(repo, review_code=installed_review_code(repo)))
        expected = expected_wiring(prefix, rendered.gates)
        render_error = ""
    except (InitError, OSError, UnicodeDecodeError, ValueError, KeyError) as exc:
        logger.warning("Could not render kerbtap's install for comparison: %s", exc)
        rendered, expected, render_error = None, {}, str(exc)

    rendered_names = set(rendered.gates) if rendered else set()
    names = sorted(rendered_names | set(GATE_DOCS) | set(LIBRARIES))
    rows: list[dict[str, object]] = []
    for name in names:
        kind = "library" if name in LIBRARIES else "gate"
        want = expected.get(name, Counter())
        path = hooks_dir / name
        if not path.exists() and not path.is_symlink():
            if name in rendered_names:
                rows.append(_row(name, kind, want, ABSENT, "Part of today's install for this repository; not present."))
            continue
        if render_error:
            rows.append(_row(name, kind, want, INDETERMINATE, "kerbtap's install could not be rendered for comparison."))
            continue
        if name not in rendered_names:
            rows.append(_row(name, kind, want, INDETERMINATE,
                             "Present, but not part of the install kerbtap renders for this repository today."))
            continue
        if path.is_symlink() or not path.is_file():
            rows.append(_row(name, kind, want, INDETERMINATE, "Present, but not a regular file."))
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            rows.append(_row(name, kind, want, INDETERMINATE, "Present, but unreadable."))
            continue
        same = content == rendered.files[name]
        if kind == "library":
            state = MATCHED if same else MODIFIED
            detail = ("Byte-identical to today's rendered copy." if same
                      else "Differs from today's rendered copy.")
            rows.append(_row(name, kind, want, state, detail))
            continue
        if wiring is None:
            rows.append(_row(name, kind, want, INDETERMINATE, "The settings file could not be read."))
            continue
        seen = wiring.get(name, Counter())
        if not seen:
            rows.append(_row(name, kind, want, UNWIRED, "Present; no settings entry names it."))
            continue
        if seen != want:
            rows.append(_row(name, kind, want, INDETERMINATE,
                             "A settings entry names it, but not exactly as the installer wires it "
                             "(another command, type or timeout, or duplicate, partial or extra entries)."))
            continue
        if same:
            rows.append(_row(name, kind, want, MATCHED, "Wired as installed; byte-identical to today's rendered copy."))
        else:
            rows.append(_row(name, kind, want, MODIFIED, "Wired as installed; content differs from today's rendered copy."))
    record["gates"] = rows
    logger.info(
        "Enforcement layer: %s",
        ", ".join(f"{r['path'].rsplit('/', 1)[-1]}={r['state']}" for r in rows) or "no kerbtap gates",
    )
    return record
