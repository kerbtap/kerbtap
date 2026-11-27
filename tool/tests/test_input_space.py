"""Systematic input-space pass: every enumerated dimension of repository shape and
content the generator can meet, covered by construction.

Dimensions: unborn HEAD (zero commits), non-UTF-8 bytes, separator control characters,
unicode content, empty messages, detached HEAD, shallow clones, bad CLI paths, gh
failure modes (malformed JSON, mid-run errors), trailer-classifier precision (human
names must never be labeled AI), redaction unification (emails in every quoted path),
evidence-conditioned narratives (no birth-environment hardcodes), and the PR-enrichment
cap for very large repos.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kerbtap.assemble import (
    _has_ai_trailer,
    _pr_evidence,
    _select_prs_for_enrichment,
    build_bundle,
)
from kerbtap.build import render_pack
from kerbtap.cli import main
from kerbtap.collect import github as github_mod
from kerbtap.collect.git import collect_commits, detect_review, is_shallow
from kerbtap.review.record import record
from tests.factories import make_commit
from tests.repo_factory import (
    commit,
    commit_raw_message,
    detach_head,
    git,
    init_repo,
    merge_pr,
    shallow_clone,
    write_dogfood_log,
)


@pytest.fixture()
def no_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the git-only path so tests are deterministic and offline."""
    monkeypatch.setattr(github_mod, "gh_available", lambda: False)


# --- dimension: unborn HEAD (git init, zero commits) ---------------------------------


class TestUnbornHead:
    def test_zero_commit_repo_builds_an_honest_empty_pack(self, tmp_path: Path, no_gh: None) -> None:
        repo = init_repo(tmp_path / "empty")
        rendered = render_pack(build_bundle(repo))  # must not raise
        manifest = json.loads(rendered["manifest.json"])
        assert manifest["scope"]["assessed_commits"] == 0
        assert manifest["changes"] == []
        assert manifest["scope"]["commit_range"] == "none..none"
        assert "0 commits" in rendered["report.md"]

    def test_zero_commit_repo_collects_empty_not_error(self, tmp_path: Path) -> None:
        repo = init_repo(tmp_path / "empty")
        assert collect_commits(repo) == []


# --- dimension: hostile bytes in commit messages -------------------------------------


class TestHostileMessageBytes:
    def test_invalid_utf8_in_message_does_not_crash_collection(self, tmp_path: Path) -> None:
        repo = init_repo(tmp_path / "latin")
        commit_raw_message(repo, b"Caf\xe9 subject in latin-1\n\nBody with raw \xff\xfe bytes.")
        commits = collect_commits(repo)
        assert len(commits) == 1
        # Undecodable bytes are replaced, never fatal; the rest of the text survives.
        assert "subject in latin-1" in commits[0].subject

    def test_field_and_record_separator_bytes_cannot_drop_or_corrupt_commits(
        self, tmp_path: Path
    ) -> None:
        repo = init_repo(tmp_path / "seps")
        commit(repo, "clean one")
        commit_raw_message(
            repo,
            b"hostile subject\n\nbody with field sep \x1f and record sep \x1e then MARKER-END.",
        )
        commit(repo, "clean two")
        commits = collect_commits(repo)
        collected = {c.sha for c in commits}
        ground_truth = set(git(repo, "rev-list", "HEAD").stdout.split())
        assert collected == ground_truth  # no commit silently lost
        hostile = next(c for c in commits if c.subject == "hostile subject")
        assert "MARKER-END" in hostile.body  # body content after the control char survives
        assert all(len(c.sha) == 40 for c in commits)  # no field-shift corruption

    def test_unicode_content_flows_through_the_pack(self, tmp_path: Path, no_gh: None) -> None:
        repo = init_repo(tmp_path / "uni")
        commit(repo, "🚀 修复 مرحبا naïve fix")
        rendered = render_pack(build_bundle(repo))
        manifest = json.loads(rendered["manifest.json"])
        assert manifest["changes"][0]["subject"] == "🚀 修复 مرحبا naïve fix"

    def test_empty_commit_message_renders(self, tmp_path: Path, no_gh: None) -> None:
        repo = init_repo(tmp_path / "nomsg")
        (repo / "a.txt").write_text("a", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "--allow-empty-message", "-m", "")
        rendered = render_pack(build_bundle(repo))
        assert json.loads(rendered["manifest.json"])["scope"]["assessed_commits"] == 1


# --- dimension: repository states (detached HEAD, shallow clone) ---------------------


