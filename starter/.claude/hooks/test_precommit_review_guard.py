#!/usr/bin/env python3
"""Integration test for precommit_review_guard.py.

Drives the actual hook as a subprocess against a throwaway git repo, exactly as
the harness invokes it: a JSON payload on stdin, ``CLAUDE_PROJECT_DIR`` in the
environment, and the deny decision (or silence) read back from stdout.

Run: ``python3 .claude/hooks/test_precommit_review_guard.py`` (exit 0 = all pass).
No pytest dependency — plain asserts so it runs anywhere the hook does.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "precommit_review_guard.py"


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _run_hook(project_dir, command):
    """Invoke the hook with a Bash git-commit payload; return (denied, stdout)."""
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": project_dir}
    )
    env = {**os.environ, "CLAUDE_PROJECT_DIR": project_dir}
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, text=True,
        capture_output=True, env=env,
    )
    denied = '"permissionDecision": "deny"' in proc.stdout
    return denied, proc.stdout


def _stage(project_dir, rel_path):
    p = Path(project_dir) / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x\n")
    _git(["add", rel_path], project_dir)


CASES = [
    # (description, staged files, command, expect_denied)
    ("md deliverable → deny", ["docs/deliverables/market-sizing.md"], "git commit -m wip", True),
    ("json deliverable → deny (widened scope)", ["docs/deliverables/model/assumptions.json"], "git commit -m wip", True),
    ("md outside business-plan → silent", ["docs/workplan.md"], "git commit -m wip", False),
    ("json outside business-plan → silent", ["docs/decision-log.json"], "git commit -m wip", False),
    ("bypass prefix → silent", ["docs/deliverables/market-sizing.md"], "PROJECT_REVIEWED=1 PROJECT_REVIEWED_REASON=x git commit -m wip", False),
    ("non-commit command → silent", ["docs/deliverables/market-sizing.md"], "git status", False),
    # Bypass-token placement (audit H1): only the env-assignment prefix opens the gate.
    ("bypass token quoted in message → deny", ["docs/deliverables/market-sizing.md"], 'git commit -m "PROJECT_REVIEWED=1"', True),
    ("bypass token inside message text → deny", ["docs/deliverables/market-sizing.md"], "git commit -m 'see PROJECT_REVIEWED=1 note'", True),
    ("bypass prefix after && → silent", ["docs/deliverables/market-sizing.md"], "git add -A && PROJECT_REVIEWED=1 PROJECT_REVIEWED_REASON=x git commit -m wip", False),
    ("bypass prefix on non-commit segment → deny", ["docs/deliverables/market-sizing.md"], "PROJECT_REVIEWED=1 git status; git commit -m wip", True),
    ("second assignment between bypass and git → silent", ["docs/deliverables/market-sizing.md"], "PROJECT_REVIEWED=1 PROJECT_REVIEWED_REASON=x GIT_AUTHOR_NAME=x git commit -m wip", False),
    # Git's own options sit between `git` and the subcommand, so a substring
    # match for "git commit" never sees these. Each one was a live bypass.
    ("global -C before commit → deny", ["docs/deliverables/market-sizing.md"], "git -C . commit -m wip", True),
    ("global -c before commit → deny", ["docs/deliverables/market-sizing.md"], "git -c user.name=x commit -m wip", True),
    ("hooksPath override → deny", ["docs/deliverables/market-sizing.md"], "git -c core.hooksPath=/dev/null commit -m wip", True),
    ("--no-pager before commit → deny", ["docs/deliverables/market-sizing.md"], "git --no-pager commit -m wip", True),
    ("--git-dir before commit → deny", ["docs/deliverables/market-sizing.md"], "git --git-dir=.git commit -m wip", True),
    ("stacked global options → deny", ["docs/deliverables/market-sizing.md"], "git --no-pager -C . -c user.name=x commit -m wip", True),
    ("bypass prefix with global option → silent", ["docs/deliverables/market-sizing.md"], "PROJECT_REVIEWED=1 PROJECT_REVIEWED_REASON=x git -C . commit -m wip", False),
    # The subcommand is read as a word, so `commit` appearing as an argument to
    # another subcommand must not trip the gate.
    ("commit as argument to another subcommand → silent", ["docs/deliverables/market-sizing.md"], "git log --grep commit", False),
    ("git show of a commit → silent", ["docs/deliverables/market-sizing.md"], "git show HEAD --format=commit", False),
]


TELEMETRY = Path(".kerbtap") / "telemetry" / "ungated-commit-verbs.jsonl"

# (description, command, verb expected in telemetry or None). Every case runs with a
# guarded file staged, and none may deny: telemetry never decides.
TELEMETRY_CASES = [
    ("rebase --continue → recorded", "git rebase --continue", "rebase --continue"),
    ("cherry-pick → recorded", "git cherry-pick abc123", "cherry-pick"),
    ("cherry-pick --continue → recorded", "git cherry-pick --continue", "cherry-pick"),
    ("revert → recorded", "git revert HEAD", "revert"),
    ("merge → recorded", "git merge feature", "merge"),
    ("merge --continue → recorded", "git merge --continue", "merge"),
    ("am → recorded", "git am patch.mbox", "am"),
    ("global options before the verb → recorded", "git -C . -c user.name=x cherry-pick abc", "cherry-pick"),
    ("verb chained after add → recorded", "git add -A && git merge feature", "merge"),
    ("env prefix → recorded", "GIT_EDITOR=true git rebase --continue", "rebase --continue"),
    ("excluded first, verb second → second recorded", "git merge --abort && git cherry-pick abc", "cherry-pick"),
    ("bare rebase → not recorded", "git rebase main", None),
    ("rebase --abort → not recorded", "git rebase --abort", None),
    ("rebase --skip → not recorded", "git rebase --skip", None),
    ("cherry-pick --abort → not recorded", "git cherry-pick --abort", None),
    ("cherry-pick -n → not recorded", "git cherry-pick -n abc", None),
    ("cherry-pick --no-commit → not recorded", "git cherry-pick --no-commit abc", None),
    ("revert --no-commit → not recorded", "git revert --no-commit HEAD", None),
    ("revert --quit → not recorded", "git revert --quit", None),
    ("merge --abort → not recorded", "git merge --abort", None),
    ("merge --squash → not recorded", "git merge --squash feature", None),
    ("merge --no-commit → not recorded", "git merge --no-commit feature", None),
    ("am --abort → not recorded", "git am --abort", None),
    ("am --show-current-patch → not recorded", "git am --show-current-patch", None),
    ("merge -n still merges → recorded", "git merge -n feature", "merge"),
    ("verb word as an argument → not recorded", "git log --grep merge", None),
    ("exclusion does not leak across a separator", "git merge --abort; git status", None),
    ("plain commit → not recorded", "git commit -m wip", None),
]


def _telemetry_lines(repo):
    path = Path(repo) / TELEMETRY
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _telemetry_failures():
    failures = []
    for desc, command, expected in TELEMETRY_CASES:
        with tempfile.TemporaryDirectory() as tmp:
            _git(["init"], tmp)
            _stage(tmp, "docs/deliverables/market-sizing.md")
            denied, out = _run_hook(tmp, command)
            if command.startswith("git commit"):
                denied = False  # the commit itself is gated; this case only checks telemetry
            lines = _telemetry_lines(tmp)
            got = [line.get("verb") for line in lines]
            want = [expected] if expected else []
            shape_ok = all({"ts", "gate", "verb", "branch", "head"} <= set(line) for line in lines)
            if denied or got != want or not shape_ok:
                failures.append(
                    f"FAIL: telemetry: {desc}\n  denied={denied} verbs={got} want={want} "
                    f"shape_ok={shape_ok}\n  stdout={out!r}"
                )
            else:
                print(f"pass: telemetry: {desc}")

    # A commit-creating verb chained with a gated commit: recorded AND still denied.
    with tempfile.TemporaryDirectory() as tmp:
        _git(["init"], tmp)
        _stage(tmp, "docs/deliverables/market-sizing.md")
        denied, out = _run_hook(tmp, "git cherry-pick abc && git commit -m wip")
        verbs = [line.get("verb") for line in _telemetry_lines(tmp)]
        if denied and verbs == ["cherry-pick"]:
            print("pass: telemetry: verb + gated commit → recorded and denied")
        else:
            failures.append(
                f"FAIL: telemetry: verb + gated commit\n  denied={denied} verbs={verbs}\n  stdout={out!r}"
            )

    # An unwritable telemetry location never blocks the command.
    with tempfile.TemporaryDirectory() as tmp:
        _git(["init"], tmp)
        (Path(tmp) / ".governor").write_text("not a directory\n")
        payload = json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "git merge feature"}, "cwd": tmp}
        )
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input=payload, text=True, capture_output=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": tmp},
        )
        if proc.returncode == 0 and proc.stdout.strip() == "":
            print("pass: telemetry: unwritable location → command proceeds silently")
        else:
            failures.append(
                f"FAIL: telemetry: unwritable location\n  rc={proc.returncode} "
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
            )
    return failures


TELEMETRY_TOTAL = len(TELEMETRY_CASES) + 2


def _run_hook_at(project_dir, cwd, command):
    """Like _run_hook, but with CLAUDE_PROJECT_DIR and the payload cwd set independently."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd})
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, text=True, capture_output=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": project_dir},
    )
    return '"permissionDecision": "deny"' in proc.stdout, proc.stdout


