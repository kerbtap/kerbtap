"""Whether the review gate was active at each commit, and what it guarded — from that commit's tree.

The Evidence Pack has to say, per change, whether the pre-commit review gate applied. That
depends on facts about the commit itself: which paths it changed, whether the gate was
installed *and wired* at that commit, and which surface it guarded then. ``kerbtap init``
rewrites the surface and the bypass name per repository (``TRACKED_PATH_PREFIXES``,
``TRACKED_SUFFIXES``, ``BYPASS_ENV``), and the configuration moves over a repository's life,
so today's checkout is not evidence about last year's commit.

A hook file on disk does nothing by itself: Claude Code runs it only when
``.claude/settings.json`` wires it as a ``PreToolUse`` hook for ``Bash``. Both files are read
from the commit's tree.

The hook is parsed with :mod:`ast` and its constants read with :func:`ast.literal_eval`. It is
never imported or executed: it is repository content, and the pack runs against repositories
it does not trust.

States per commit:

* ``absent`` — no hook file in the commit's tree: the gate was not installed there.
* ``unwired`` — the hook file exists but the tracked settings do not wire it (wiring kept
  outside the repository, such as a user's own settings, is not visible to the pack).
* ``active`` — wired, and every surface constant is a literal.
* ``unreadable`` — the hook or the settings exist but cannot be read with certainty (not
  UTF-8, not valid Python or JSON, a constant missing or computed, a matcher that is not a
  valid pattern, not a blob, or a failed git call). Reported as ``undetermined``, never
  guessed either way.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from kerbtap.review.trailer import DEFAULT_OVERRIDE_NAMES

logger = logging.getLogger(__name__)

HOOK_PATH = ".claude/hooks/precommit_review_guard.py"
SETTINGS_PATH = ".claude/settings.json"
# The hook named in a command, absolute (`$CLAUDE_PROJECT_DIR/.claude/...`) or relative
# (`.claude/...`, run from the project directory) — but not `x.claude/...`.
_HOOK_REF_RE = re.compile(r"""(?:^|[\s"'/=])\.claude/hooks/precommit_review_guard\.py(?![\w.])""")

ACTIVE = "active"
ABSENT = "absent"
UNWIRED = "unwired"
UNREADABLE = "unreadable"

# Larger files than this are not hand-written configuration; refuse to parse them.
_MAX_BYTES = 1 << 20
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_NON_LITERAL = object()


@dataclass(frozen=True, slots=True)
class GateSurface:
    """The review gate's state and configuration as recorded in one commit's tree."""

    state: str  # ACTIVE | ABSENT | UNWIRED | UNREADABLE
    prefixes: tuple[str, ...] = ()
    suffixes: tuple[str, ...] = ()
    bypass_env: str | None = None

    def guards(self, path: str) -> bool:
        """Whether the gate, as configured, would have matched ``path``.

        Mirrors the hooks' ``_on_surface`` (SURF-001): suffix match AND a prefix match. A
        non-empty prefix matches by ``startswith``; the member ``""`` matches every path
        that does not start with ``.``. Only meaningful for an ``active`` surface.
        """
        if not path.endswith(self.suffixes):
            return False
        if "" in self.prefixes and not path.startswith("."):
            return True
        return any(p and path.startswith(p) for p in self.prefixes)

    @property
    def override_names(self) -> tuple[str, ...]:
        """Every bypass-variable name that marks an explicit override at this commit.

        The pre-rename default is always included, so a repository's history from before
        its install renamed the variable still classifies its overrides.
        """
        if self.bypass_env and self.bypass_env not in DEFAULT_OVERRIDE_NAMES:
            return (*DEFAULT_OVERRIDE_NAMES, self.bypass_env)
        return DEFAULT_OVERRIDE_NAMES


ABSENT_SURFACE = GateSurface(ABSENT)
UNWIRED_SURFACE = GateSurface(UNWIRED)
UNREADABLE_SURFACE = GateSurface(UNREADABLE)


# --- hook file ---------------------------------------------------------------------


def _literal_assignments(tree: ast.Module) -> dict[str, object]:
    """Top-level ``NAME = <literal>`` bindings; a non-literal binding records a sentinel.

    Later bindings win, as they would at import. A computed value is recorded as the
    sentinel so it cannot silently fall back to an earlier literal.
    """
    wanted = {"TRACKED_PATH_PREFIXES", "TRACKED_SUFFIXES", "TRACKED_SUFFIX", "BYPASS_ENV"}
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        names = [t.id for t in targets if isinstance(t, ast.Name) and t.id in wanted]
        if not names:
            continue
        try:
            literal = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            literal = _NON_LITERAL
        for name in names:
            found[name] = literal
    return found


def _str_tuple(value: object, *, allow_empty_member: bool = False) -> tuple[str, ...] | None:
    if isinstance(value, (tuple, list)) and all(
        isinstance(v, str) and (v or allow_empty_member) for v in value
    ):
        return tuple(value)
    return None


def parse_surface(source: bytes) -> GateSurface:
    """Read the guarded surface out of a hook file's bytes without executing them.

    Returns an ``active`` surface when every constant is a literal (the caller decides
    whether the hook is wired), or ``unreadable``.
    """
    if len(source) > _MAX_BYTES:
        return UNREADABLE_SURFACE
    try:
        found = _literal_assignments(ast.parse(source.decode("utf-8")))
    except (UnicodeDecodeError, SyntaxError, ValueError, MemoryError, RecursionError):
        return UNREADABLE_SURFACE

    # A bare string would make the hook iterate characters — not a surface anyone meant.
    # A prefix may be "" (every path that does not start with a dot); a suffix may not.
    prefixes = _str_tuple(found.get("TRACKED_PATH_PREFIXES"), allow_empty_member=True)
    if "TRACKED_SUFFIXES" in found:
        raw_suffixes = found["TRACKED_SUFFIXES"]
    else:
        raw_suffixes = found.get("TRACKED_SUFFIX")  # the gate's earliest spelling
    if isinstance(raw_suffixes, str) and raw_suffixes:
        suffixes: tuple[str, ...] | None = (raw_suffixes,)
    else:
        suffixes = _str_tuple(raw_suffixes)
    if prefixes is None or suffixes is None:
        return UNREADABLE_SURFACE

    bypass = found.get("BYPASS_ENV")
    bypass_env = bypass if isinstance(bypass, str) and _ENV_NAME_RE.match(bypass) else None
    return GateSurface(ACTIVE, prefixes=prefixes, suffixes=suffixes, bypass_env=bypass_env)


# --- settings wiring ---------------------------------------------------------------


def _matches_bash(matcher: object) -> bool | None:
    """Whether a ``PreToolUse`` matcher selects the ``Bash`` tool; ``None`` if unknowable."""
    if matcher is None or matcher in ("", "*"):
        return True
    if not isinstance(matcher, str):
        return None
    try:
        return re.fullmatch(matcher, "Bash") is not None
    except re.error:
        return None


def parse_wiring(settings: bytes) -> bool | None:
    """Whether a settings file wires the review gate as a ``PreToolUse`` hook for ``Bash``.

    ``True`` wired, ``False`` not wired, ``None`` when the file cannot be read with
    certainty (malformed JSON, a wrong shape, or an invalid matcher on an entry that names
    the gate).
    """
    if len(settings) > _MAX_BYTES:
        return None
    try:
        data = json.loads(settings.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return None
    pre = hooks.get("PreToolUse", [])
    if not isinstance(pre, list):
        return None
    uncertain = False
    for entry in pre:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks", []), list):
            return None
        names_gate = any(
            isinstance(h, dict) and isinstance(h.get("command"), str) and _HOOK_REF_RE.search(h["command"])
            for h in entry.get("hooks", [])
        )
        if not names_gate:
            continue
        selects = _matches_bash(entry.get("matcher"))
        if selects:
            return True
        if selects is None:
            uncertain = True
    return None if uncertain else False


def combine(surface: GateSurface | None, wired: bool | None) -> GateSurface:
    """One commit's state from its parsed hook (``None`` = no hook file) and wiring."""
    if surface is None:
        return ABSENT_SURFACE
    if wired is None:
        return UNREADABLE_SURFACE
    if wired is False:
        return UNWIRED_SURFACE
    return surface


# --- batch read from git -----------------------------------------------------------


def _git_batch(repo_path: Path, args: list[str], stdin: bytes) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            input=stdin,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("git %s failed: %s", " ".join(args), exc)
        return None
    return result.stdout


def _read_blobs(repo_path: Path, oids: list[str]) -> dict[str, bytes] | None:
    """Contents of ``oids`` via one ``git cat-file --batch`` process."""
    if not oids:
        return {}
    out = _git_batch(repo_path, ["cat-file", "--batch"], "".join(f"{o}\n" for o in oids).encode())
    if out is None:
        return None
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in oids:
        header_end = out.find(b"\n", pos)
        if header_end < 0:
            return None
        header = out[pos:header_end].decode("ascii", "replace").split()
        if len(header) != 3 or header[0] != oid or header[1] != "blob" or not header[2].isdigit():
            return None
        size = int(header[2])
        start = header_end + 1
        blobs[oid] = out[start:start + size]
        pos = start + size + 1  # content is followed by one newline
    return blobs


def _blob_ids(repo_path: Path, shas: list[str], path: str) -> list[str | None] | None:
    """Per commit: the blob id of ``path`` in its tree, ``None`` if absent, ``""`` if not a blob."""
    out = _git_batch(repo_path, ["cat-file", "--batch-check"], "".join(f"{s}:{path}\n" for s in shas).encode())
    if out is None:
        return None
    lines = out.decode("utf-8", "replace").splitlines()
    if len(lines) != len(shas):
        logger.warning("cat-file --batch-check returned %d lines for %d commits", len(lines), len(shas))
        return None
    ids: list[str | None] = []
    for line in lines:
        if line.endswith(" missing"):
            ids.append(None)
            continue
        parts = line.split()
        ids.append(parts[0] if len(parts) == 3 and parts[1] == "blob" else "")
    return ids


def surfaces_by_commit(repo_path: Path, shas: list[str]) -> dict[str, GateSurface]:
    """The review gate's state at each commit in ``shas``, in three git processes total.

    Every commit whose state cannot be established — including a failed git call — is
    ``unreadable``, never ``absent``: a missing reading must not turn into an exemption.
    """
    if not shas:
        return {}
    unknown = {sha: UNREADABLE_SURFACE for sha in shas}
    hook_ids = _blob_ids(repo_path, shas, HOOK_PATH)
    settings_ids = _blob_ids(repo_path, shas, SETTINGS_PATH)
    if hook_ids is None or settings_ids is None:
        return unknown

    oids = sorted({oid for oid in (*hook_ids, *settings_ids) if oid})
    blobs = _read_blobs(repo_path, oids)
    if blobs is None:
        return unknown
    hook_cache: dict[str, GateSurface] = {}
    wiring_cache: dict[str, bool | None] = {}

    result: dict[str, GateSurface] = {}
    for sha, hook_id, settings_id in zip(shas, hook_ids, settings_ids):
        if hook_id is None:
            result[sha] = ABSENT_SURFACE
            continue
        if not hook_id:
            result[sha] = UNREADABLE_SURFACE
            continue
        if hook_id not in hook_cache:
            hook_cache[hook_id] = parse_surface(blobs[hook_id])
        if settings_id is None:
            wired: bool | None = False
        elif not settings_id:
            wired = None
        else:
            if settings_id not in wiring_cache:
                wiring_cache[settings_id] = parse_wiring(blobs[settings_id])
            wired = wiring_cache[settings_id]
        result[sha] = combine(hook_cache[hook_id], wired)
    logger.info(
        "Review-gate state read for %d commits from %d hook and %d settings version(s)",
        len(shas), len(hook_cache), len(wiring_cache),
    )
    return result
