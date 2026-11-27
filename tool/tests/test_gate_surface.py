"""Review-gate applicability per commit: changed paths × the gate's own state at that commit.

The pack used to decide whether the review gate applied from the commit message alone, so a
guarded change with no review signal was reported ``not-applicable`` and never reached the
exception log. These tests pin the replacement: the gate's surface and wiring are read from
each commit's own tree, and a reading that cannot be established is ``undetermined``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kerbtap.assemble import build_bundle
from kerbtap.build import render_pack
from kerbtap.collect import gate_surface
from kerbtap.collect import github as github_mod
from kerbtap.collect.gate_surface import (
    ABSENT,
    ACTIVE,
    UNREADABLE,
    UNWIRED,
    parse_surface,
    parse_wiring,
    surfaces_by_commit,
)
from kerbtap.collect.git import changed_paths
from tests.repo_factory import (
    GATE_HOOK_REL as HOOK_REL,
    GATE_SETTINGS_REL as SETTINGS_REL,
    commit_files as _commit,
    gate_hook_source as _hook,
    gate_settings_source as _settings,
    git,
    init_repo,
    install_review_gate as _install,
)


def _gates(repo: Path) -> dict[str, tuple[str, str]]:
    """change_id -> (outcome, basis) for the review gate, via the real bundle."""
    bundle = build_bundle(repo)
    return {c.change_id: (c.policy_gates[0].outcome, c.policy_gates[0].basis) for c in bundle.changes}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Offline and deterministic: the git-only path, as in the input-space suite.
    monkeypatch.setattr(github_mod, "gh_available", lambda: False)
    return init_repo(tmp_path / "r")


# --- parse_surface ------------------------------------------------------------------


class TestParseSurface:
    def test_literal_constants_are_read(self) -> None:
        s = parse_surface(_hook(bypass='"ACME_REVIEWED"').encode())
        assert s.state == ACTIVE
        assert s.prefixes == ("docs/plan/",)
        assert s.suffixes == (".md", ".json")
        assert s.bypass_env == "ACME_REVIEWED"
        assert s.override_names == ("GOVERNOR_REVIEWED", "ACME_REVIEWED")

    def test_earliest_singular_suffix_spelling_is_read(self) -> None:
        src = 'TRACKED_PATH_PREFIXES = ("docs/",)\nTRACKED_SUFFIX = ".md"\n'
        assert parse_surface(src.encode()).suffixes == (".md",)

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param('TRACKED_SUFFIXES = (".md",)\n', id="prefixes-missing"),
            pytest.param('TRACKED_PATH_PREFIXES = ("docs/",)\n', id="suffixes-missing"),
            pytest.param('P = "docs/"\nTRACKED_PATH_PREFIXES = (P,)\nTRACKED_SUFFIXES = (".md",)\n', id="computed-prefixes"),
            pytest.param('TRACKED_PATH_PREFIXES = "docs/"\nTRACKED_SUFFIXES = (".md",)\n', id="bare-string-prefixes"),
            pytest.param('TRACKED_PATH_PREFIXES = ("docs/", 3)\nTRACKED_SUFFIXES = (".md",)\n', id="non-string-member"),
            pytest.param(
                'TRACKED_PATH_PREFIXES = ("docs/",)\nTRACKED_SUFFIXES = (".md",)\nTRACKED_PATH_PREFIXES = tuple(x)\n',
                id="later-computed-binding-wins",
            ),
            pytest.param("def broken(:\n", id="syntax-error"),
            pytest.param("# gate\n", id="no-constants"),
            pytest.param('TRACKED_PATH_PREFIXES = ("docs/",)\nTRACKED_SUFFIXES = ("",)\n', id="empty-suffix"),
            pytest.param('TRACKED_PATH_PREFIXES = ("docs/",)\nTRACKED_SUFFIX = ""\n', id="empty-singular-suffix"),
        ],
    )
    def test_uncertain_surface_is_unreadable(self, source: str) -> None:
        assert parse_surface(source.encode()).state == UNREADABLE

    def test_empty_prefix_is_read(self) -> None:
        s = parse_surface(b'TRACKED_PATH_PREFIXES = ("",)\nTRACKED_SUFFIXES = (".py",)\n')
        assert s.state == ACTIVE
        assert s.prefixes == ("",)
        assert s.guards("@x.py") and not s.guards(".github/x.py")

    def test_non_utf8_is_unreadable(self) -> None:
        assert parse_surface(b"TRACKED_PATH_PREFIXES = ('\xff',)\n").state == UNREADABLE

    def test_oversized_is_unreadable(self) -> None:
        assert parse_surface(b"#" * ((1 << 20) + 1)).state == UNREADABLE

    def test_computed_bypass_env_falls_back_to_default_name(self) -> None:
        s = parse_surface(_hook(bypass='"A" + "B"').encode())
        assert s.state == ACTIVE
        assert s.override_names == ("GOVERNOR_REVIEWED",)

    def test_hook_code_is_never_executed(self, tmp_path: Path) -> None:
        marker = tmp_path / "executed"
        source = (
            f"open({str(marker)!r}, 'w').write('x')\n"
            'TRACKED_PATH_PREFIXES = ("docs/",)\nTRACKED_SUFFIXES = (".md",)\n'
        )
        assert parse_surface(source.encode()).state == ACTIVE
        assert not marker.exists()


# --- parse_wiring -------------------------------------------------------------------


class TestParseWiring:
    @pytest.mark.parametrize("matcher", ["Bash", "Bash|Edit", "Edit|Bash", "*", "", None, "Ba.*"])
    def test_matchers_that_select_bash_are_wired(self, matcher: object) -> None:
        assert parse_wiring(_settings(matcher=matcher).encode()) is True

    @pytest.mark.parametrize("matcher", ["Edit|Write", "Task|Agent", "Bashful"])
    def test_matchers_that_do_not_select_bash_are_unwired(self, matcher: str) -> None:
        assert parse_wiring(_settings(matcher=matcher).encode()) is False

    def test_other_hooks_only_is_unwired(self) -> None:
        assert parse_wiring(_settings(command="python3 other_guard.py").encode()) is False

    @pytest.mark.parametrize(
        "command",
        [
            'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/precommit_review_guard.py"',
            "python3 .claude/hooks/precommit_review_guard.py",
            "python3 '/abs/repo/.claude/hooks/precommit_review_guard.py'",
            "uv run .claude/hooks/precommit_review_guard.py --flag",
        ],
    )
    def test_absolute_and_relative_hook_commands_are_wired(self, command: str) -> None:
        assert parse_wiring(_settings(command=command).encode()) is True

    @pytest.mark.parametrize(
        "command",
        [
            "python3 x.claude/hooks/precommit_review_guard.py",
            "python3 .claude/hooks/precommit_review_guard.py.bak",
            "python3 .claude/hooks/precommit_review_guard_old.py",
        ],
    )
    def test_lookalike_hook_commands_are_not_wired(self, command: str) -> None:
        assert parse_wiring(_settings(command=command).encode()) is False

    def test_no_hooks_key_is_unwired(self) -> None:
        assert parse_wiring(b'{"permissions": {}}') is False

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(b"{not json", id="malformed"),
            pytest.param(b"[]", id="not-an-object"),
            pytest.param(b'{"hooks": []}', id="hooks-not-object"),
            pytest.param(b'{"hooks": {"PreToolUse": {}}}', id="pretooluse-not-list"),
            pytest.param(b'{"hooks": {"PreToolUse": ["x"]}}', id="entry-not-object"),
            pytest.param(b"\xff", id="non-utf8"),
        ],
    )
    def test_uncertain_settings_are_unknown(self, raw: bytes) -> None:
        assert parse_wiring(raw) is None

    def test_invalid_matcher_on_the_gate_entry_is_unknown(self) -> None:
        assert parse_wiring(_settings(matcher="(").encode()) is None

    def test_invalid_matcher_does_not_mask_a_valid_wiring(self) -> None:
        data = json.loads(_settings())
        bad = {"matcher": "(", "hooks": data["hooks"]["PreToolUse"][0]["hooks"]}
        data["hooks"]["PreToolUse"].insert(0, bad)
        assert parse_wiring(json.dumps(data).encode()) is True


# --- changed_paths ------------------------------------------------------------------


class TestChangedPaths:
    def test_root_add_modify_delete_rename_and_odd_names(self, repo: Path) -> None:
        root = _commit(repo, "root", {"a.md": "a\n", "dir/b.md": "b\n"})
        odd = "docs/with space\nand newline-é.md"
        added = _commit(repo, "odd", {odd: "x\n"})
        git(repo, "mv", "dir/b.md", "dir/c.md")
        git(repo, "commit", "-m", "rename")
        renamed = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "rm", "a.md")
        git(repo, "commit", "-m", "delete")
        deleted = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "commit", "--allow-empty", "-m", "empty")
        empty = git(repo, "rev-parse", "HEAD").stdout.strip()

        paths = changed_paths(repo)
        assert set(paths[root]) == {"a.md", "dir/b.md"}
        assert paths[added] == (odd,)
        assert set(paths[renamed]) == {"dir/b.md", "dir/c.md"}
        assert paths[deleted] == ("a.md",)
        assert paths[empty] == ()

    def test_merge_commits_are_absent(self, repo: Path) -> None:
        _commit(repo, "root", {"a.md": "a\n"})
        git(repo, "checkout", "-b", "feat")
        _commit(repo, "feat", {"b.md": "b\n"})
        git(repo, "checkout", "main")
        git(repo, "merge", "--no-ff", "feat", "-m", "Merge pull request #1 from org/feat")
        merge = git(repo, "rev-parse", "HEAD").stdout.strip()
        assert merge not in changed_paths(repo)

    def test_unborn_repository_has_no_paths(self, repo: Path) -> None:
        assert changed_paths(repo) == {}


# --- surfaces_by_commit -------------------------------------------------------------


class TestSurfacesByCommit:
    def test_each_commit_reads_its_own_hook_version(self, repo: Path) -> None:
        before = _commit(repo, "before gate", {"x.md": "x\n"})
        first = _install(repo, hook=_hook(prefixes='("docs/a/",)'))
        second = _commit(repo, "move surface", {HOOK_REL: _hook(prefixes='("docs/b/",)', bypass='"ACME_REVIEWED"')})
        surfaces = surfaces_by_commit(repo, [before, first, second])
        assert surfaces[before].state == ABSENT
        assert surfaces[first].prefixes == ("docs/a/",)
        assert surfaces[second].prefixes == ("docs/b/",)
        assert surfaces[second].bypass_env == "ACME_REVIEWED"

    def test_hook_without_settings_is_unwired(self, repo: Path) -> None:
        sha = _commit(repo, "hook only", {HOOK_REL: _hook()})
        assert surfaces_by_commit(repo, [sha])[sha].state == UNWIRED

    def test_malformed_settings_is_unreadable(self, repo: Path) -> None:
        sha = _install(repo, settings="{oops")
        assert surfaces_by_commit(repo, [sha])[sha].state == UNREADABLE

    def test_failed_git_reading_is_unreadable_not_absent(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sha = _commit(repo, "plain", {"x.md": "x\n"})
        monkeypatch.setattr(gate_surface, "_git_batch", lambda *a, **k: None)
        assert surfaces_by_commit(repo, [sha])[sha].state == UNREADABLE

    def test_misaligned_batch_output_is_unreadable(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sha = _commit(repo, "plain", {"x.md": "x\n"})
        monkeypatch.setattr(gate_surface, "_git_batch", lambda *a, **k: b"")
        assert surfaces_by_commit(repo, [sha])[sha].state == UNREADABLE

    def test_hook_path_that_is_a_directory_is_unreadable(self, repo: Path) -> None:
        sha = _commit(repo, "dir hook", {f"{HOOK_REL}/inner.txt": "x\n", SETTINGS_REL: _settings()})
        assert surfaces_by_commit(repo, [sha])[sha].state == UNREADABLE


# --- end to end through the pack -----------------------------------------------------


class TestReviewGateOutcome:
    def test_guarded_change_with_no_signal_is_no_recorded_evidence(self, repo: Path) -> None:
        _install(repo)
        sha = _commit(repo, "edit plan", {"docs/plan/x.md": "x\n"})
        outcome, basis = _gates(repo)[sha[:12]]
        assert outcome == "no-recorded-evidence"
        assert "touched a path the gate guarded" in basis

    @pytest.mark.parametrize(
        "rel",
        [
            pytest.param("docs/other/x.md", id="outside-prefix"),
            pytest.param("docs/plan/x.txt", id="outside-suffix"),
            pytest.param("docs/planning/x.md", id="prefix-lookalike"),
        ],
    )
    def test_unguarded_change_is_not_applicable(self, repo: Path, rel: str) -> None:
        _install(repo)
        sha = _commit(repo, "edit", {rel: "x\n"})
        assert _gates(repo)[sha[:12]][0] == "not-applicable"

    def test_guarded_json_is_guarded(self, repo: Path) -> None:
        _install(repo)
        sha = _commit(repo, "edit data", {"docs/plan/model.json": "{}\n"})
        assert _gates(repo)[sha[:12]][0] == "no-recorded-evidence"

    def test_change_before_the_gate_existed_is_not_applicable(self, repo: Path) -> None:
        sha = _commit(repo, "early", {"docs/plan/x.md": "x\n"})
        _install(repo)
        outcome, basis = _gates(repo)[sha[:12]]
        assert outcome == "not-applicable"
        assert basis == "The review gate was not installed at this commit."

    def test_unwired_gate_is_not_applicable(self, repo: Path) -> None:
        _commit(repo, "hook only", {HOOK_REL: _hook()})
        sha = _commit(repo, "edit plan", {"docs/plan/x.md": "x\n"})
        outcome, basis = _gates(repo)[sha[:12]]
        assert outcome == "not-applicable"
        assert "not wired" in basis

    @pytest.mark.parametrize(
        "hook",
        [
            pytest.param("# gate\n", id="no-constants"),
            pytest.param("def broken(:\n", id="syntax-error"),
            pytest.param('TRACKED_PATH_PREFIXES = tuple(["docs/"])\nTRACKED_SUFFIXES = (".md",)\n', id="non-literal"),
        ],
    )
    def test_unreadable_surface_with_no_signal_is_undetermined(self, repo: Path, hook: str) -> None:
        _install(repo, hook=hook)
        sha = _commit(repo, "edit anything", {"docs/plan/x.md": "x\n"})
        outcome, basis = _gates(repo)[sha[:12]]
        assert outcome == "undetermined"
        assert "undetermined" in basis

    def test_unreadable_surface_with_an_override_is_still_undetermined(self, repo: Path) -> None:
        _install(repo, hook="# gate\n")
        sha = _commit(repo, "edit", {"docs/plan/x.md": "x\n"}, body="GOVERNOR_REVIEWED=1 used")
        assert _gates(repo)[sha[:12]][0] == "undetermined"

    def test_renamed_bypass_is_recognised_per_commit(self, repo: Path) -> None:
        _install(repo, hook=_hook(bypass='"ACME_REVIEWED"'))
        sha = _commit(repo, "edit plan", {"docs/plan/x.md": "x\n"}, body="Landed with ACME_REVIEWED=1.")
        bundle = build_bundle(repo)
        change = next(c for c in bundle.changes if c.change_id == sha[:12])
        assert change.review.verification == "override"
        assert change.policy_gates[0].outcome == "no-recorded-evidence"

    def test_renamed_bypass_is_not_recognised_before_the_rename(self, repo: Path) -> None:
        _install(repo)
        sha = _commit(repo, "edit plan", {"docs/plan/x.md": "x\n"}, body="Landed with ACME_REVIEWED=1.")
        _commit(repo, "rename bypass", {HOOK_REL: _hook(bypass='"ACME_REVIEWED"')})
        change = next(c for c in build_bundle(repo).changes if c.change_id == sha[:12])
        assert change.review.verification == "none"

    def test_merge_commit_is_not_applicable(self, repo: Path) -> None:
        _install(repo)
        git(repo, "checkout", "-b", "feat")
        _commit(repo, "feat", {"docs/plan/x.md": "x\n"})
        git(repo, "checkout", "main")
        git(repo, "merge", "--no-ff", "feat", "-m", "Merge pull request #1 from org/feat")
        merge = git(repo, "rev-parse", "HEAD").stdout.strip()
        outcome, basis = _gates(repo)[merge[:12]]
        assert outcome == "not-applicable"
        assert basis.startswith("Merge commit")

    def test_exception_log_and_report_surface_silent_and_undetermined_changes(self, repo: Path) -> None:
        _install(repo)
        silent = _commit(repo, "silent plan edit", {"docs/plan/x.md": "x\n"})
        _commit(repo, "break the hook", {HOOK_REL: "# gate\n"})
        unknown = _commit(repo, "edit under unreadable gate", {"docs/plan/y.md": "y\n"})
        rendered = render_pack(build_bundle(repo))
        log = rendered["exception-log.md"]
        assert "**No review signal at all (1).**" in log
        assert f"`{silent[:12]}`" in log
        assert "**Applicability undetermined (2).**" in log  # the hook-breaking commit is guarded-surface-unknown too
        assert f"`{unknown[:12]}`" in log
        report = rendered["report.md"]
        assert "1 change(s) touched a path the review gate guarded and carry no review signal at all" in report
        assert "2 change(s) could not be assessed against the review gate" in report
        manifest = json.loads(rendered["manifest.json"])
        gates = {c["change_id"]: c["policy_gates"][0] for c in manifest["changes"]}
        assert gates[silent[:12]]["outcome"] == "no-recorded-evidence"
        assert gates[silent[:12]]["basis"]