def _commit_all(repo):
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base"], repo)


def _repo_resolution_failures():
    """The gate reads the index of the repository the commit lands in.

    It used to read CLAUDE_PROJECT_DIR's index: a commit in a linked worktree was judged by
    the main checkout's staged files — a false deny when main had guarded files staged, and
    an unguarded landing when the worktree did.
    """
    failures = []
    guarded = "docs/deliverables/market-sizing.md"
    with tempfile.TemporaryDirectory() as tmp:
        main_repo = str(Path(tmp) / "main")
        other = str(Path(tmp) / "other")
        for repo in (main_repo, other):
            Path(repo).mkdir()
            _git(["init", "-q"], repo)
            _commit_all(repo)
        worktree = str(Path(tmp) / "wt")
        _git(["worktree", "add", "-q", "-b", "side", worktree], main_repo)

        cases = []
        # Linked worktree, guarded file staged only in the worktree: deny.
        _stage(worktree, guarded)
        cases.append(("worktree staged, project root clean → deny", main_repo, worktree, "git commit -m wip", True))
        cases.append(("-C into the dirty worktree from a clean cwd → deny", main_repo, main_repo,
                      f"git -C {worktree} commit -m wip", True))
        cases.append(("cd into the dirty worktree, then commit → deny", main_repo, main_repo,
                      f"cd {worktree} && git commit -m wip", True))
        cases.append(("a quoted && is not an operator, so the cd is not followed → silent", main_repo, main_repo,
                      f'cd {worktree} "&&" git commit -m wip', False))
        cases.append(("a quoted && before a ; is not an operator, so the cd is not followed → silent", main_repo, main_repo,
                      f'cd {worktree} "&&"; git commit -m wip', False))
        cases.append(("relative -C resolves against the cwd → deny", main_repo, tmp,
                      "git -C wt commit -m wip", True))
        cases.append(("cd as an argument is not followed → silent", main_repo, main_repo,
                      f"echo cd {worktree} && git commit -m wip", False))
        # Project root with guarded file staged, committing elsewhere: silent.
        _stage(other, guarded)
        cases.append(("project root dirty, commit in a clean repo → silent", other, main_repo, "git commit -m wip", False))
        cases.append(("-C into a clean repo from a dirty cwd → silent", other, other,
                      f"git -C {main_repo} commit -m wip", False))

        for desc, project_dir, cwd, command, expect in cases:
            denied, out = _run_hook_at(project_dir, cwd, command)
            if denied != expect:
                failures.append(f"FAIL: repo resolution: {desc}\n  expected denied={expect}, got {denied}\n  stdout={out!r}")
            else:
                print(f"pass: repo resolution: {desc}")
    return failures


