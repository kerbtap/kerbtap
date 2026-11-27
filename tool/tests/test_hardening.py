"""Tests for the six external-readiness hardening fixes."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from kerbtap import __version__
from kerbtap.assemble import _pr_evidence, build_bundle
from kerbtap.build import PackWriteError, generate_pack, render_pack
from kerbtap.build.manifest import render as render_manifest
from kerbtap.collect.governance import parse_dogfood_observations
from kerbtap.honesty import (
    BARRED_TERMS,
    assert_clean,
    neutralize_barred,
    redact_quoted,
    scrub_emails,
    scrub_paths,
)
from kerbtap.textutil import ends_cleanly
from tests.factories import make_bundle

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w][\w.-]*")
# Dangling connectors that betray a mid-sentence truncation ending in a stray period.
_DANGLING = re.compile(r"\b(were|and|not by|to|of|the|a|by|with|as)\.\s*$", re.IGNORECASE)


# --- #1 no mid-sentence truncation --------------------------------------------------


def test_rendered_review_evidence_ends_cleanly() -> None:
    manifest = json.loads(render_manifest(make_bundle()))
    for change in manifest["changes"]:
        evidence = change["review"]["evidence"]
        assert ends_cleanly(evidence), evidence
        assert not _DANGLING.search(evidence), f"dangling truncation: {evidence!r}"


def test_dogfood_parser_carries_full_lead_sentence(tmp_path: Path) -> None:
    log = tmp_path / "docs" / "replication" / "dogfood-log.md"
    log.parent.mkdir(parents=True)
    log.write_text(
        "# Dogfood Log\n\n"
        "**O8 — The whole program ran three sprints without ever\n"
        "starting one; deliverables were marked Done off merged PRs, and the agent\n"
        "then proposed reconstructing history.** The early signal was logged as O7.\n\n"
        "**O12 — The O11 merge tension was closed operationally by an explicit human\n"
        "sentence, not by the attestation.** This session the owner reversed it.\n",
        encoding="utf-8",
    )
    obs = parse_dogfood_observations(tmp_path)
    by_id = {o["id"]: o["headline"] for o in obs}
    assert by_id["O8"].endswith("reconstructing history.")
    assert by_id["O12"].endswith("not by the attestation.")
    for headline in by_id.values():
        assert ends_cleanly(headline)
        assert not _DANGLING.search(headline)


# --- #4 enriched PR evidence --------------------------------------------------------


def test_pr_evidence_present_for_merge_pr_when_gh_up() -> None:
    details = {34: {"number": 34, "collection": "gh pr view", "body_excerpt": "x."}}
    ev = _pr_evidence(landing="merge-pr", pr_number=34, pr_details=details, gh_up=True)
    assert ev["collection"] == "gh pr view"
    assert ev["merge_method_inferred"] == "merge-commit"


def test_pr_evidence_notes_gh_unavailable_git_only() -> None:
    ev = _pr_evidence(landing="squash-pr", pr_number=17, pr_details={}, gh_up=False)
    assert ev["collection"] == "not collected: gh unavailable"
    assert ev["merge_method_inferred"] == "squash"


def test_pr_evidence_none_for_branch_and_direct_changes() -> None:
    assert _pr_evidence(landing="pr-branch-commit", pr_number=34, pr_details={}, gh_up=True) is None
    assert _pr_evidence(landing="direct-to-main", pr_number=None, pr_details={}, gh_up=True) is None


def test_quoted_evidence_barred_terms_are_neutralized() -> None:
    quote = "The product will ship an immutable, tamper-evident, audit-grade ledger."
    cleaned, changed = neutralize_barred(quote)
    assert changed is True
    for term in ("immutable", "tamper-evident", "audit-grade"):
        assert term not in cleaned.lower()
    # A quoted excerpt that has been neutralized must now pass the honesty guard.
    assert_clean(cleaned, context="quoted-pr-body")


def test_neutralize_leaves_clean_text_untouched() -> None:
    quote = "Architect C1-C7 adversarial review: re-review PASS."
    cleaned, changed = neutralize_barred(quote)
    assert changed is False
    assert cleaned == quote


# --- ITEM B: scrub absolute paths from quoted excerpts ------------------------------


def test_scrub_posix_home_path() -> None:
    text = "See /Users/example/AppData/thing.html for details."
    cleaned, changed = scrub_paths(text)
    assert changed is True
    assert "/Users/example" not in cleaned
    assert "[path-redacted]" in cleaned


def test_scrub_windows_vscode_deeplink_path() -> None:
    text = "pdfplumber (vscode-file://vscode-app/c:/Users/example/AppData/Local/x.html) leaks."
    cleaned, changed = scrub_paths(text)
    assert changed is True
    assert "example" not in cleaned
    assert "[path-redacted]" in cleaned


def test_scrub_home_dir_path() -> None:
    cleaned, changed = scrub_paths("logs at /home/alice/project/out.log now")
    assert changed is True
    assert "/home/alice" not in cleaned


def test_scrub_leaves_ordinary_text_untouched() -> None:
    text = "Fixes #1611. Memory grows ~1.1 MiB/page in src/converter.py."
    cleaned, changed = scrub_paths(text)
    assert changed is False
    assert cleaned == text


def test_scrub_emails_redacts_quoted_address() -> None:
    text = "Onboard your team — a CTA directing teams to hello@headroomlabs.ai for access."
    cleaned, changed = scrub_emails(text)
    assert changed is True
    assert "hello@headroomlabs.ai" not in cleaned
    assert "[email-redacted]" in cleaned


def test_scrub_emails_leaves_ordinary_text_untouched() -> None:
    cleaned, changed = scrub_emails("Fixes #1611 in src/converter.py; see docs/README.md.")
    assert changed is False


def test_redact_quoted_scrubs_barred_path_and_email_together() -> None:
    text = "make it immutable; ping bob@example.com; log at /Users/example/x.log"
    cleaned, changed = redact_quoted(text)
    assert changed is True
    assert "immutable" not in cleaned
    assert "bob@example.com" not in cleaned
    assert "/Users/example" not in cleaned
    assert "[integrity-term omitted]" in cleaned
    assert "[email-redacted]" in cleaned
    assert "[path-redacted]" in cleaned


# --- #6 schema version --------------------------------------------------------------


def test_manifest_carries_schema_version_and_compatibility() -> None:
    manifest = json.loads(render_manifest(make_bundle()))
    assert manifest["schema_version"] == "governor.evidence-pack/2"
    assert "compatibility" in manifest
    assert manifest["generator"]["version"] == __version__  # generator version preserved


# --- #5 external-share hygiene (end-to-end on a throwaway git repo) ------------------


def _init_repo(path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)

    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)
    git("config", "user.name", "Test Dev")
    git("config", "user.email", "dev@example.com")
    (path / "a.txt").write_text("one", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-m", "Founding commit: initial")
    (path / "b.txt").write_text("two", encoding="utf-8")
    git("add", "b.txt")
    git("commit", "-m", "Second change")


def test_default_pack_is_external_safe(tmp_path: Path) -> None:
    repo = tmp_path / "samplerepo"
    repo.mkdir()
    _init_repo(repo)

    rendered = render_pack(build_bundle(repo, internal=False))
    for filename, text in rendered.items():
        assert _EMAIL_RE.search(text) is None, f"email leaked into {filename}"
        assert str(repo) not in text, f"absolute repo path leaked into {filename}"
        assert "/Users/" not in text and str(tmp_path) not in text

    manifest = json.loads(rendered["manifest.json"])
    assert manifest["external_safe"] is True
    assert "repo_path" not in manifest["scope"]
    for change in manifest["changes"]:
        assert "email" not in change["author"]


def test_internal_pack_includes_pii(tmp_path: Path) -> None:
    repo = tmp_path / "samplerepo"
    repo.mkdir()
    _init_repo(repo)

    rendered = render_pack(build_bundle(repo, internal=True))
    manifest = json.loads(rendered["manifest.json"])
    assert manifest["external_safe"] is False
    assert manifest["scope"]["repo_path"] == str(repo.resolve())
    assert any("email" in c["author"] for c in manifest["changes"])
    # The PII email is present somewhere in the internal pack.
    assert any(_EMAIL_RE.search(t) for t in rendered.values())


@pytest.mark.parametrize("filename", ["report.md", "manifest.json", "executive-summary.md"])
def test_no_absolute_path_in_key_default_components(tmp_path: Path, filename: str) -> None:
    repo = tmp_path / "samplerepo"
    repo.mkdir()
    _init_repo(repo)
    rendered = render_pack(build_bundle(repo, internal=False))
    assert str(repo) not in rendered[filename]


# --- symlink-safe pack writes (security audit M5) ------------------------------------


def test_generate_pack_refuses_symlinked_component(tmp_path: Path) -> None:
    """A pre-planted symlink at a component path must fail, not clobber its target."""
    target = tmp_path / "victim.json"
    target.write_text("untouched", encoding="utf-8")
    out_dir = tmp_path / "evidence-pack"
    out_dir.mkdir()
    (out_dir / "manifest.json").symlink_to(target)

    with pytest.raises(PackWriteError):
        generate_pack(make_bundle(), out_dir)
    assert target.read_text(encoding="utf-8") == "untouched"


def test_generate_pack_refuses_symlinked_out_dir(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    out_dir = tmp_path / "evidence-pack"
    out_dir.symlink_to(real)

    with pytest.raises(PackWriteError):
        generate_pack(make_bundle(), out_dir)
    assert not any(real.iterdir())


def test_generate_pack_overwrites_regular_files(tmp_path: Path) -> None:
    """Re-running generation into the same directory still works."""
    out_dir = tmp_path / "evidence-pack"
    first = generate_pack(make_bundle(), out_dir)
    second = generate_pack(make_bundle(), out_dir)
    assert first == second
    assert (out_dir / "manifest.json").is_file()