class TestRepositoryStates:
    def test_detached_head_collects_reachable_history(self, tmp_path: Path, no_gh: None) -> None:
        repo = init_repo(tmp_path / "detached")
        commit(repo, "first")
        commit(repo, "second")
        detach_head(repo)
        manifest = json.loads(render_pack(build_bundle(repo))["manifest.json"])
        assert manifest["scope"]["assessed_commits"] == 2

    def test_shallow_clone_is_detected_and_never_claims_full_history(
        self, tmp_path: Path, no_gh: None
    ) -> None:
        src = init_repo(tmp_path / "src")
        commit(src, "one")
        commit(src, "two")
        commit(src, "three")
        clone = shallow_clone(src, tmp_path / "clone", depth=1)
        assert is_shallow(clone) is True
        assert is_shallow(src) is False

        rendered = render_pack(build_bundle(clone))
        manifest = json.loads(rendered["manifest.json"])
        assert manifest["scope"]["assessed_commits"] == 1
        git_system = next(
            s for s in manifest["source_systems"] if s["name"] == "git version control"
        )
        assert "shallow" in git_system["evidence"].lower()
        # The full-history claim must not appear anywhere in a shallow-clone pack.
        for text in rendered.values():
            assert "Full commit history" not in text


# --- dimension: bad CLI inputs --------------------------------------------------------


class TestCliInputs:
    def test_path_is_a_regular_file_fails_cleanly(self, tmp_path: Path) -> None:
        f = tmp_path / "not-a-repo.txt"
        f.write_text("hello", encoding="utf-8")
        assert main(["generate", str(f), "-o", str(tmp_path / "out")]) == 2

    def test_nonexistent_path_fails_cleanly(self, tmp_path: Path) -> None:
        assert main(["generate", str(tmp_path / "missing")]) == 2

    def test_directory_that_is_not_a_repo_fails_cleanly(self, tmp_path: Path) -> None:
        d = tmp_path / "plain-dir"
        d.mkdir()
        assert main(["generate", str(d), "-o", str(tmp_path / "out")]) == 2


# --- dimension: gh failure modes ------------------------------------------------------


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


class TestGhFailureModes:
    def test_malformed_pr_list_json_degrades_to_git_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(github_mod, "gh_available", lambda: True)
        monkeypatch.setattr(
            github_mod.subprocess, "run", lambda *a, **k: _FakeCompleted("this is not json {")
        )
        assert github_mod.collect_pull_requests(tmp_path) == []

    def test_pr_view_failing_mid_run_skips_only_that_pr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(github_mod, "gh_available", lambda: True)
        good = json.dumps(
            {"number": 8, "title": "ok", "state": "MERGED", "body": "fine.",
             "reviews": [], "author": {"login": "a"}, "mergedBy": {"login": "b"},
             "mergeCommit": {"oid": "abc"}, "baseRefName": "main", "headRefName": "feat/x",
             "mergedAt": "2026-01-01T00:00:00Z"}
        )

        def fake_run(argv, **kwargs):
            number = argv[3]
            if number == "7":
                raise subprocess.CalledProcessError(1, argv, stderr="rate limited")
            return _FakeCompleted(good)

        monkeypatch.setattr(github_mod.subprocess, "run", fake_run)
        details = github_mod.collect_pr_details(tmp_path, [7, 8])
        assert set(details) == {8}

    def test_pr_view_body_email_is_redacted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(github_mod, "gh_available", lambda: True)
        payload = json.dumps(
            {"number": 5, "title": "CTA", "state": "MERGED",
             "body": "Onboard teams via hello@vendor.ai for access.",
             "reviews": [], "author": {"login": "a"}, "mergedBy": None,
             "mergeCommit": {}, "baseRefName": "main", "headRefName": "feat/cta",
             "mergedAt": None}
        )
        monkeypatch.setattr(github_mod.subprocess, "run", lambda *a, **k: _FakeCompleted(payload))
        details = github_mod.collect_pr_details(tmp_path, [5])
        assert "hello@vendor.ai" not in details[5]["body_excerpt"]
        assert "[email-redacted]" in details[5]["body_excerpt"]


# --- dimension: trailer-classifier precision -----------------------------------------
# 7A honesty asymmetry: prefer missing an AI trailer (under-claim) to labeling a human
# co-author as AI (a false claim about a person).


