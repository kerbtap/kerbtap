#!/usr/bin/env python3
"""Integration test for the prospective-commit scope in commit_scope.py.

Drives BOTH real commit gates as subprocesses over git states that each scope rule
decides: a merge conclusion (dogfood O73), an empty index (O72), ``-a`` and pathspec
commits, and ``--amend``. Every rule is exercised in both directions — a state the
gate must deny and a state it must let through — so a scope that returned nothing,
or everything, fails here.

Run: ``python3 .claude/hooks/test_commit_scope.py`` (exit 0 = all pass).
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))
sys.dont_write_bytecode = True  # importing the gates must not litter the hooks directory
# Not on any gate's surface: no suffix a gate tracks, at the repository root.
OTHER = "scope-fixture.unguarded"


def _gates():
    """``(gate file, a path it guards)`` for each commit gate present, read from the gate.

    An install rewrites each gate's surface, so the guarded path comes from the gate's
    own constants rather than from this repository's.
    """
    gates = []
    if (HOOKS / "precommit_doc_consistency_guard.py").is_file():
        import precommit_doc_consistency_guard as doc
        gates.append(("precommit_doc_consistency_guard.py", sorted(doc.TRACKED_PATHS)[0]))
    if (HOOKS / "precommit_review_guard.py").is_file():
        import precommit_review_guard as review
        prefix = review.TRACKED_PATH_PREFIXES[0]
        gates.append(("precommit_review_guard.py", f"{prefix}scope-fixture{review.TRACKED_SUFFIXES[0]}"))
    return gates


def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=cwd, check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _write(repo, rel, text):
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _commit(repo, files, message):
    for rel, text in files.items():
        _write(repo, rel, text)
        _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", message)


def _repo(tmp, guarded):
    _git(tmp, "init", "-q", "-b", "main")
    _git(tmp, "config", "user.email", "t@example.com")
    _git(tmp, "config", "user.name", "T")
    _commit(tmp, {guarded: "base\n", OTHER: "base\n"}, "base")


def _publish_main(repo):
    """Make ``main`` the remote default branch, as a clone's ``origin/HEAD`` is."""
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


def _denied(gate, repo, command):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": repo})
    proc = subprocess.run(
        [sys.executable, str(HOOKS / gate)], input=payload, text=True,
        capture_output=True, env={**os.environ, "CLAUDE_PROJECT_DIR": repo},
    )
    if '"permissionDecision": "deny"' in proc.stdout:
        return True
    # The doc gate is always shadow: it fires as a warning, and never blocks.
    return gate.startswith("precommit_doc") and "KERBTAP SHADOW" in proc.stdout


def _merge_state(tmp, guarded, *, main_edit, feature_edit, publish=True):
    """``feature`` with ``main`` merged in and not yet committed."""
    _repo(tmp, guarded)
    _git(tmp, "checkout", "-q", "-b", "feature")
    _commit(tmp, feature_edit, "feature")
    _git(tmp, "checkout", "-q", "main")
    _commit(tmp, main_edit, "main")
    if publish:
        _publish_main(tmp)
    _git(tmp, "checkout", "-q", "feature")
    _git(tmp, "merge", "--no-commit", "--no-ff", "main")


# Each scenario: (description, setup(tmp, guarded), command, expect_denied).

def merge_incoming_unchanged(tmp, g):
    _merge_state(tmp, g, main_edit={g: "main\n"}, feature_edit={OTHER: "feature\n"})


def merge_incoming_unpublished(tmp, g):
    _merge_state(tmp, g, main_edit={g: "main\n"}, feature_edit={OTHER: "feature\n"}, publish=False)


def merge_conflict_resolved(tmp, g):
    _merge_state(tmp, g, main_edit={g: "main\n"}, feature_edit={g: "feature\n"})
    _write(tmp, g, "resolved\n")
    _git(tmp, "add", g)


def merge_conflict_resolved_to_ours(tmp, g):
    _merge_state(tmp, g, main_edit={g: "main\n"}, feature_edit={g: "feature\n"})
    _git(tmp, "checkout", "--ours", "--", g)
    _git(tmp, "add", g)


