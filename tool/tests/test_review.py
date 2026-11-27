"""Tests for evidence-bound review attestation: trailer parsing, record, and verify."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import os

from kerbtap.review.record import (
    ReviewError,
    record,
    resolve_pack_signal,
    verify,
    verify_artifact,
)
from kerbtap.review.trailer import (
    INTEGRITY_CHECKED,
    ReviewSignal,
    classify_review,
    format_trailers,
    is_valid_ref,
    parse_trailers,
)
from kerbtap.state import state_rel

_HEX64 = "a" * 64
_REF = f"sha256:{_HEX64}"


def _verified_body(ref: str = _REF, verdict: str = "PASS", reviewer: str = "architect") -> str:
    return f"subject\n\nGovernor-Review: {verdict}\nGovernor-Reviewer: {reviewer}\nGovernor-Review-Ref: {ref}"


# --- trailer grammar ----------------------------------------------------------------


class TestParseAndValidate:
    def test_parses_trailer_lines_only(self) -> None:
        body = "Fix: the thing.\n\nGovernor-Review: PASS\nGovernor-Review-Ref: " + _REF
        parsed = parse_trailers(body)
        assert parsed["Governor-Review"] == "PASS"
        assert parsed["Governor-Review-Ref"] == _REF
        assert "Fix" not in parsed  # prose "Key: value" is not a Governor trailer

    def test_last_value_wins(self) -> None:
        body = "subject\n\nGovernor-Review: NEEDS-CHANGES\nGovernor-Review: PASS"
        assert parse_trailers(body)["Governor-Review"] == "PASS"

    @pytest.mark.parametrize(
        "ref,ok",
        [
            (_REF, True),
            (f"sha256:{'A' * 64}", False),  # uppercase hex rejected
            (f"sha256:{'a' * 63}", False),  # too short
            ("sha1:" + "a" * 40, False),  # wrong algo
            (None, False),
        ],
    )
    def test_ref_validation(self, ref: str | None, ok: bool) -> None:
        assert is_valid_ref(ref) is ok


class TestClassifyReview:
    def test_verified(self) -> None:
        sig = classify_review(_verified_body())
        assert (sig.verification, sig.attested, sig.verdict, sig.ref) == ("integrity-checked", True, "PASS", _REF)

    def test_prose_only_is_claimed(self) -> None:
        sig = classify_review("did an adversarial review, verdict PASS")
        assert sig.verification == "claimed"
        assert sig.attested is False

    def test_pass_trailer_with_bad_ref_is_claimed(self) -> None:
        sig = classify_review(_verified_body(ref="sha256:nope"))
        assert sig.verification == "claimed"
        assert sig.attested is False

    def test_needs_changes_trailer_is_claimed_not_attested(self) -> None:
        sig = classify_review(_verified_body(verdict="NEEDS-CHANGES"))
        assert sig.verification == "claimed"
        assert sig.attested is False

    def test_override_is_recorded(self) -> None:
        sig = classify_review("bypassed with GOVERNOR_REVIEWED=1")
        assert sig.verification == "override"
        assert sig.attested is False

    def test_renamed_override_is_recorded_only_when_named(self) -> None:
        body = "bypassed with ACME_REVIEWED=1"
        assert classify_review(body).verification == "none"
        assert classify_review(body, ("GOVERNOR_REVIEWED", "ACME_REVIEWED")).verification == "override"

    def test_override_names_that_are_not_identifiers_are_ignored(self) -> None:
        # A name read from repository content must never act as a pattern.
        assert classify_review("anything at all", (".*",)).verification == "none"
        assert classify_review("ACMEXREVIEWED", ("ACME.REVIEWED",)).verification == "none"

    def test_none(self) -> None:
        assert classify_review("routine chore").verification == "none"

    def test_verified_trailer_block_in_footer(self) -> None:
        body = "subject\n\nfixed the thing\n\n" + format_trailers("PASS", "architect", _REF)
        assert classify_review(body).verification == "integrity-checked"

    def test_trailer_lines_in_prose_paragraph_are_not_parsed(self) -> None:
        # A Governor-Review block quoted mid-body (followed by prose) is not the footer.
        body = (
            "subject\n\n"
            "Example of a trailer:\n"
            "Governor-Review: PASS\n"
            "Governor-Review-Ref: " + _REF + "\n"
            "...but this is just documentation, not the real footer.\n"
        )
        assert classify_review(body).verification != "integrity-checked"

    def test_trailer_block_with_a_non_trailer_line_is_rejected(self) -> None:
        body = "subject\n\nGovernor-Review: PASS\nnot a trailer line\nGovernor-Review-Ref: " + _REF
        assert classify_review(body).verification != "integrity-checked"

    def test_no_blank_separator_before_block_is_not_a_footer(self) -> None:
        # subject glued to the trailer with no blank line — git parses no trailers here.
        body = f"feat: add thing\nGovernor-Review: PASS\nGovernor-Review-Ref: {_REF}"
        assert classify_review(body).verification != "integrity-checked"

    def test_git_continuation_lines_do_not_break_the_footer(self) -> None:
        # A whitespace-continuation line is valid git trailer syntax; the block must survive.
        body = (
            "subject\n\n"
            "Governor-Review: PASS\n"
            "Governor-Reviewer: architect\n"
            "  (continued reviewer note)\n"
            f"Governor-Review-Ref: {_REF}"
        )
        assert classify_review(body).verification == "integrity-checked"

    def test_continuation_on_verdict_is_folded_and_fails_closed(self) -> None:
        # git folds "PASS\n  NEEDS-CHANGES" into "PASS NEEDS-CHANGES" — not an exact PASS.
        body = f"subject\n\nGovernor-Review: PASS\n  NEEDS-CHANGES\nGovernor-Review-Ref: {_REF}"
        assert classify_review(body).verification != "integrity-checked"

    def test_continuation_on_ref_is_folded_and_fails_closed(self) -> None:
        # A continuation on the ref makes the folded value not an exact sha256:<64hex>.
        body = f"subject\n\nGovernor-Review: PASS\nGovernor-Review-Ref: {_REF}\n  /Users/alice/x"
        assert classify_review(body).verification != "integrity-checked"


class TestFormatTrailers:
    def test_round_trips_through_classify(self) -> None:
        block = format_trailers("PASS", "architect", _REF)
        sig = classify_review(f"subject\n\n{block}")
        assert sig.attested is True and sig.reviewer == "architect"

    def test_rejects_bad_verdict(self) -> None:
        with pytest.raises(ValueError):
            format_trailers("LGTM", "architect", _REF)

    def test_rejects_bad_ref(self) -> None:
        with pytest.raises(ValueError):
            format_trailers("PASS", "architect", "not-a-ref")


# --- record / verify (repo-aware) ---------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    return r


def _write_verdict(path: Path, subject: list[str] | None = None) -> Path:
    path.write_text(json.dumps(
        {"verdict": "PASS", "reviewer": "architect", "summary": "ok", "subject": subject or ["v.json"]}
    ))
    return path


def _commit(repo: Path, message: str) -> str:
    """Stage everything and commit with ``message``; return the new HEAD sha."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