RESOLUTION_TOTAL = 7


def _engine_missing_fails_closed():
    """The guard denies when it cannot import its scope engine.

    A PreToolUse hook that raises fails OPEN — the harness logs the traceback and the
    commit proceeds ungated. Since the scope engine lives in a separate file, "someone
    deleted commit_scope.py" is a real way for this gate to stop gating with no signal.
    The guard must refuse rather than crash.

    Driven by copying the guard alone into a directory with no engine beside it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        _git(["init"], str(repo))
        _stage(str(repo), "docs/deliverables/market-sizing.md")

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
        denied = '"permissionDecision": "deny"' in proc.stdout
        names_cause = "commit_scope" in proc.stdout
        if denied and names_cause:
            print("pass: engine missing → deny (fails closed)")
            return None
        return (
            "FAIL: engine missing → deny (fails closed)\n"
            f"  denied={denied} names_cause={names_cause}\n"
            f"  stdout={proc.stdout!r}\n  stderr={proc.stderr!r}"
        )


def _deny_message_failures():
    """The deny message names a record command that runs from the repo root.

    `python -m kerbtap` fails there unless the tool is installed into that interpreter;
    the `kerbtap` console script runs under every documented install (pipx, venv).
    """
    with tempfile.TemporaryDirectory() as tmp:
        _git(["init"], tmp)
        _stage(tmp, "docs/deliverables/market-sizing.md")
        denied, out = _run_hook(tmp, "git commit -m wip")
    checks = [
        ("deny message names `kerbtap review record`", "`kerbtap review record " in out),
        ("deny message does not name `python -m kerbtap`", "python -m kerbtap" not in out),
        ("deny message keeps its bypass name", "PROJECT_REVIEWED=1" in out),
    ]
    failures = []
    for desc, ok in checks:
        if denied and ok:
            print(f"pass: {desc}")
        else:
            failures.append(f"FAIL: {desc}\n  denied={denied}\n  stdout={out!r}")
    return failures


DENY_MESSAGE_TOTAL = 3

GUARDED = "docs/deliverables/market-sizing.md"


def _corrupt_index(repo):
    (Path(repo) / ".git" / "index").write_bytes(b"garbage, not an index\n" * 8)


def test_corrupt_index_denies():
    """SCOPE-001: a failed scope query denies, names the command and the bypass."""
    with tempfile.TemporaryDirectory() as tmp:
        _git(["init"], tmp)
        _stage(tmp, GUARDED)
        _corrupt_index(tmp)
        denied, out = _run_hook(tmp, "git commit -m x")
    if denied and "git diff --cached --name-only" in out and "PROJECT_REVIEWED=1" in out:
        print("pass: test_corrupt_index_denies")
        return None
    return f"FAIL: test_corrupt_index_denies\n  denied={denied}\n  stdout={out!r}"


def test_corrupt_index_bypass_allows():
    """SCOPE-001: the env-assignment prefix clears the gate; the token in the message does not."""
    with tempfile.TemporaryDirectory() as tmp:
        _git(["init"], tmp)
        _stage(tmp, GUARDED)
        _corrupt_index(tmp)
        prefixed, out1 = _run_hook(tmp, "PROJECT_REVIEWED=1 PROJECT_REVIEWED_REASON=x git commit -m x")
        quoted, out2 = _run_hook(tmp, 'git commit -m "PROJECT_REVIEWED=1"')
    if not prefixed and quoted:
        print("pass: test_corrupt_index_bypass_allows")
        return None
    return (
        f"FAIL: test_corrupt_index_bypass_allows\n  prefixed denied={prefixed} "
        f"quoted denied={quoted}\n  {out1!r} {out2!r}"
    )


def test_non_ascii_path_denies():
    """SCOPE-002: a quoted-by-git path still matches the guarded surface."""
    head, _, tail = GUARDED.rpartition("/")
    rel = f"{head}/\u00e9-{tail}"  # same directory and suffix as a guarded path, so any install layout matches
    with tempfile.TemporaryDirectory() as tmp:
        _git(["init"], tmp)
        _stage(tmp, rel)
        denied, out = _run_hook(tmp, "git commit -m x")
    if denied and rel in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]:
        print("pass: test_non_ascii_path_denies")
        return None
    return f"FAIL: test_non_ascii_path_denies\n  denied={denied}\n  stdout={out!r}"


def _hook_with_surface(root, prefixes, suffixes):
    """Copy the hook and its engine under ``root`` with its own surface constants.

    The tests below set their own surface, so an installed copy (whose constants and
    fixtures ``kerbtap init`` rewrites) still runs them against a whole-repository surface.
    """
    hooks = Path(root) / "hooks"
    hooks.mkdir()
    shutil.copy(HOOK.parent / "commit_scope.py", hooks / "commit_scope.py")
    text = HOOK.read_text(encoding="utf-8")
    for name, values in (("TRACKED_PATH_PREFIXES", prefixes), ("TRACKED_SUFFIXES", suffixes)):
        rendered = repr(tuple(values))
        text, n = re.subn(rf"^{name} = \([^)]*\)", lambda _m: f"{name} = {rendered}", text, count=1, flags=re.MULTILINE)
        assert n == 1, name
    copy = hooks / HOOK.name
    copy.write_text(text, encoding="utf-8")
    return copy


def _whole_repo_run(rel):
    """Stage ``rel`` in a repo guarded by ("",) / (".py",); return (denied, stdout)."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        _git(["init"], str(repo))
        _stage(str(repo), rel)
        copy = _hook_with_surface(tmp, [""], [".py"])
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": str(repo)})
        proc = subprocess.run(
            [sys.executable, str(copy)], input=payload, text=True, capture_output=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)},
        )
    return '"permissionDecision": "deny"' in proc.stdout, proc.stdout