class TestTrailerPrecision:
    @pytest.mark.parametrize(
        "trailer",
        [
            # substring traps: gpt in egypt, claude in Claudette, human Cody/Devin/Cursor
            "Co-authored-by: Ahmed Hassan <ahmed@egypt-tours.com>",
            "Co-authored-by: Claudette Martin <claudette@corp.com>",
            "Co-authored-by: Cody Smith <cody.smith@corp.com>",
            "Co-authored-by: Devin Brown <devin.brown@corp.com>",
            "Co-authored-by: Claude Monet <claude.monet@gmail.com>",
            "Co-authored-by: Gemma Cursor <gemma.cursor@corp.com>",
            # bare human given name that is also an AI product name: needs the email tier
            "Co-authored-by: Claude <claude@familyname.fr>",
            # HUMAN EMPLOYEES at AI companies: a corporate domain is never decisive
            "Co-authored-by: Jane Doe <jane@sourcegraph.com>",
            "Co-authored-by: John Smith <john.smith@anthropic.com>",
            "Co-authored-by: Priya Rao <priya@openai.com>",
            "Co-authored-by: Sam Lee <sam@cursor.com>",
            # suffix collisions: the WHOLE email must be the service address
            "Co-authored-by: GH User <12345+notcopilot@users.noreply.github.com>",
            "Co-authored-by: Sales Agent <sales-agent@cursor.com>",
            "Co-authored-by: X <xnoreply@anthropic.com>",
            # bots stay excluded
            "Co-authored-by: dependabot[bot] <support@github.com>",
            # malformed trailer: no crash, no claim
            "Co-authored-by:",
        ],
    )
    def test_humans_bots_and_malformed_are_never_ai(self, trailer: str) -> None:
        assert _has_ai_trailer(make_commit(body=trailer)) is False

    @pytest.mark.parametrize(
        "trailer",
        [
            "Co-Authored-By: Claude <noreply@anthropic.com>",
            "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>",
            "Co-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>",
            "Co-authored-by: GitHub Copilot <copilot@github.com>",
            "Co-authored-by: ChatGPT <chatgpt@openai.com>",
            "Co-authored-by: Cursor Agent <agent@cursor.com>",
            "Co-authored-by: Sourcegraph Cody <cody@sourcegraph.com>",
            "Co-authored-by: Devin AI <devin-ai-integration@devin.ai>",
            "Co-authored-by: Клод <noreply@anthropic.com>",  # unicode name, decisive email
            "Co-authored-by: Copilot",  # tool name with no email at all
            # aider's canonical trailer shape (model in parens + service address)
            "Co-authored-by: aider (gpt-4o) <aider@aider.chat>",
            # AI coding agents shipped as GitHub apps are AI authorship, not automation
            "Co-authored-by: devin-ai-integration[bot] <158243242+devin-ai-integration[bot]@users.noreply.github.com>",
            "Co-authored-by: google-labs-jules[bot] <161369871+google-labs-jules[bot]@users.noreply.github.com>",
        ],
    )
    def test_real_ai_trailers_are_detected(self, trailer: str) -> None:
        assert _has_ai_trailer(make_commit(body=trailer)) is True


# --- dimension: redaction unification (emails in EVERY quoted path) ------------------


class TestRedactionUnification:
    def test_free_text_review_claim_does_not_leak_body_text(self) -> None:
        # A free-text claim is not quoted into evidence at all — the evidence is controlled
        # reason-derived prose, so an email in the body cannot leak into the pack.
        body = (
            "Adversarial review: architect C1-C7, re-review PASS. "
            "Escalations go to reviewer bob@corp.com directly."
        )
        signal = detect_review(make_commit(body=body))
        assert signal.attested is False
        assert signal.verification == "claimed"
        assert "bob@corp.com" not in signal.evidence

    def test_malformed_trailer_verdict_value_is_dropped_not_stored(self) -> None:
        # A hostile footer can put an absolute path in the Governor-Review value. It is an
        # unrecognized verdict, so it is dropped (verdict=None) rather than stored raw —
        # nothing repo-sourced reaches evidence, by construction.
        body = (
            "subject\n\n"
            "Governor-Review: /Users/alice/private/notes\n"
            f"Governor-Review-Ref: sha256:{'a' * 64}"
        )
        signal = detect_review(make_commit(body=body))
        assert signal.verification == "claimed"
        assert signal.verdict is None
        assert "/Users/alice" not in signal.evidence
        assert "/Users/alice" not in (signal.reviewer or "")

    def test_reviewer_field_is_redacted_at_the_pack_boundary(self) -> None:
        # reviewer is the ONE raw commit-authored field; it must be redacted in the pack.
        body = (
            "subject\n\n"
            "Governor-Review: PASS\n"
            "Governor-Reviewer: /Users/x/dir contact bob@corp.com\n"
            f"Governor-Review-Ref: sha256:{'a' * 64}"
        )
        signal = detect_review(make_commit(body=body))
        assert "/Users/x" not in (signal.reviewer or "")
        assert "bob@corp.com" not in (signal.reviewer or "")

    def test_author_name_containing_email_is_redacted_in_external_pack(
        self, tmp_path: Path, no_gh: None
    ) -> None:
        repo = init_repo(tmp_path / "authors")
        commit(repo, "change", author="bob@corp.com <bob@corp.com>")
        rendered = render_pack(build_bundle(repo, internal=False))
        for filename, text in rendered.items():
            assert "bob@corp.com" not in text, f"author email leaked into {filename}"


