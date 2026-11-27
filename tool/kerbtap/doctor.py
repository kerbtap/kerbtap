"""``kerbtap doctor``: does each project gate fire, as wired?

Each gate is run by the ``command`` in ``.claude/settings.json`` on a violating probe and a
harmless probe, inside a throwaway repository. Nothing is written to the checked repository.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from kerbtap.collect.gate_surface import ACTIVE, parse_surface
from kerbtap.git_hook import GitHookError, status as git_hook_status
from kerbtap.settings_merge import SettingsMergeError, load_settings
from kerbtap.state import state_rel

logger = logging.getLogger("kerbtap.doctor")

GATES = ("precommit_review_guard", "precommit_doc_consistency_guard")
FIRES, SILENT, STUCK, CRASHED, NOT_WIRED = "fires", "silent", "stuck", "crashed", "not wired"
NOT_INSTALLED = "not installed"
TIMEOUT_SECONDS = 30
SHADOW_WARNINGS = ("KERBTAP SHADOW", "GOVERNOR SHADOW")
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


class _ProbeError(Exception):
    """A probe cannot be built or read; the gate is reported ``crashed``."""


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # a hook import must not write __pycache__ into the repo
    env.update(extra)
    return env


def _git(cwd: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False, env=_env()
    )
    if proc.returncode != 0:
        raise _ProbeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")


def _make_probe_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "doctor-probe")
    _git(root, "config", "user.name", "kerbtap doctor")
    _git(root, "config", "user.email", "doctor@invalid")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "commit", "-q", "--allow-empty", "-m", "probe base")


def _stage(root: Path, rel: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("probe\n", encoding="utf-8")
    _git(root, "add", "--", rel)


def _wired_command(repo: Path, gate: str) -> str | None:
    settings = load_settings(repo / ".claude" / "settings.json")
    for entry in settings.get("hooks", {}).get("PreToolUse", []):
        for hook in entry.get("hooks", []):
            command = hook.get("command")
            if isinstance(command, str) and f"{gate}.py" in command:
                return command
    return None


def _hook_set(repo: Path, gate: str, name: str) -> list[str]:
    """The strings of ``NAME = frozenset({...})`` in a gate's hook file, read with ``ast``."""
    try:
        tree = ast.parse((repo / ".claude" / "hooks" / f"{gate}.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                call = node.value
                if isinstance(call, ast.Call) and len(call.args) == 1:
                    value = ast.literal_eval(call.args[0])
                    if value and all(isinstance(v, str) for v in value):
                        return sorted(value)
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError, MemoryError, RecursionError):
        pass
    raise _ProbeError(f"cannot read {name} from {gate}.py")


def _review_probe_path(repo: Path) -> str:
    try:
        source = (repo / ".claude" / "hooks" / "precommit_review_guard.py").read_bytes()
    except OSError as exc:
        raise _ProbeError(f"cannot read precommit_review_guard.py: {exc}") from exc
    surface = parse_surface(source)
    if surface.state != ACTIVE or not surface.prefixes or not surface.suffixes:
        raise _ProbeError("cannot read the review gate surface")
    return f"{surface.prefixes[0]}doctor_probe{surface.suffixes[0]}"


def _violating_path(repo: Path, gate: str) -> str | None:
    if gate == "precommit_review_guard":
        return _review_probe_path(repo)
    if gate == "precommit_doc_consistency_guard":
        return _hook_set(repo, gate, "TRACKED_PATHS")[0]
    return None


def _payloads(repo: Path, gate: str, probe: Path) -> tuple[dict, dict]:
    return (
        {"tool_name": "Bash", "tool_input": {"command": "git commit -m probe"}, "cwd": str(probe)},
        {"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": str(probe)},
    )


def _verdict(command: str, repo: Path, payload: dict, probe: Path) -> str:
    """Run the wired command once: ``"deny"``, ``"shadow"`` (a shadow warning) or ``""``.

    Raises ``_ProbeError`` on a crash.
    """
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", command],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            cwd=probe,
            env=_env(CLAUDE_PROJECT_DIR=str(repo)),
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise _ProbeError(f"could not run: {exc}") from exc
    logger.info("doctor probe: %s %s -> exit %d", command, payload["tool_input"], proc.returncode)
    if proc.returncode != 0:
        raise _ProbeError(f"exit {proc.returncode}: {proc.stderr.strip()[-200:]}")
    out = proc.stdout.strip()
    if not out:
        return ""
    try:
        decoded = json.loads(out)
    except ValueError as exc:
        raise _ProbeError(f"output is not JSON: {out[:200]}") from exc
    if not isinstance(decoded, dict):
        raise _ProbeError(f"output is not a JSON object: {out[:200]}")
    hook_out = decoded.get("hookSpecificOutput")
    if isinstance(hook_out, dict) and hook_out.get("permissionDecision") == "deny":
        return "deny"
    message = decoded.get("systemMessage")
    return "shadow" if isinstance(message, str) and message.startswith(SHADOW_WARNINGS) else ""


def _probe_gate(repo: Path, gate: str, command: str) -> tuple[str, bool]:
    """``(result, shadow)``: ``shadow`` is true when the violating probe gave a shadow warning."""
    try:
        violating, harmless = _payloads(repo, gate, Path("."))
        path = _violating_path(repo, gate)
        with tempfile.TemporaryDirectory(prefix="governor-doctor-") as tmp:
            probe = Path(tmp)
            _make_probe_repo(probe)
            if path is not None:
                _stage(probe, path)
            violating["cwd"] = harmless["cwd"] = str(probe)
            violation = _verdict(command, repo, violating, probe)
            harmless_verdict = _verdict(command, repo, harmless, probe)
    except _ProbeError as exc:
        logger.error("doctor probe of %s crashed: %s", gate, exc)
        return CRASHED, False
    shadow = violation == "shadow"
    if not violation:
        return SILENT, shadow
    if harmless_verdict:
        return STUCK, shadow
    return FIRES, shadow


def _is_shadow(repo: Path, gate: str) -> bool:
    """Read-only twin of the hooks' ``gate_mode``: the main worktree's mode file."""
    try:
        common = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True, timeout=5, env=_env(),
        ).stdout.strip()
        common_path = Path(common) if os.path.isabs(common) else repo / common
        main_root = common_path.resolve().parent
        modes = json.loads(
            (main_root / state_rel(main_root, "gate-mode.json")).read_text(encoding="utf-8")
        )
        return modes.get(gate) == "shadow"
    except (subprocess.SubprocessError, OSError, ValueError, AttributeError):
        return False


