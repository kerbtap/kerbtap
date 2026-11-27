#!/usr/bin/env python3
"""Integration test for precommit_doc_consistency_guard.py.

Drives the actual hook as a subprocess, exactly as the harness invokes it: a JSON
payload on stdin, ``CLAUDE_PROJECT_DIR`` in the environment, and the verdict (a
``KERBTAP SHADOW`` warning, or silence) read back from stdout. The doc gate is always
shadow: it fires as a warning and never prints a deny decision. A row "fires"
when the output has a deny decision or a ``KERBTAP SHADOW`` warning.

Every check the hook performs is exercised in BOTH directions — a case that makes
it deny and a case that makes it stay silent. A suite that only walks the silent
path cannot tell a working gate from a gate wired to nothing.

Run: ``python3 .claude/hooks/test_precommit_doc_consistency_guard.py``
(exit 0 = all pass). No pytest dependency — plain asserts so it runs anywhere the
hook does.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "precommit_doc_consistency_guard.py"


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _init(cwd):
    _git(["init"], cwd)
    _git(["config", "user.email", "test@example.com"], cwd)
    _git(["config", "user.name", "Test"], cwd)


def _write(project_dir, rel_path, text="x\n"):
    p = Path(project_dir) / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _stage(project_dir, rel_path):
    _write(project_dir, rel_path)
    _git(["add", rel_path], project_dir)


def _commit_then_modify(project_dir, rel_path):
    """Land *rel_path* in a commit, then dirty it WITHOUT staging.

    This is the ``git commit -a`` shape: nothing is staged, so the hook sees the
    change only because ``-a`` adds the working-tree diff to the scope.
    """
    _stage(project_dir, rel_path)
    _git(["commit", "-m", "seed"], project_dir)
    _write(project_dir, rel_path, "changed\n")


DENY = '"permissionDecision": "deny"'


def _fired(stdout):
    """The gate fired: a deny decision or its shadow warning."""
    return DENY in stdout or "KERBTAP SHADOW" in stdout


def _run_hook(project_dir, command, tool_name="Bash", payload=None):
    """Invoke the hook with a payload; return (fired, stdout)."""
    if payload is None:
        payload = json.dumps(
            {"tool_name": tool_name, "tool_input": {"command": command}, "cwd": project_dir}
        )
    env = {**os.environ, "CLAUDE_PROJECT_DIR": project_dir}
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, text=True,
        capture_output=True, env=env,
    )
    return _fired(proc.stdout), proc.stdout


TRACKED = "CLAUDE.md"
UNTRACKED_DOC = "docs/user-manual.md"

CASES = [
    # (description, staged files, command, expect_fired): fired = warning, never a block.
    ("tracked rules file → deny", [TRACKED], "git commit -m wip", True),
    ("tracked decision log → deny", ["docs/decision-log.md"], "git commit -m wip", True),
    ("tracked workplan → deny", ["docs/workplan.md"], "git commit -m wip", True),
    ("tracked vision → deny", ["docs/vision.md"], "git commit -m wip", True),
    ("tracked agent boundary file → deny", ["AGENTS.md"], "git commit -m wip", True),
    # Exact-path matching: a same-named file elsewhere in the tree is a different doc.
    ("nested CLAUDE.md → silent", ["starter/CLAUDE.md"], "git commit -m wip", False),
    ("untracked living doc → silent", [UNTRACKED_DOC], "git commit -m wip", False),
    ("source file → silent", ["tool/kerbtap/cli.py"], "git commit -m wip", False),
    ("mixed commit still denies on the tracked doc", [TRACKED, "tool/kerbtap/cli.py"], "git commit -m wip", True),
    # Trigger surface.
    ("non-commit git command → silent", [TRACKED], "git status", False),
    ("commit-shaped word in another command → silent", [TRACKED], "echo committing", False),
    # Bypass placement (audit H1 class): only the env-assignment prefix opens the gate.
    ("bypass prefix is inert (shadow) → warns", [TRACKED], "PROJECT_DOC_REVIEWED=1 PROJECT_DOC_REVIEWED_REASON=x git commit -m wip", True),
    ("bypass token quoted in message → deny", [TRACKED], 'git commit -m "PROJECT_DOC_REVIEWED=1"', True),
    ("bypass token inside message text → deny", [TRACKED], "git commit -m 'see PROJECT_DOC_REVIEWED=1 note'", True),
    ("bypass prefix after && is inert → warns", [TRACKED], "git add -A && PROJECT_DOC_REVIEWED=1 PROJECT_DOC_REVIEWED_REASON=x git commit -m wip", True),
    ("bypass prefix on non-commit segment → deny", [TRACKED], "PROJECT_DOC_REVIEWED=1 git status; git commit -m wip", True),
    ("second assignment between bypass and git is inert → warns", [TRACKED], "PROJECT_DOC_REVIEWED=1 PROJECT_DOC_REVIEWED_REASON=x GIT_AUTHOR_NAME=x git commit -m wip", True),
    ("other project's bypass token → deny", [TRACKED], "PROJECT_REVIEWED=1 git commit -m wip", True),
    ("unbalanced quoting cannot bypass", [TRACKED], "PROJECT_DOC_REVIEWED=1 PROJECT_DOC_REVIEWED_REASON=x git commit -m 'unclosed", True),
    # Git's own options sit between `git` and the subcommand, so a substring
    # match for "git commit" never sees these. Each one was a live bypass.
    ("global -C before commit → deny", [TRACKED], "git -C . commit -m wip", True),
    ("global -c before commit → deny", [TRACKED], "git -c user.name=x commit -m wip", True),
    ("hooksPath override → deny", [TRACKED], "git -c core.hooksPath=/dev/null commit -m wip", True),
    ("--no-pager before commit → deny", [TRACKED], "git --no-pager commit -m wip", True),
    ("stacked global options → deny", [TRACKED], "git --no-pager -C . -c user.name=x commit -m wip", True),
    ("bypass prefix with global option is inert → warns", [TRACKED], "PROJECT_DOC_REVIEWED=1 PROJECT_DOC_REVIEWED_REASON=x git -C . commit -m wip", True),
    # The subcommand is read as a word, so `commit` as an argument to another
    # subcommand must not trip the gate.
    ("commit as argument to another subcommand → silent", [TRACKED], "git log --grep commit", False),
]


def _run_table():
    failures = []
    for desc, files, command, expect_denied in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            _init(tmp)
            for f in files:
                _stage(tmp, f)
            denied, out = _run_hook(tmp, command)
        if DENY in out:
            failures.append(f"FAIL: {desc}\n  the doc gate blocked\n  stdout={out!r}")
        elif denied != expect_denied:
            failures.append(
                f"FAIL: {desc}\n  expected fired={expect_denied}, got {denied}\n  stdout={out!r}"
            )
        else:
            print(f"pass: {desc}")
    return failures


def check_unstaged_fallback_denies(tmp):
    """`git commit -a` shape: tracked doc dirty but unstaged → still denied."""
    _init(tmp)
    _commit_then_modify(tmp, TRACKED)
    denied, out = _run_hook(tmp, "git commit -am wip")
    if not denied:
        return f"unstaged tracked doc escaped the gate (stdout={out!r})"
    return None


def check_unstaged_fallback_stays_scoped(tmp):
    """The same `-a` scope must not fire for an unstaged UNtracked doc."""
    _init(tmp)
    _commit_then_modify(tmp, UNTRACKED_DOC)
    denied, out = _run_hook(tmp, "git commit -am wip")
    if denied:
        return f"unstaged untracked doc wrongly denied (stdout={out!r})"
    return None


def check_non_bash_tool_is_ignored(tmp):
    _init(tmp)
    _stage(tmp, TRACKED)
    denied, out = _run_hook(tmp, "git commit -m wip", tool_name="Edit")
    if denied:
        return f"non-Bash tool call wrongly denied (stdout={out!r})"
    return None


def check_malformed_stdin_is_silent(tmp):
    _init(tmp)
    _stage(tmp, TRACKED)
    denied, out = _run_hook(tmp, "", payload="not json")
    if denied or out.strip():
        return f"malformed stdin should be silent, got stdout={out!r}"
    return None


def check_non_object_json_is_silent(tmp):
    _init(tmp)
    _stage(tmp, TRACKED)
    denied, out = _run_hook(tmp, "", payload="[1, 2, 3]")
    if denied or out.strip():
        return f"non-object JSON should be silent, got stdout={out!r}"
    return None


def check_reason_names_the_doc(tmp):
    """The deny reason has to name what to fix, not merely that something is wrong."""
    _init(tmp)
    _stage(tmp, TRACKED)
    _, out = _run_hook(tmp, "git commit -m wip")
    if TRACKED not in out or "/verify-claims doc-pass" not in out:
        return f"deny reason must name the doc and the pass to run: {out!r}"
    return None


def check_engine_missing_warns(tmp):
    """The guard warns, and does not block, when it cannot import its scope engine.

    A PreToolUse hook that raises fails OPEN — the harness logs the traceback and the
    commit proceeds ungated. Since the scope engine lives in a separate file, "someone
    deleted commit_scope.py" is a real way for this gate to stop gating with no signal.
    The guard must refuse rather than crash.
    """
    repo = Path(tmp) / "repo"
    repo.mkdir()
    _init(str(repo))
    _stage(str(repo), TRACKED)

    lonely = Path(tmp) / "hooks"
    lonely.mkdir()
    copy = lonely / HOOK.name
    copy.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")

    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m wip"},
            "cwd": str(repo),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(copy)], input=payload, text=True,
        capture_output=True, env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)},
    )
    if DENY in proc.stdout or not proc.stdout.startswith('{"systemMessage": "KERBTAP SHADOW'):
        return f"missing engine must warn and not block, got stdout={proc.stdout!r} stderr={proc.stderr!r}"
    if "commit_scope" not in proc.stdout:
        return f"deny reason must name the missing engine: {proc.stdout!r}"
    return None


def check_linked_worktree_index_is_the_one_judged(tmp):
    """The gate reads the committing worktree's index, not CLAUDE_PROJECT_DIR's."""
    main_repo = Path(tmp) / "main"
    main_repo.mkdir()
    _init(str(main_repo))
    _git(["commit", "-q", "--allow-empty", "-m", "base"], str(main_repo))
    worktree = Path(tmp) / "wt"
    _git(["worktree", "add", "-q", "-b", "side", str(worktree)], str(main_repo))
    _stage(str(worktree), TRACKED)

    def run(project_dir, cwd):
        payload = json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "git commit -m wip"}, "cwd": str(cwd)}
        )
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input=payload, text=True, capture_output=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(project_dir)},
        )
        return _fired(proc.stdout)

    if not run(main_repo, worktree):
        return "doc staged in the worktree, project root clean: must deny"
    if run(worktree, main_repo):
        return "project root dirty, commit in a clean checkout: must stay silent"
    return None


def _corrupt_index(repo):
    (Path(repo) / ".git" / "index").write_bytes(b"garbage, not an index\n" * 8)


def test_corrupt_index_denies(tmp):
    """SCOPE-001: a failed scope query denies, names the command and the bypass."""
    _init(tmp)
    _stage(tmp, TRACKED)
    _corrupt_index(tmp)
    denied, out = _run_hook(tmp, "git commit -m x")
    if not (denied and DENY not in out and "git diff --cached --name-only" in out and "_DOC_REVIEWED" not in out):
        return f"corrupt index must warn, naming the command and no bypass: {out!r}"
    return None


def test_corrupt_index_bypass_allows(tmp):
    """SCOPE-001: a corrupt index warns and never blocks, with or without the prefix."""
    _init(tmp)
    _stage(tmp, TRACKED)
    _corrupt_index(tmp)
    prefixed, out = _run_hook(tmp, "PROJECT_DOC_REVIEWED=1 PROJECT_DOC_REVIEWED_REASON=x git commit -m x")
    quoted, out2 = _run_hook(tmp, 'git commit -m "PROJECT_DOC_REVIEWED=1"')
    if not (prefixed and quoted) or DENY in out + out2:
        return f"both must warn and none may block: {out!r} {out2!r}"
    return None


GATE_LOG = Path(".kerbtap") / "gate-log" / "events.jsonl"
BYPASS = "PROJECT_DOC_REVIEWED=1 PROJECT_DOC_REVIEWED_REASON=x"


def _gate_log(repo):
    path = Path(repo) / GATE_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def check_warning_logged_once_per_commit(tmp):
    """GEL-001: each bare commit logs one shadow-deny line, and no deny line."""
    _init(tmp)
    _stage(tmp, TRACKED)
    fired, out = _run_hook(tmp, "git commit -m x")
    lines = _gate_log(tmp)
    if not (
        fired and DENY not in out and len(lines) == 1
        and lines[0]["gate"] == "precommit_doc_consistency_guard"
        and lines[0]["decision"] == "shadow-deny"
        and lines[0]["reason_code"] == "living-docs"
    ):
        return f"fired={fired} out={out!r} log={lines}"
    return None


def check_bypass_is_inert(tmp):
    """GEL-002: a bypass prefix, bare, expired or valid, clears nothing: the gate warns and logs a shadow-deny."""
    _init(tmp)
    _stage(tmp, TRACKED)
    commands = [
        "PROJECT_DOC_REVIEWED=1 git commit -m x",
        f"{BYPASS} PROJECT_DOC_REVIEWED_UNTIL=2000-01-01T00:00:00Z git commit -m x",
        f"{BYPASS} PROJECT_DOC_REVIEWED_UNTIL=2999-01-01T00:00:00Z git commit -m x",
        f"{BYPASS} git commit -m x",
    ]
    for command in commands:
        fired, out = _run_hook(tmp, command)
        if not fired or DENY in out or "BYPASS REFUSED" in out:
            return f"{command}: fired={fired} out={out!r}"
    decisions = [(l["decision"], l["reason_code"]) for l in _gate_log(tmp)]
    if decisions != [("shadow-deny", "living-docs")] * len(commands):
        return f"log={_gate_log(tmp)}"
    return None


def check_unwritable_log_still_warns(tmp):
    """GEL-004: with no way to write the gate log the gate still warns and does not block."""
    _init(tmp)
    _stage(tmp, TRACKED)
    (Path(tmp) / ".governor").mkdir()
    (Path(tmp) / ".governor" / "gate-log").write_text("not a directory", encoding="utf-8")
    fired, out = _run_hook(tmp, f"{BYPASS} git commit -m x")
    if not fired or DENY in out:
        return f"fired={fired} stdout={out!r}"
    return None


RECORDS = Path(".kerbtap") / "doc-pass" / "records.jsonl"
SECOND = "docs/decision-log.md"


def _record(repo, rel):
    """Append the record `kerbtap doc-pass record` writes, for the working-tree file."""
    blob = subprocess.run(
        ["git", "hash-object", "--", rel], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()
    path = Path(repo) / RECORDS
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": "2026-01-01T00:00:00Z", "path": rel, "blob": blob}) + "\n")


def _decisions(repo):
    return [line["decision"] for line in _gate_log(repo)]


def check_matching_record_clears(tmp):
    """DPR-002: a record of the staged blob allows the commit with one clear line, no bypass."""
    _init(tmp)
    _stage(tmp, TRACKED)
    _record(tmp, TRACKED)
    denied, out = _run_hook(tmp, "git commit -m x")
    lines = _gate_log(tmp)
    if denied or out or [l["decision"] for l in lines] != ["clear"] or lines[0]["reason_code"] != "doc-pass-record":
        return f"denied={denied} stdout={out!r} log={lines}"
    return None


def check_unwritable_log_refuses_clear(tmp):
    """DPR-002: with no way to write the clear line the matching record does not clear."""
    _init(tmp)
    _stage(tmp, TRACKED)
    _record(tmp, TRACKED)
    (Path(tmp) / ".kerbtap" / "gate-log").write_text("not a directory", encoding="utf-8")
    denied, out = _run_hook(tmp, "git commit -m x")
    return None if denied else f"stdout={out!r}"


def check_no_record_denies(tmp):
    """DPR-003: no record file, no clearance."""
    _init(tmp)
    _stage(tmp, TRACKED)
    denied, out = _run_hook(tmp, "git commit -m x")
    if not denied or "clear" in _decisions(tmp):
        return f"denied={denied} log={_gate_log(tmp)}"
    return None


def check_stale_record_denies(tmp):
    """DPR-003: a doc edited after the record no longer matches."""
    _init(tmp)
    _write(tmp, TRACKED, "one\n")
    _record(tmp, TRACKED)
    _write(tmp, TRACKED, "two\n")
    _git(["add", TRACKED], tmp)
    denied, _ = _run_hook(tmp, "git commit -m x")
    if not denied or "clear" in _decisions(tmp):
        return f"denied={denied} log={_gate_log(tmp)}"
    return None


def check_partial_record_denies(tmp):
    """DPR-003: one unrecorded doc denies the commit and the text names it."""
    _init(tmp)
    _stage(tmp, TRACKED)
    _stage(tmp, SECOND)
    _record(tmp, TRACKED)
    denied, out = _run_hook(tmp, "git commit -m x")
    if not (denied and SECOND in out and "clear" not in _decisions(tmp)):
        return f"denied={denied} stdout={out!r}"
    return None


def check_warning_names_the_pass_then_the_record(tmp):
    """DPR-004: the warning names the pass, then the record command, and names no bypass."""
    _init(tmp)
    _stage(tmp, TRACKED)
    fired, out = _run_hook(tmp, "git commit -m x")
    if not (
        fired and 0 <= out.find("/verify-claims doc-pass") < out.find("kerbtap doc-pass record")
        and "_DOC_REVIEWED" not in out
    ):
        return f"fired={fired} stdout={out!r}"
    return None


def _staged_then_edited(tmp):
    _init(tmp)
    _stage(tmp, TRACKED)
    _write(tmp, TRACKED, "edited\n")


def check_all_commit_worktree_record_clears(tmp):
    """DPR-005: -a lands the working-tree blob, so a record of it clears."""
    _staged_then_edited(tmp)
    _record(tmp, TRACKED)
    denied, out = _run_hook(tmp, "git commit -a -m x")
    if denied or out or _decisions(tmp) != ["clear"]:
        return f"denied={denied} stdout={out!r} log={_gate_log(tmp)}"
    return None


def check_all_commit_stale_worktree_denies(tmp):
    """DPR-005: -a after an edit that no record covers denies, though the index blob is recorded."""
    _init(tmp)
    _stage(tmp, TRACKED)
    _record(tmp, TRACKED)
    _write(tmp, TRACKED, "edited\n")
    denied, _ = _run_hook(tmp, "git commit -a -m x")
    if not denied or "clear" in _decisions(tmp):
        return f"denied={denied} log={_gate_log(tmp)}"
    return None


def check_pathspec_commit_index_differs_denies(tmp):
    """DPR-005: a pathspec commit takes the working tree, so index != working tree denies."""
    _staged_then_edited(tmp)
    _record(tmp, TRACKED)
    denied, _ = _run_hook(tmp, f"git commit -m x -- {TRACKED}")
    if not denied or "clear" in _decisions(tmp):
        return f"denied={denied} log={_gate_log(tmp)}"
    return None


def check_add_and_commit_old_record_denies(tmp):
    """CS-002: a record of the old content does not clear `git add <doc> && git commit`."""
    _init(tmp)
    _stage(tmp, TRACKED)
    _record(tmp, TRACKED)
    _write(tmp, TRACKED, "edited\n")
    denied, out = _run_hook(tmp, f"git add {TRACKED} && git commit -m x")
    if not denied or "clear" in _decisions(tmp):
        return f"denied={denied} log={_gate_log(tmp)}"
    return None


def check_add_and_commit_current_record_clears(tmp):
    """CS-002: a record of the working-tree content clears `git add <doc> && git commit`."""
    _init(tmp)
    _git(["commit", "-q", "--allow-empty", "-m", "base"], tmp)
    _write(tmp, TRACKED, "one\n")
    _record(tmp, TRACKED)
    denied, out = _run_hook(tmp, f"git add {TRACKED} && git commit -m x")
    if denied or out or _decisions(tmp) != ["clear"]:
        return f"denied={denied} stdout={out!r} log={_gate_log(tmp)}"
    return None


def check_add_patch_record_does_not_clear(tmp):
    """CS-002: `git add -p <doc>` may stage part of the doc, so a record of the whole does not clear."""
    _init(tmp)
    _git(["commit", "-q", "--allow-empty", "-m", "base"], tmp)
    _write(tmp, TRACKED, "one\n")
    _record(tmp, TRACKED)
    denied, out = _run_hook(tmp, f"git add -p {TRACKED} && git commit -m x")
    if not denied or "clear" in _decisions(tmp):
        return f"denied={denied} log={_gate_log(tmp)}"
    return None


def check_untokenizable_add_record_does_not_clear(tmp):
    """CS-002: an add in a command that does not tokenize may be partial; a full record does not clear."""
    _init(tmp)
    _git(["commit", "-q", "--allow-empty", "-m", "base"], tmp)
    _write(tmp, TRACKED, "one\n")
    _record(tmp, TRACKED)
    command = f"git add -p {TRACKED} && git commit -F - <<'EOF'\nit's wip\nEOF"
    denied, out = _run_hook(tmp, command)
    if not denied or "clear" in _decisions(tmp):
        return f"denied={denied} log={_gate_log(tmp)}"
    return None


def _set_mode(repo, modes):
    mode_file = Path(repo) / ".kerbtap" / "gate-mode.json"
    mode_file.parent.mkdir(parents=True, exist_ok=True)
    mode_file.write_text(json.dumps(modes), encoding="utf-8")


def _shadow_failure(out, lines):
    """None when the output is one shadow warning with no deny decision and the log has one shadow-deny."""
    message = json.loads(out).get("systemMessage", "") if out.strip() else ""
    if not (
        DENY not in out and "permissionDecision" not in out
        and message.startswith("KERBTAP SHADOW (precommit_doc_consistency_guard would deny)")
        and [l["decision"] for l in lines] == ["shadow-deny"]
        and lines[0]["reason_code"]
    ):
        return f"out={out!r} log={lines}"
    return None


def check_default_is_shadow(tmp):
    """DG-001: with no mode file the violating commit is allowed, warned about, and logged once."""
    _init(tmp)
    _stage(tmp, TRACKED)
    _, out = _run_hook(tmp, "git commit -m x")
    return _shadow_failure(out, _gate_log(tmp))


def check_malformed_mode_is_shadow(tmp):
    """DG-001: a mode file that is not JSON does not make the gate block."""
    _init(tmp)
    _stage(tmp, TRACKED)
    mode_file = Path(tmp) / ".kerbtap" / "gate-mode.json"
    mode_file.parent.mkdir(parents=True)
    mode_file.write_text("{not json", encoding="utf-8")
    _, out = _run_hook(tmp, "git commit -m x")
    return _shadow_failure(out, _gate_log(tmp))


def check_enforce_does_not_block(tmp):
    """DG-001: `enforce` in the mode file does not make the gate block, and a mode for another gate is no change."""
    for modes in ({"precommit_doc_consistency_guard": "enforce"}, {"precommit_review_guard": "shadow"}):
        _init(tmp)
        _stage(tmp, TRACKED)
        _set_mode(tmp, modes)
        _, out = _run_hook(tmp, "git commit -m x")
        problem = _shadow_failure(out, _gate_log(tmp))
        if problem:
            return f"{modes}: {problem}"
        shutil.rmtree(Path(tmp) / ".git")
        shutil.rmtree(Path(tmp) / ".kerbtap")
    return None


def check_shadow_mode_allows_and_logs(tmp):
    """SGM-001: a `shadow` mode file gives the same warning and one shadow-deny line."""
    _init(tmp)
    _stage(tmp, TRACKED)
    _set_mode(tmp, {"precommit_doc_consistency_guard": "shadow"})
    _, out = _run_hook(tmp, "git commit -m x")
    return _shadow_failure(out, _gate_log(tmp))


CHECKS = [
    ("check_warning_logged_once_per_commit", check_warning_logged_once_per_commit),
    ("check_default_is_shadow", check_default_is_shadow),
    ("check_malformed_mode_is_shadow", check_malformed_mode_is_shadow),
    ("check_enforce_does_not_block", check_enforce_does_not_block),
    ("check_shadow_mode_allows_and_logs", check_shadow_mode_allows_and_logs),
    ("check_bypass_is_inert", check_bypass_is_inert),
    ("check_unwritable_log_still_warns", check_unwritable_log_still_warns),
    ("check_matching_record_clears", check_matching_record_clears),
    ("check_unwritable_log_refuses_clear", check_unwritable_log_refuses_clear),
    ("check_no_record_denies", check_no_record_denies),
    ("check_stale_record_denies", check_stale_record_denies),
    ("check_partial_record_denies", check_partial_record_denies),
    ("check_warning_names_the_pass_then_the_record", check_warning_names_the_pass_then_the_record),
    ("check_all_commit_worktree_record_clears", check_all_commit_worktree_record_clears),
    ("check_all_commit_stale_worktree_denies", check_all_commit_stale_worktree_denies),
    ("check_pathspec_commit_index_differs_denies", check_pathspec_commit_index_differs_denies),
    ("check_add_and_commit_old_record_denies", check_add_and_commit_old_record_denies),
    ("check_add_and_commit_current_record_clears", check_add_and_commit_current_record_clears),
    ("check_add_patch_record_does_not_clear", check_add_patch_record_does_not_clear),
    ("check_untokenizable_add_record_does_not_clear", check_untokenizable_add_record_does_not_clear),
    ("linked worktree: its own index is judged", check_linked_worktree_index_is_the_one_judged),
    ("unstaged tracked doc still denied (commit -a)", check_unstaged_fallback_denies),
    ("unstaged untracked doc stays silent", check_unstaged_fallback_stays_scoped),
    ("non-Bash tool call ignored", check_non_bash_tool_is_ignored),
    ("malformed stdin is silent", check_malformed_stdin_is_silent),
    ("non-object JSON stdin is silent", check_non_object_json_is_silent),
    ("deny reason names the doc and the pass", check_reason_names_the_doc),
    ("engine missing → warns, never blocks", check_engine_missing_warns),
    ("test_corrupt_index_denies", test_corrupt_index_denies),
    ("test_corrupt_index_bypass_allows", test_corrupt_index_bypass_allows),
]


def main():
    failures = _run_table()

    for desc, check in CHECKS:
        with tempfile.TemporaryDirectory() as tmp:
            problem = check(tmp)
        if problem:
            failures.append(f"FAIL: {desc}\n  {problem}")
        else:
            print(f"pass: {desc}")

    total = len(CASES) + len(CHECKS)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"\n{len(failures)}/{total} case(s) failed", file=sys.stderr)
        sys.exit(1)
    print(f"\nall {total} cases passed")


if __name__ == "__main__":
    main()