def merge_edit_without_conflict(tmp, g):
    _merge_state(tmp, g, main_edit={OTHER: "main\n"}, feature_edit={OTHER + ".x": "f\n"})
    _write(tmp, g, "edited during the merge\n")
    _git(tmp, "add", g)


def unrelated_staged_guarded_unstaged(tmp, g):
    _repo(tmp, g)
    _write(tmp, OTHER, "staged\n")
    _git(tmp, "add", OTHER)
    _write(tmp, g, "unstaged\n")


def guarded_unstaged_only(tmp, g):
    _repo(tmp, g)
    _write(tmp, g, "unstaged\n")


def amend_head_unrelated(tmp, g):
    _repo(tmp, g)
    _commit(tmp, {OTHER: "second\n"}, "second")
    _write(tmp, g, "unstaged\n")


def amend_head_touched_guarded(tmp, g):
    _repo(tmp, g)
    _commit(tmp, {g: "second\n"}, "second")


def guarded_untracked(tmp, g):
    _git(tmp, "init", "-q", "-b", "main")
    _git(tmp, "config", "user.email", "t@example.com")
    _git(tmp, "config", "user.name", "T")
    _commit(tmp, {OTHER: "base\n"}, "base")
    _write(tmp, g, "new\n")


def guarded_unstaged_with_sub(tmp, g):
    guarded_unstaged_only(tmp, g)
    (Path(tmp) / "sub").mkdir()  # the command's `mkdir -p sub` has not run when the hook reads


SCENARIOS = [
    ("merge: guarded file unchanged from a landed MERGE_HEAD → silent",
     merge_incoming_unchanged, "git commit -m merge", False),
    ("merge: same, MERGE_HEAD not in the remote default branch → deny",
     merge_incoming_unpublished, "git commit -m merge", True),
    ("merge: guarded conflict resolved by hand → deny",
     merge_conflict_resolved, "git commit -m merge", True),
    ("merge: guarded conflict resolved to our side → deny",
     merge_conflict_resolved_to_ours, "git commit -m merge", True),
    ("merge: guarded file edited with no conflict → deny",
     merge_edit_without_conflict, "git commit -m merge", True),
    ("-a with unrelated file staged, guarded file unstaged → deny",
     unrelated_staged_guarded_unstaged, "git commit -a -m wip", True),
    ("-qam cluster → deny", unrelated_staged_guarded_unstaged, "git commit -qam wip", True),
    ("--all after the message → deny",
     unrelated_staged_guarded_unstaged, "git commit -m wip --all", True),
    ("plain commit, guarded file only unstaged → silent (O72)",
     unrelated_staged_guarded_unstaged, "git commit -m wip", False),
    ("pathspec after -- names the unstaged guarded file → deny",
     guarded_unstaged_only, "git commit -m wip -- {g}", True),
    ("bare pathspec names the unstaged guarded file → deny",
     guarded_unstaged_only, "git commit -m wip {g}", True),
    ("-o with pathspec → deny", guarded_unstaged_only, "git commit -o -m wip {g}", True),
    ("message value that looks like the path is not a pathspec → silent",
     guarded_unstaged_only, "git commit -m {g}", False),
    ("message-only amend, HEAD unrelated, guarded file unstaged → silent (O72)",
     amend_head_unrelated, "git commit --amend -m reworded", False),
    ("amend with empty index, HEAD touched the guarded file → deny",
     amend_head_touched_guarded, "git commit --amend --no-edit", True),
    ("add <guarded> && commit → deny",
     guarded_unstaged_only, "git add {g} && git commit -m wip", True),
    ("add <guarded>; commit → deny",
     guarded_unstaged_only, "git add {g}; git commit -m wip", True),
    ("add -A && commit → deny",
     guarded_unstaged_only, "git add -A && git commit -m wip", True),
    ("add . && commit, guarded file untracked → deny",
     guarded_untracked, "git add . && git commit -m wip", True),
    ("cd sub && git add ../<guarded> && cd .. && commit → deny",
     guarded_unstaged_with_sub,
     "mkdir -p sub && cd sub && git add ../{g} && cd .. && git commit -m wip", True),
    ("stage <guarded> && commit → deny",
     guarded_unstaged_only, "git stage {g} && git commit -m wip", True),
    ("add && commit with an untokenizable heredoc → deny",
     guarded_unstaged_only,
     "git add {g} && git commit -F - <<'EOF'\nit's wip\nEOF", True),
    ("add --pathspec-from-file && commit → deny",
     guarded_unstaged_only,
     "git add --pathspec-from-file list.txt && git commit -m wip", True),
    ("add <other> && commit, guarded unstaged → silent (O37)",
     guarded_unstaged_only, "git add {o} && git commit -m wip", False),
]