class TestRecord:
    def test_writes_content_addressed_artifact_and_valid_trailer(self, repo: Path) -> None:
        vf = _write_verdict(repo / "v.json")
        artifact, trailers = record(vf, "PASS", "architect", repo)
        assert artifact.parent == repo / state_rel(repo, "reviews")
        assert artifact.name.endswith(".json")
        # the printed trailer must classify as verified
        assert classify_review(f"x\n\n{trailers}").attested is True
        # filename stem is the ref digest
        assert trailers.splitlines()[-1].endswith(artifact.stem)

    def test_is_idempotent_for_same_content(self, repo: Path) -> None:
        vf = _write_verdict(repo / "v.json")
        a1, t1 = record(vf, "PASS", "architect", repo)
        a2, t2 = record(vf, "PASS", "architect", repo)
        assert a1 == a2 and t1 == t2

    def test_rejects_non_json_output(self, repo: Path) -> None:
        bad = repo / "bad.json"
        bad.write_text("not json{")
        with pytest.raises(ReviewError):
            record(bad, "PASS", "architect", repo)

    def test_rejects_bad_verdict(self, repo: Path) -> None:
        vf = _write_verdict(repo / "v.json")
        with pytest.raises(ReviewError):
            record(vf, "LGTM", "architect", repo)

    def test_missing_output_file(self, repo: Path) -> None:
        with pytest.raises(ReviewError):
            record(repo / "nope.json", "PASS", "architect", repo)

    def test_rejects_artifact_verdict_not_matching_trailer_verdict(self, repo: Path) -> None:
        # artifact says NEEDS-CHANGES but the caller asks for a PASS trailer → refuse.
        bad = repo / "v.json"
        bad.write_text(json.dumps({"verdict": "NEEDS-CHANGES", "reviewer": "architect", "subject": ["v.json"]}))
        with pytest.raises(ReviewError):
            record(bad, "PASS", "architect", repo)

    def test_rejects_symlinked_reviews_dir(self, repo: Path, tmp_path: Path) -> None:
        # A symlinked reviews directory could redirect storage outside the repo.
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / ".governor").mkdir()
        os.symlink(outside, repo / ".governor" / "reviews")
        vf = _write_verdict(repo / "v.json")
        with pytest.raises(ReviewError):
            record(vf, "PASS", "architect", repo)