def _probe_adapter(repo: Path) -> str:
    adapter = repo / ".claude" / "git-hooks" / "pre_commit.py"
    try:
        with tempfile.TemporaryDirectory(prefix="governor-doctor-") as tmp:
            probe = Path(tmp)
            _make_probe_repo(probe)
            _stage(probe, _review_probe_path(repo))
            proc = subprocess.run(
                ["python3", str(adapter)],
                capture_output=True, text=True, timeout=TIMEOUT_SECONDS, cwd=probe,
                env=_env(), check=False,
            )
    except (_ProbeError, subprocess.SubprocessError, OSError) as exc:
        logger.error("doctor probe of the git adapter crashed: %s", exc)
        return CRASHED
    return {1: FIRES, 0: SILENT}.get(proc.returncode, CRASHED)


def run_doctor(repo: Path) -> tuple[list[str], int]:
    """The report lines and the exit code for ``repo``."""
    try:
        top = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True, timeout=5, env=_env(),
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        logger.error("doctor: %s is not in a git work tree: %s", repo, exc)
        return [f"no git repository at {repo}"], 1
    repo = Path(top).resolve()
    lines: list[str] = []
    broken = False
    for gate in GATES:
        try:
            command = _wired_command(repo, gate)
        except SettingsMergeError as exc:
            logger.error("doctor cannot read settings: %s", exc)
            command = None
        if command is not None:
            result, probed_shadow = _probe_gate(repo, gate, command)
        elif (repo / ".claude" / "hooks" / f"{gate}.py").exists():
            result, probed_shadow = NOT_WIRED, False
        else:
            result, probed_shadow = NOT_INSTALLED, False
        logger.info("doctor: %s -> %s", gate, result)
        shadow = (
            " (shadow)"
            if result != NOT_INSTALLED and (probed_shadow or _is_shadow(repo, gate))
            else ""
        )
        lines.append(f"{gate}: {result}{shadow}")
        broken |= result not in (FIRES, NOT_INSTALLED)
    if all(line.endswith(f": {NOT_INSTALLED}") for line in lines):
        logger.error("doctor: no gate wired and no gate hook file in %s", repo)
        return [f"no kerbtap install found at {repo}"], 1
    try:
        installed, _ = git_hook_status(repo)
    except GitHookError as exc:
        logger.error("doctor cannot read the git hook: %s", exc)
        installed = False
    if installed:
        result = _probe_adapter(repo)
        logger.info("doctor: git adapter -> %s", result)
        lines.append(f"git adapter: {result}")
        broken |= result != FIRES
    else:
        lines.append("git adapter: not installed")
    return lines, 1 if broken else 0
