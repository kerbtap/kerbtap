"""Review verdicts name their subject, bound to content; verification checks coverage.

A recorded verdict used to carry only free text about what it reviewed, so a trailer could
resolve while its artifact vouched for a different file or an earlier revision. These tests
pin the binding: ``record`` refuses a verdict without a subject and binds each subject path
to its staged blob; a record counts only when it covers the change at the committed content
(its guarded paths, else all its paths; never a merge or empty commit); artifacts that name
no files still count and are reported apart.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from kerbtap.assemble import build_bundle
from kerbtap.build import render_pack
from kerbtap.collect import github as github_mod
from kerbtap.review.record import (
    BINDING_KEY,
    ReviewError,
    coverage_paths,
    normalize_subject,
    record,
    resolve_pack_signal,
    verify,
    verify_artifact,
)
from kerbtap.review.trailer import (
    REASON_SUBJECT_MISMATCH,
    SCOPE_CONFIRMED,
    SCOPE_NOT_RECORDED,
    classify_review,
    format_trailers,
)
from kerbtap.state import state_rel
from tests.repo_factory import (
    GATE_HOOK_REL,
    commit_files,
    gate_hook_source,
    git,
    init_repo,
    install_review_gate,
)

TOOL_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(github_mod, "gh_available", lambda: False)
    r = init_repo(tmp_path / "r")
    install_review_gate(r)
    return r


def _stage(repo: Path, files: dict[str, str | None]) -> None:
    """Write (or delete, for ``None``) files and stage them."""
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            git(repo, "rm", "-q", rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(repo, "add", rel)


def _verdict(tmp: Path, subject: object, **extra: object) -> Path:
    path = tmp / "verdict.json"
    path.write_text(json.dumps({"verdict": "PASS", "reviewer": "reviewer", "subject": subject, **extra}))
    return path


def _record_and_commit(repo: Path, tmp: Path, subject: list[str], files: dict[str, str | None]) -> str:
    """Stage ``files``, record a PASS naming ``subject``, commit change + artifact."""
    _stage(repo, files)
    artifact, trailers = record(_verdict(tmp, subject), "PASS", "reviewer", repo)
    git(repo, "add", str(artifact.relative_to(repo)))
    git(repo, "commit", "-q", "-m", f"reviewed change\n\n{trailers}")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def _signal(repo: Path, sha: str):
    body = git(repo, "log", "-1", "--format=%B", sha).stdout
    return classify_review(body)


# --- record refuses a verdict without a usable subject --------------------------------


class TestSubjectValidation:
    @pytest.mark.parametrize(
        "subject",
        [
            pytest.param(None, id="missing"),
            pytest.param([], id="empty"),
            pytest.param("docs/plan/a.md", id="not-a-list"),
            pytest.param([""], id="empty-entry"),
            pytest.param([3], id="non-string"),
            pytest.param(["/etc/passwd"], id="absolute"),
            pytest.param(["../outside.md"], id="traversal"),
            pytest.param(["docs/../x.md"], id="inner-traversal"),
            pytest.param(["./docs/plan/a.md"], id="dot-component"),
            pytest.param(["docs//plan/a.md"], id="double-slash"),
            pytest.param(["docs/plan/"], id="trailing-slash"),
            pytest.param(["docs\\plan\\a.md"], id="backslash"),
            pytest.param(["a.md", "a.md"], id="duplicate"),
        ],
    )
    def test_record_refuses(self, repo: Path, tmp_path: Path, subject: object) -> None:
        payload = {"verdict": "PASS", "reviewer": "reviewer"}
        if subject is not None:
            payload["subject"] = subject
        path = tmp_path / "v.json"
        path.write_text(json.dumps(payload))
        with pytest.raises(ReviewError):
            record(path, "PASS", "reviewer", repo)
        assert not (repo / state_rel(repo, "reviews")).exists()

    def test_normal_paths_are_accepted(self) -> None:
        assert normalize_subject(["docs/plan/a.md", "README"]) == ["docs/plan/a.md", "README"]

    def test_cli_record_exits_2_without_subject(self, repo: Path, tmp_path: Path) -> None:
        path = tmp_path / "v.json"
        path.write_text(json.dumps({"verdict": "PASS", "reviewer": "reviewer"}))
        proc = subprocess.run(
            [sys.executable, "-m", "kerbtap", "review", "record", "--review-output", str(path),
             "--verdict", "PASS", "--reviewer", "reviewer", "--repo", str(repo)],
            cwd=TOOL_ROOT, capture_output=True, text=True,
        )
        assert proc.returncode == 2
        assert "subject" in proc.stderr


class TestBinding:
    def test_binding_is_the_staged_blob_and_overwrites_a_forged_one(self, repo: Path, tmp_path: Path) -> None:
        _stage(repo, {"docs/plan/a.md": "reviewed\n"})
        staged = git(repo, "rev-parse", ":docs/plan/a.md").stdout.strip()
        forged = [{"path": "docs/plan/a.md", "blob": "0" * 40}]
        artifact, _ = record(_verdict(tmp_path, ["docs/plan/a.md"], **{BINDING_KEY: forged}), "PASS", "reviewer", repo)
        stored = json.loads(artifact.read_text())
        assert stored[BINDING_KEY] == [{"path": "docs/plan/a.md", "blob": staged}]

    def test_unstaged_path_binds_null(self, repo: Path, tmp_path: Path) -> None:
        artifact, _ = record(_verdict(tmp_path, ["docs/plan/never.md"]), "PASS", "reviewer", repo)
        assert json.loads(artifact.read_text())[BINDING_KEY] == [{"path": "docs/plan/never.md", "blob": None}]


# --- coverage ------------------------------------------------------------------------


class TestCoverage:
    def test_named_guarded_paths_at_committed_content_are_confirmed(self, repo: Path, tmp_path: Path) -> None:
        sha = _record_and_commit(repo, tmp_path, ["docs/plan/a.md", "docs/plan/b.json"],
                                 {"docs/plan/a.md": "a\n", "docs/plan/b.json": "{}\n"})
        ok, message, _ = verify(repo, sha)
        assert ok, message
        assert verify_artifact(repo, _signal(repo, sha), sha).scope == SCOPE_CONFIRMED

    def test_unguarded_changes_and_extra_subject_paths_do_not_fail(self, repo: Path, tmp_path: Path) -> None:
        sha = _record_and_commit(
            repo, tmp_path, ["docs/plan/a.md", "docs/plan/not-in-commit.md"],
            {"docs/plan/a.md": "a\n", "README.md": "unguarded\n", "src/x.py": "x = 1\n"},
        )
        assert verify(repo, sha)[0]

    def test_a_guarded_path_missing_from_the_subject_fails(self, repo: Path, tmp_path: Path) -> None:
        sha = _record_and_commit(repo, tmp_path, ["docs/plan/a.md"],
                                 {"docs/plan/a.md": "a\n", "docs/plan/b.md": "b\n"})
        ok, message, _ = verify(repo, sha)
        assert not ok
        assert "1 required path(s) not named" in message

    def test_content_changed_after_review_fails(self, repo: Path, tmp_path: Path) -> None:
        _stage(repo, {"docs/plan/a.md": "reviewed text\n"})
        artifact, trailers = record(_verdict(tmp_path, ["docs/plan/a.md"]), "PASS", "reviewer", repo)
        _stage(repo, {"docs/plan/a.md": "edited after the review\n"})
        git(repo, "add", str(artifact.relative_to(repo)))
        git(repo, "commit", "-q", "-m", f"reviewed change\n\n{trailers}")
        sha = git(repo, "rev-parse", "HEAD").stdout.strip()
        ok, message, _ = verify(repo, sha)
        assert not ok
        assert "1 named at different content" in message

    def test_verdict_replayed_onto_a_later_commit_fails(self, repo: Path, tmp_path: Path) -> None:
        first = _record_and_commit(repo, tmp_path, ["docs/plan/a.md"], {"docs/plan/a.md": "v1\n"})
        trailer_block = git(repo, "log", "-1", "--format=%B", first).stdout.split("\n\n", 1)[1]
        commit_files(repo, "replay", {"docs/plan/a.md": "v2\n"}, body=trailer_block.strip())
        second = git(repo, "rev-parse", "HEAD").stdout.strip()
        assert verify(repo, first)[0]
        assert not verify(repo, second)[0]

    def test_deletion_is_covered_by_a_null_binding(self, repo: Path, tmp_path: Path) -> None:
        commit_files(repo, "seed", {"docs/plan/old.md": "old\n"})
        sha = _record_and_commit(repo, tmp_path, ["docs/plan/old.md"], {"docs/plan/old.md": None})
        assert verify(repo, sha)[0]

    def test_rename_needs_both_paths(self, repo: Path, tmp_path: Path) -> None:
        commit_files(repo, "seed", {"docs/plan/old.md": "same\n"})
        git(repo, "mv", "docs/plan/old.md", "docs/plan/new.md")
        artifact, trailers = record(_verdict(tmp_path, ["docs/plan/new.md"]), "PASS", "reviewer", repo)
        git(repo, "add", str(artifact.relative_to(repo)))
        git(repo, "commit", "-q", "-m", f"rename\n\n{trailers}")
        only_new = git(repo, "rev-parse", "HEAD").stdout.strip()
        assert not verify(repo, only_new)[0]

        git(repo, "mv", "docs/plan/new.md", "docs/plan/newer.md")
        sha = _record_and_commit(repo, tmp_path, ["docs/plan/new.md", "docs/plan/newer.md"], {})
        assert verify(repo, sha)[0]

    def test_root_commit_paths_are_all_guarded_candidates(self, tmp_path: Path) -> None:
        r = init_repo(tmp_path / "root")
        (r / ".claude/hooks").mkdir(parents=True)
        (r / GATE_HOOK_REL).write_text(gate_hook_source())
        (r / ".claude/settings.json").write_text(
            json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "python3 .claude/hooks/precommit_review_guard.py"}]}]}})
        )
        (r / "docs/plan").mkdir(parents=True)
        (r / "docs/plan/a.md").write_text("a\n")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "root")
        assert coverage_paths(r, "HEAD") == ("docs/plan/a.md",)

    def test_merge_commit_has_no_guarded_paths(self, repo: Path) -> None:
        git(repo, "checkout", "-q", "-b", "feat")
        commit_files(repo, "feat", {"docs/plan/a.md": "a\n"})
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", "feat", "-m", "merge")
        assert coverage_paths(repo, "HEAD") == ()

    def test_unreadable_gate_configuration_requires_every_changed_path(self, repo: Path, tmp_path: Path) -> None:
        commit_files(repo, "break hook", {GATE_HOOK_REL: "# gate\n"})
        partial = _record_and_commit(repo, tmp_path, ["docs/plan/a.md"], {"docs/plan/a.md": "a\n", "notes.txt": "n\n"})
        ok, message, _ = verify(repo, partial)
        assert not ok
        assert "1 required path(s) not named" in message
        full = _record_and_commit(repo, tmp_path, ["docs/plan/b.md", "notes2.txt"],
                                  {"docs/plan/b.md": "b\n", "notes2.txt": "n\n"})
        assert verify(repo, full)[0]

    def test_unguarded_change_needs_every_changed_path_named(self, repo: Path, tmp_path: Path) -> None:
        sha = _record_and_commit(repo, tmp_path, ["README.md"], {"README.md": "r\n", "src/x.py": "x\n"})
        ok, message, _ = verify(repo, sha)
        assert not ok
        assert "1 required path(s) not named" in message

    def test_record_about_other_files_does_not_count_on_an_unguarded_change(self, repo: Path, tmp_path: Path) -> None:
        sha = _record_and_commit(repo, tmp_path, ["docs/plan/elsewhere.md"], {"README.md": "r\n"})
        assert not verify(repo, sha)[0]

    def test_copied_trailer_on_a_later_unguarded_commit_does_not_count(self, repo: Path, tmp_path: Path) -> None:
        first = _record_and_commit(repo, tmp_path, ["docs/plan/a.md"], {"docs/plan/a.md": "a\n"})
        trailer_block = git(repo, "log", "-1", "--format=%B", first).stdout.split("\n\n", 1)[1].strip()
        later = commit_files(repo, "unguarded tweak", {"README.md": "later\n"}, body=trailer_block)
        assert verify(repo, first)[0]
        ok, message, _ = verify(repo, later)
        assert not ok
        change = next(c for c in build_bundle(repo).changes if c.change_id == later[:12])
        assert not change.review.attested
        assert change.review.verification == "claimed"
        assert change.control_outcomes["ISO-A.8.32"] != "attested"

    def test_trailer_on_a_merge_commit_never_counts(self, repo: Path, tmp_path: Path) -> None:
        first = _record_and_commit(repo, tmp_path, ["docs/plan/a.md"], {"docs/plan/a.md": "a\n"})
        trailer_block = git(repo, "log", "-1", "--format=%B", first).stdout.split("\n\n", 1)[1].strip()
        git(repo, "checkout", "-q", "-b", "feat")
        commit_files(repo, "feat", {"README.md": "f\n"})
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", "feat", "-m", f"Merge feat\n\n{trailer_block}")
        merge = git(repo, "rev-parse", "HEAD").stdout.strip()
        ok, message, _ = verify(repo, merge)
        assert not ok
        assert "no changed paths of its own" in message
        change = next(c for c in build_bundle(repo).changes if c.change_id == merge[:12])
        assert not change.review.attested

    def test_trailer_on_an_empty_commit_never_counts(self, repo: Path, tmp_path: Path) -> None:
        first = _record_and_commit(repo, tmp_path, ["docs/plan/a.md"], {"docs/plan/a.md": "a\n"})
        trailer_block = git(repo, "log", "-1", "--format=%B", first).stdout.split("\n\n", 1)[1].strip()
        git(repo, "commit", "-q", "--allow-empty", "-m", f"empty\n\n{trailer_block}")
        assert not verify(repo, "HEAD")[0]

    def test_malformed_binding_fails(self, repo: Path, tmp_path: Path) -> None:
        payload = {"verdict": "PASS", "reviewer": "reviewer", "subject": ["docs/plan/a.md"], BINDING_KEY: "nope"}
        _commit_hand_written_artifact(repo, payload, {"docs/plan/a.md": "a\n"})
        ok, message, _ = verify(repo, "HEAD")
        assert not ok
        assert "malformed" in message


# --- legacy artifacts and the pack -----------------------------------------------------


def _commit_hand_written_artifact(repo: Path, payload: dict, files: dict[str, str]) -> str:
    """Commit an artifact written without ``record`` (a pre-subject or tampered one)."""
    canonical = (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    digest = hashlib.sha256(canonical).hexdigest()
    rel = f".governor/reviews/{digest}.json"
    (repo / ".governor/reviews").mkdir(parents=True, exist_ok=True)
    (repo / rel).write_bytes(canonical)
    return commit_files(repo, "hand-written", files, body=format_trailers("PASS", "architect", f"sha256:{digest}"))


class TestLegacyAndPack:
    def test_subjectless_artifact_still_counts_with_scope_not_recorded(self, repo: Path) -> None:
        sha = _commit_hand_written_artifact(
            repo, {"verdict": "PASS", "reviewer": "architect", "deliverable": "docs/plan/a.md"},
            {"docs/plan/a.md": "a\n"},
        )
        ok, message, _ = verify(repo, sha)
        assert ok
        assert "names no reviewed files" in message
        assert resolve_pack_signal(repo, _signal(repo, sha), sha).scope == SCOPE_NOT_RECORDED

    def test_pack_counts_scope_and_downgrades_mismatch(self, repo: Path, tmp_path: Path) -> None:
        legacy = _commit_hand_written_artifact(
            repo, {"verdict": "PASS", "reviewer": "architect"}, {"docs/plan/legacy.md": "l\n"}
        )
        confirmed = _record_and_commit(repo, tmp_path, ["docs/plan/ok.md"], {"docs/plan/ok.md": "ok\n"})
        mismatched = _record_and_commit(repo, tmp_path, ["docs/plan/other.md"], {"docs/plan/bad.md": "bad\n"})

        bundle = build_bundle(repo)
        by_id = {c.change_id: c for c in bundle.changes}
        assert by_id[legacy[:12]].review.attested and by_id[legacy[:12]].review.scope == "not-recorded"
        assert by_id[confirmed[:12]].review.attested and by_id[confirmed[:12]].review.scope == "confirmed"
        bad = by_id[mismatched[:12]]
        assert not bad.review.attested
        assert bad.review.verification == "claimed"
        assert bad.policy_gates[0].outcome == "no-recorded-evidence"

        signal = resolve_pack_signal(repo, _signal(repo, mismatched), mismatched)
        assert signal.reason == REASON_SUBJECT_MISMATCH
        assert str(repo) not in signal.evidence

        rendered = render_pack(bundle)
        assert "2 change(s) carry an integrity-checked `Governor-Review` PASS trailer" in rendered["report.md"]
        assert "(1 of them name no reviewed files" in rendered["report.md"]
        for component in ("report.md", "exception-log.md", "executive-summary.md"):
            assert "verified `Governor-Review`" not in rendered[component]
        manifest = json.loads(rendered["manifest.json"])
        assert manifest["schema_version"] == "governor.evidence-pack/2"
        assert {c["review"]["verification"] for c in manifest["changes"]} >= {"integrity-checked", "claimed"}
        assert '"verified"' not in rendered["manifest.json"]
        scopes = {c["change_id"]: c["review"]["scope"] for c in manifest["changes"]}
        assert scopes[legacy[:12]] == "not-recorded"
        assert scopes[confirmed[:12]] == "confirmed"

    def test_review_on_an_unguarded_change_does_not_make_the_gate_apply(self, repo: Path, tmp_path: Path) -> None:
        sha = _record_and_commit(repo, tmp_path, ["README.md"], {"README.md": "unguarded\n"})
        change = next(c for c in build_bundle(repo).changes if c.change_id == sha[:12])
        assert change.review.attested
        gate = change.policy_gates[0]
        assert (gate.outcome, gate.applies) == ("not-applicable", False)

    def test_unreadable_gate_with_a_subjectless_record_is_undetermined(self, repo: Path) -> None:
        commit_files(repo, "break hook", {GATE_HOOK_REL: "# gate\n"})
        sha = _commit_hand_written_artifact(
            repo, {"verdict": "PASS", "reviewer": "architect"}, {"docs/plan/a.md": "a\n"}
        )
        change = next(c for c in build_bundle(repo).changes if c.change_id == sha[:12])
        assert change.policy_gates[0].outcome == "undetermined"

    def test_cli_verify_exits_1_on_mismatch(self, repo: Path, tmp_path: Path) -> None:
        _record_and_commit(repo, tmp_path, ["docs/plan/other.md"], {"docs/plan/bad.md": "bad\n"})
        proc = subprocess.run(
            [sys.executable, "-m", "kerbtap", "review", "verify", "HEAD", "--repo", str(repo)],
            cwd=TOOL_ROOT, capture_output=True, text=True,
        )
        assert proc.returncode == 1
        assert "does not cover" in proc.stdout