class TestPackSignalResolution:
    """resolve_pack_signal attests only from an artifact present in the ATTESTED COMMIT'S
    TREE — never from trailer syntax alone, and never from the working-tree checkout."""

    def _verified_signal(self, ref: str = _REF) -> ReviewSignal:
        return classify_review(f"x\n\n{format_trailers('PASS', 'architect', ref)}")

    def test_committed_artifact_stays_verified(self, repo: Path) -> None:
        vf = _write_verdict(repo / "v.json")
        _git(repo, "add", "v.json")  # the reviewed content is staged before recording
        _artifact, trailers = record(vf, "PASS", "architect", repo)
        sha = _commit(repo, f"reviewed\n\n{trailers}")
        resolved = resolve_pack_signal(repo, classify_review(f"x\n\n{trailers}"), sha)
        assert resolved.verification == INTEGRITY_CHECKED and resolved.attested is True

    def test_artifact_missing_from_commit_tree_downgrades(self, repo: Path) -> None:
        (repo / "f").write_text("x")
        sha = _commit(repo, f"reviewed\n\n{format_trailers('PASS', 'architect', _REF)}")
        resolved = resolve_pack_signal(repo, self._verified_signal(), sha)
        assert resolved.verification == "claimed" and resolved.attested is False

    def test_working_tree_only_artifact_does_not_attest(self, repo: Path) -> None:
        # The exact codex finding: an artifact present in the working checkout but NOT in
        # the attested commit's tree must not make the trailer verify.
        vf = _write_verdict(repo / "v.json")
        _artifact, trailers = record(vf, "PASS", "architect", repo)  # working-tree artifact
        (repo / "f").write_text("x")
        _git(repo, "add", "f")  # commit ONLY f — the artifact stays untracked at this commit
        _git(repo, "commit", "-q", "-m", f"reviewed\n\n{trailers}")
        sha = _git(repo, "rev-parse", "HEAD").strip()
        resolved = resolve_pack_signal(repo, classify_review(f"x\n\n{trailers}"), sha)
        assert resolved.attested is False

    def test_committed_artifact_content_mismatch_downgrades(self, repo: Path) -> None:
        vf = _write_verdict(repo / "v.json")
        artifact, trailers = record(vf, "PASS", "architect", repo)
        # Commit different content under the same (ref-named) path → tree hash != ref.
        artifact.write_text('{"verdict":"PASS","reviewer":"architect","summary":"DIFF"}')
        sha = _commit(repo, f"reviewed\n\n{trailers}")
        resolved = resolve_pack_signal(repo, classify_review(f"x\n\n{trailers}"), sha)
        assert resolved.verification == "claimed" and resolved.attested is False

    def test_pass_trailer_pointing_at_needs_changes_artifact_fails(self, repo: Path) -> None:
        # Record + commit a NEEDS-CHANGES artifact, then forge a PASS trailer for its hash.
        nc = repo / "nc.json"
        nc.write_text(json.dumps({"verdict": "NEEDS-CHANGES", "reviewer": "architect", "subject": ["nc.json"]}))
        _artifact, trailers = record(nc, "NEEDS-CHANGES", "architect", repo)
        ref = trailers.splitlines()[-1].split(": ", 1)[1]
        forged = classify_review(f"x\n\n{format_trailers('PASS', 'architect', ref)}")
        sha = _commit(repo, "c\n\n" + format_trailers("PASS", "architect", ref))
        check = verify_artifact(repo, forged, sha)
        assert check.ok is False and "verdict" in check.message
        assert resolve_pack_signal(repo, forged, sha).attested is False

    def test_non_verified_signal_passes_through_unchanged(self, repo: Path) -> None:
        # Portability: a repo with no trailer/artifact is untouched, never crashes. The
        # commit arg is unused for a non-verified signal (early return before any git read).
        prose = classify_review("did an adversarial review, PASS")
        assert resolve_pack_signal(repo, prose, "HEAD") is prose

    def test_downgrade_evidence_is_path_free(self, repo: Path) -> None:
        # The downgrade reason flows into the external-safe manifest — no absolute path.
        (repo / "f").write_text("x")
        sha = _commit(repo, f"reviewed\n\n{format_trailers('PASS', 'architect', _REF)}")
        resolved = resolve_pack_signal(repo, self._verified_signal(), sha)
        assert str(repo) not in resolved.evidence
        assert "/Users/" not in resolved.evidence and "/private/" not in resolved.evidence