def test_failed_query_raises_scope_error():
    """SCOPE-001: staged_paths raises on a failed query instead of returning []."""
    import commit_scope
    with tempfile.TemporaryDirectory() as tmp:
        _repo(tmp, "docs/a.md")
        (Path(tmp) / ".git" / "index").write_bytes(b"garbage, not an index\n" * 8)
        try:
            commit_scope.staged_paths(tmp, "git commit -m x")
        except commit_scope.ScopeError as exc:
            if "git diff --cached --name-only" not in str(exc):
                return f"ScopeError must name the command: {exc}"
            return None
    return "staged_paths returned instead of raising ScopeError"


def test_special_char_paths_unquoted():
    """SCOPE-002: tab, double quote, non-ASCII and edge-space paths come back as real names."""
    import commit_scope
    names = ["docs/t\tab.md", 'docs/q"uote.md', "docs/\u00e9.md", " docs/lead.md", "docs/trail.md "]
    with tempfile.TemporaryDirectory() as tmp:
        _repo(tmp, "docs/a.md")
        for rel in names:
            _write(tmp, rel, "x\n")
            _git(tmp, "add", "--", rel)
        got = commit_scope.staged_paths(tmp, "git commit -m x")
    missing = [n for n in names if n not in got]
    return f"paths not returned by real name: {missing!r} (got {got!r})" if missing else None


def test_landing_blob_conflict_is_none():
    """DPR-005: a path with conflict stages has no landing blob; a clean staged path has one."""
    import commit_scope
    with tempfile.TemporaryDirectory() as tmp:
        _repo(tmp, "CLAUDE.md")
        _git(tmp, "checkout", "-q", "-b", "side")
        _commit(tmp, {"CLAUDE.md": "side\n"}, "side")
        _git(tmp, "checkout", "-q", "-")
        _commit(tmp, {"CLAUDE.md": "main\n"}, "main")
        _git(tmp, "merge", "side")
        got = commit_scope.landing_blob(tmp, "git commit -m x", "CLAUDE.md")
        if got is not None:
            return f"conflicted path returned {got!r}"
        _write(tmp, "CLAUDE.md", "resolved\n")
        _git(tmp, "add", "CLAUDE.md")
        if not commit_scope.landing_blob(tmp, "git commit -m x", "CLAUDE.md"):
            return "resolved path returned no blob"
    return None


def main():
    failures = []
    total = 0
    for test in (test_failed_query_raises_scope_error, test_special_char_paths_unquoted,
                 test_landing_blob_conflict_is_none):
        total += 1
        problem = test()
        if problem:
            failures.append(f"FAIL: {test.__name__}\n  {problem}")
        else:
            print(f"pass: {test.__name__}")
    gates = _gates()
    if not gates:
        print("no commit gate installed; nothing to test")
        return
    for gate, guarded in gates:
        for desc, setup, command, expect_denied in SCENARIOS:
            total += 1
            with tempfile.TemporaryDirectory() as tmp:
                setup(tmp, guarded)
                denied = _denied(gate, tmp, command.format(g=guarded, o=OTHER))
            label = f"{gate}: {desc}"
            if denied != expect_denied:
                failures.append(f"FAIL: {label}\n  expected denied={expect_denied}, got {denied}")
            else:
                print(f"pass: {label}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"\n{len(failures)}/{total} case(s) failed", file=sys.stderr)
        sys.exit(1)
    print(f"\nall {total} cases passed")


if __name__ == "__main__":
    main()