# --- dimension: evidence-conditioned narratives (no birth-environment hardcodes) -----


class TestNarrativeConditioning:
    def test_direct_commits_predating_prs_get_the_evidence_derived_sentence(
        self, tmp_path: Path, no_gh: None
    ) -> None:
        repo = init_repo(tmp_path / "early")
        commit(repo, "founding work", date="2026-01-01T10:00:00 +0000")
        merge_pr(repo, 1, "feat/a", "feature one", date="2026-02-01T10:00:00 +0000")
        log = render_pack(build_bundle(repo))["exception-log.md"]
        assert "predate the repository's first pull-request-landed change" in log

    def test_direct_only_repo_gets_no_vacuous_remaining_changes_sentence(
        self, tmp_path: Path, no_gh: None
    ) -> None:
        repo = init_repo(tmp_path / "directonly")
        commit(repo, "one")
        commit(repo, "two")
        log = render_pack(build_bundle(repo))["exception-log.md"]
        assert "2 of 2 assessed commits landed directly" in log
        assert "The remaining changes landed through pull requests" not in log

    def test_direct_commit_newer_than_prs_gets_no_founding_story(
        self, tmp_path: Path, no_gh: None
    ) -> None:
        repo = init_repo(tmp_path / "late")
        commit(repo, "founding work", date="2026-01-01T10:00:00 +0000")
        merge_pr(repo, 1, "feat/a", "feature one", date="2026-02-01T10:00:00 +0000")
        commit(repo, "hotfix straight to main", date="2026-03-01T10:00:00 +0000")
        log = render_pack(build_bundle(repo))["exception-log.md"]
        assert "predate" not in log
        assert "founding/setup" not in log

    def test_foreign_dogfood_log_renders_its_own_observations_not_governors(
        self, tmp_path: Path, no_gh: None
    ) -> None:
        repo = init_repo(tmp_path / "foreign")
        write_dogfood_log(
            repo,
            [("O1", "We forgot tests on the deploy path."),
             ("O2", "Docs drifted from reality.")],
        )
        commit(repo, "add dogfood log")
        rendered = render_pack(build_bundle(repo))
        log = rendered["exception-log.md"]
        assert "We forgot tests on the deploy path." in log
        assert "Docs drifted from reality." in log
        # kerbtap-specific hardcodes must not be asserted about a foreign repo.
        assert "GOVERNOR_REVIEWED" not in log
        assert "DUMMY" not in log
        remediation = rendered["remediation-plan.md"]
        assert "GOVERNOR_REVIEWED" not in remediation
        assert "sprint" not in remediation.lower()  # O8 story needs an O8-shaped observation
        assert "self-report" in remediation  # generic self-reported-observations item instead

    def test_governor_shaped_observations_still_get_curated_remediations(
        self, tmp_path: Path, no_gh: None
    ) -> None:
        repo = init_repo(tmp_path / "govshape")
        write_dogfood_log(
            repo,
            [("O4", "The self-attestation bypass leaves no evidence artifact."),
             ("O11", "The review attestation is not the merge authorization.")],
        )
        commit(repo, "add dogfood log")
        remediation = render_pack(build_bundle(repo))["remediation-plan.md"]
        assert "kerbtap review record" in remediation  # O4-matched curated fix
        assert "merge authorization" in remediation  # O11-matched curated fix
        # Only the id that actually matched is named — O12 was not reported.
        assert "(dogfood O11)" in remediation
        assert "O11/O12" not in remediation

    def test_keyword_coincidence_does_not_inherit_governors_story(
        self, tmp_path: Path, no_gh: None
    ) -> None:
        """Foreign observations sharing an id and ONE common word must not receive the
        curated repo-of-origin remediation (the false-claim path the token gate blocks)."""
        repo = init_repo(tmp_path / "coincidence")
        write_dogfood_log(
            repo,
            [("O4", "We keep losing evidence screenshots in Slack."),
             ("O11", "We merge PRs without waiting for CI.")],
        )
        commit(repo, "add dogfood log")
        remediation = render_pack(build_bundle(repo))["remediation-plan.md"]
        assert "GOVERNOR_REVIEWED" not in remediation
        assert "verbal grant" not in remediation
        assert "self-reports 2 process observation(s)" in remediation  # generic fallback

    def test_git_only_report_never_claims_merge_actors_were_recorded(
        self, tmp_path: Path, no_gh: None
    ) -> None:
        repo = init_repo(tmp_path / "gitonly")
        commit(repo, "initial")
        merge_pr(repo, 1, "feat/a", "feature work")
        report = render_pack(build_bundle(repo))["report.md"]
        assert "merge actor is recorded" not in report
        assert "merge actors were not collected this run" in report

    def test_dogfood_headline_with_pipe_cannot_break_the_table(
        self, tmp_path: Path, no_gh: None
    ) -> None:
        repo = init_repo(tmp_path / "pipes")
        write_dogfood_log(repo, [("O1", "Weird | pipe in headline.")])
        commit(repo, "add dogfood log")
        log = render_pack(build_bundle(repo))["exception-log.md"]
        assert "Weird \\| pipe in headline." in log

    def test_report_never_asserts_owner_merge_convention_from_log_presence(
        self, tmp_path: Path, no_gh: None
    ) -> None:
        repo = init_repo(tmp_path / "ownermerge")
        write_dogfood_log(repo, [("O1", "An observation.")])
        commit(repo, "add dogfood log")
        report = render_pack(build_bundle(repo))["report.md"]
        assert "per the dogfood log" not in report