def test_whole_repo_surface_denies_at_path():
    """SURF-001: on an ("",) surface a path starting with @, + or ~ is guarded."""
    for rel in ("@x.py", "+x.py", "~x.py", "\u00e9x.py", "sub/x.py"):
        denied, out = _whole_repo_run(rel)
        if not (denied and rel in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]):
            return f"FAIL: test_whole_repo_surface_denies_at_path ({rel!r})\n  denied={denied}\n  stdout={out!r}"
    print("pass: test_whole_repo_surface_denies_at_path")
    return None


def test_whole_repo_surface_skips_dot_path():
    """SURF-001: a dot path stays off an ("",) surface, and so does a wrong suffix."""
    for rel in (".github/x.py", "@x.txt"):
        denied, out = _whole_repo_run(rel)
        if denied:
            return f"FAIL: test_whole_repo_surface_skips_dot_path ({rel!r})\n  stdout={out!r}"
    print("pass: test_whole_repo_surface_skips_dot_path")
    return None


GATE_LOG = Path(".kerbtap") / "gate-log" / "events.jsonl"
BYPASS = "PROJECT_REVIEWED=1 PROJECT_REVIEWED_REASON=x"


def _gate_log(repo):
    path = Path(repo) / GATE_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _guarded_repo(tmp):
    _git(["init"], tmp)
    _stage(tmp, GUARDED)