class TestVerify:
    def test_verified_commit_passes(self, repo: Path) -> None:
        vf = _write_verdict(repo / "v.json")
        _git(repo, "add", "v.json")  # the reviewed content is staged before recording
        _artifact, trailers = record(vf, "PASS", "architect", repo)
        _commit(repo, f"reviewed change\n\n{trailers}")
        ok, msg, sig = verify(repo, "HEAD")
        assert ok is True and sig.verification == "integrity-checked"

    def test_prose_only_commit_fails(self, repo: Path) -> None:
        (repo / "f").write_text("x")
        _commit(repo, "reviewed change\n\nadversarial review: PASS")
        ok, msg, sig = verify(repo, "HEAD")
        assert ok is False and sig.verification == "claimed"

    def test_missing_artifact_fails(self, repo: Path) -> None:
        (repo / "f").write_text("x")
        _commit(repo, f"reviewed change\n\n{format_trailers('PASS', 'architect', _REF)}")
        ok, msg, _sig = verify(repo, "HEAD")
        assert ok is False and "not present" in msg

    def test_committed_artifact_content_mismatch_fails(self, repo: Path) -> None:
        vf = _write_verdict(repo / "v.json")
        artifact, trailers = record(vf, "PASS", "architect", repo)
        artifact.write_text('{"verdict":"PASS","reviewer":"architect","summary":"DIFF"}')
        _commit(repo, f"reviewed change\n\n{trailers}")
        ok, msg, _sig = verify(repo, "HEAD")
        assert ok is False and "mismatch" in msg