# --- dimension: very large repos (PR-enrichment cap) ----------------------------------


class TestEnrichmentCap:
    def test_selection_keeps_most_recent_and_names_the_capped(self) -> None:
        numbers = list(range(1, 301))  # 300 referenced PRs
        selected, capped = _select_prs_for_enrichment(numbers, limit=150)
        assert len(selected) == 150
        assert min(selected) == 151  # the most recent 150
        assert capped == set(range(1, 151))

    def test_no_cap_when_under_limit(self) -> None:
        selected, capped = _select_prs_for_enrichment([3, 1, 2, 3], limit=150)
        assert selected == [3, 2, 1]
        assert capped == set()

    def test_capped_pr_record_carries_an_honest_note(self) -> None:
        ev = _pr_evidence(
            landing="squash-pr", pr_number=12, pr_details={}, gh_up=True, capped={12}
        )
        assert ev is not None
        assert "capped" in str(ev["collection"])
        assert ev["merge_method_inferred"] == "squash"


class TestRecordedTrailerEndToEnd:
    """The pack and `kerbtap review verify` must agree on a real recorded commit.

    Regression: the collector reads git `%b` (body without the post-subject blank line),
    so a commit made as `git commit -m subject -m "<trailers>"` has a trailer block with no
    leading blank. Classifying `%b` alone would drop it (`none`) while `verify` (reads `%B`)
    passes — a pack/CLI split-brain on the main path.
    """

    def test_pack_attests_a_real_recorded_body_only_trailer_commit(
        self, tmp_path: Path, no_gh: None
    ) -> None:
        repo = init_repo(tmp_path / "rec")
        # `commit` below writes and stages f0.txt with this exact content; stage it first so
        # the record binds the content that lands. The verdict file lives outside the repo.
        (repo / "f0.txt").write_text("content 0\n", encoding="utf-8")
        git(repo, "add", "f0.txt")
        verdict = tmp_path / "verdict.json"
        verdict.write_text(
            json.dumps({"verdict": "PASS", "reviewer": "architect", "summary": "ok", "subject": ["f0.txt"]})
        )
        _artifact, trailers = record(verdict, "PASS", "architect", repo)
        # `commit` uses `-m subject -m body`, so %b == the trailer block with no leading blank.
        commit(repo, "reviewed deliverable", body=trailers)

        bundle = build_bundle(repo)
        attested = [c for c in bundle.changes if c.review.attested]
        assert attested, "a real recorded-trailer commit must be attested by the pack"
        assert all(c.review.verification == "integrity-checked" for c in attested)