def check_deny_and_bypass_logged():
    """GEL-001: a bare commit logs one deny; the bypass with a reason logs one bypass."""
    with tempfile.TemporaryDirectory() as tmp:
        _guarded_repo(tmp)
        denied, _ = _run_hook(tmp, "git commit -m x")
        after_deny = _gate_log(tmp)
        bypassed, _ = _run_hook(tmp, f"{BYPASS} git commit -m x")
        lines = _gate_log(tmp)
    ok = (
        denied and not bypassed and len(after_deny) == 1 and len(lines) == 2
        and after_deny[0]["gate"] == "precommit_review_guard"
        and after_deny[0]["decision"] == "deny"
        and lines[1]["decision"] == "bypass"
        and lines[1]["bypass_form"] == "env-prefix"
        and lines[1]["bypass_reason"] == "x"
    )
    if ok:
        print("pass: check_deny_and_bypass_logged")
        return None
    return f"FAIL: check_deny_and_bypass_logged\n  denied={denied} bypassed={not bypassed} log={lines}"


def check_bypass_without_reason_is_refused():
    """GEL-002: a bare PROJECT_REVIEWED=1 denies, and the line says why."""
    with tempfile.TemporaryDirectory() as tmp:
        _guarded_repo(tmp)
        denied, out = _run_hook(tmp, "PROJECT_REVIEWED=1 git commit -m x")
        lines = _gate_log(tmp)
    if denied and "bypass-no-reason" in out and [l["reason_code"] for l in lines] == ["bypass-no-reason"]:
        print("pass: check_bypass_without_reason_is_refused")
        return None
    return f"FAIL: check_bypass_without_reason_is_refused\n  denied={denied} log={lines}"


def check_expired_bypass_is_refused():
    """GEL-003: a past expiry denies; so does an unreadable one."""
    for until in ("2000-01-01T00:00:00Z", "tomorrow"):
        with tempfile.TemporaryDirectory() as tmp:
            _guarded_repo(tmp)
            denied, _ = _run_hook(tmp, f"{BYPASS} PROJECT_REVIEWED_UNTIL={until} git commit -m x")
            lines = _gate_log(tmp)
        if not (denied and [l["reason_code"] for l in lines] == ["bypass-expired"]):
            return f"FAIL: check_expired_bypass_is_refused ({until})\n  denied={denied} log={lines}"
    with tempfile.TemporaryDirectory() as tmp:
        _guarded_repo(tmp)
        denied, _ = _run_hook(tmp, f"{BYPASS} PROJECT_REVIEWED_UNTIL=2999-01-01T00:00:00Z git commit -m x")
    if denied:
        return "FAIL: check_expired_bypass_is_refused\n  a future expiry denied"
    print("pass: check_expired_bypass_is_refused")
    return None


def check_unwritable_record_refuses_bypass():
    """GEL-004: with no way to write the record the bypass does not clear the gate."""
    with tempfile.TemporaryDirectory() as tmp:
        _guarded_repo(tmp)
        (Path(tmp) / ".governor").mkdir()
        (Path(tmp) / ".governor" / "gate-log").write_text("not a directory", encoding="utf-8")
        denied, out = _run_hook(tmp, f"{BYPASS} git commit -m x")
    if denied and "bypass-unrecorded" in out:
        print("pass: check_unwritable_record_refuses_bypass")
        return None
    return f"FAIL: check_unwritable_record_refuses_bypass\n  denied={denied} stdout={out!r}"


def _set_mode(repo, modes):
    mode_file = Path(repo) / ".kerbtap" / "gate-mode.json"
    mode_file.parent.mkdir(parents=True, exist_ok=True)
    mode_file.write_text(json.dumps(modes), encoding="utf-8")


def check_shadow_mode_allows_and_logs():
    """SGM-001: in shadow the violating commit is allowed, warned about, and logged once."""
    with tempfile.TemporaryDirectory() as tmp:
        _guarded_repo(tmp)
        _set_mode(tmp, {"precommit_review_guard": "shadow"})
        denied, out = _run_hook(tmp, "git commit -m x")
        lines = _gate_log(tmp)
    message = json.loads(out).get("systemMessage", "") if out.strip() else ""
    ok = (
        not denied and "permissionDecision" not in out
        and message.startswith("KERBTAP SHADOW (precommit_review_guard would deny)")
        and [(l["decision"], l["reason_code"]) for l in lines] == [("shadow-deny", "guarded-files")]
    )
    if ok:
        print("pass: check_shadow_mode_allows_and_logs")
        return None
    return f"FAIL: check_shadow_mode_allows_and_logs\n  denied={denied} out={out!r} log={lines}"


def check_enforce_mode_still_denies():
    """SGM-003: an enforce mode, or a mode for another gate, still denies and logs a deny."""
    for modes in ({"precommit_review_guard": "enforce"}, {"precommit_doc_consistency_guard": "shadow"}):
        with tempfile.TemporaryDirectory() as tmp:
            _guarded_repo(tmp)
            _set_mode(tmp, modes)
            denied, _ = _run_hook(tmp, "git commit -m x")
            lines = _gate_log(tmp)
        if not (denied and [l["decision"] for l in lines] == ["deny"]):
            return f"FAIL: check_enforce_mode_still_denies ({modes})\n  denied={denied} log={lines}"
    print("pass: check_enforce_mode_still_denies")
    return None


GEL_TESTS = (
    check_deny_and_bypass_logged, check_bypass_without_reason_is_refused,
    check_expired_bypass_is_refused, check_unwritable_record_refuses_bypass,
    check_shadow_mode_allows_and_logs, check_enforce_mode_still_denies,
)

SCOPE_TESTS = (
    test_corrupt_index_denies, test_corrupt_index_bypass_allows, test_non_ascii_path_denies,
    test_whole_repo_surface_denies_at_path, test_whole_repo_surface_skips_dot_path,
) + GEL_TESTS


def main():
    failures = []
    for desc, files, command, expect_denied in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            _git(["init"], tmp)
            for f in files:
                _stage(tmp, f)
            denied, out = _run_hook(tmp, command)
            if denied != expect_denied:
                failures.append(
                    f"FAIL: {desc}\n  expected denied={expect_denied}, got {denied}\n  stdout={out!r}"
                )
            else:
                print(f"pass: {desc}")

    total = len(CASES) + 1 + TELEMETRY_TOTAL + RESOLUTION_TOTAL + DENY_MESSAGE_TOTAL + len(SCOPE_TESTS)
    engine_failure = _engine_missing_fails_closed()
    if engine_failure:
        failures.append(engine_failure)
    failures.extend(_telemetry_failures())
    failures.extend(_repo_resolution_failures())
    failures.extend(_deny_message_failures())
    failures.extend(f for f in (t() for t in SCOPE_TESTS) if f)

    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"\n{len(failures)}/{total} case(s) failed", file=sys.stderr)
        sys.exit(1)
    print(f"\nall {total} cases passed")


if __name__ == "__main__":
    main()
