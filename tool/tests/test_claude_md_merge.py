"""A repository's CLAUDE.md survives an install, and a model's merge is gated before it lands.

The failure that motivated all of this: `governor init --force` replaced the file that says
what a repository *is* with a scaffold template describing five hooks, with no backup.

The failure found while building it: `claude -p` is an agent, not a text transform. Handed a
merge brief inside a repository it edited CLAUDE.md itself — outside verification, outside
the backup, while this code printed "CLAUDE.md is unchanged". Both are bound below.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kerbtap.claude_md import (
    BEGIN,
    BRIEF_SENTINEL,
    END,
    NO_TOOLS_ARGS,
    MergeError,
    block_body,
    extract_document,
    governor_block,
    merge_brief,
    merge_claude_md,
    replace_block,
    run_claude,
    verify_merge,
)
from kerbtap.init import init, render_claude_md_block

DATA = Path(__file__).resolve().parent / "data"
REPO_ROOT = Path(__file__).resolve().parents[2]

ORIGINAL = """# Acme Pricing Service

Rust + Postgres service that prices quotes for the broker network.

## Architecture

Axum HTTP layer, a pricing core with no IO, and a Postgres store behind a repository trait.

## Conventions

No unwrap outside tests.
"""

BLOCK = governor_block("ACME", "| Hook | Event | Behaviour |\n|---|---|---|\n| `g.py` | PreToolUse | Denies. |")


def _good_merge() -> str:
    return replace_block(ORIGINAL, BLOCK)


# --- the verifier ------------------------------------------------------------------


def test_a_faithful_merge_passes():
    assert verify_merge(ORIGINAL, _good_merge(), BLOCK).ok


def test_a_dropped_heading_is_refused():
    proposal = _good_merge().replace("## Conventions\n\n", "")
    check = verify_merge(ORIGINAL, proposal, BLOCK)
    assert not check.ok
    assert any("headings dropped" in f for f in check.failures)


def test_a_deleted_paragraph_is_refused():
    proposal = _good_merge().replace("No unwrap outside tests.\n", "")
    check = verify_merge(ORIGINAL, proposal, BLOCK)
    assert not check.ok
    assert any("paragraph" in f for f in check.failures)


def test_a_reworded_paragraph_is_refused():
    """Rewording is loss: the brief says content may move, never change."""
    proposal = _good_merge().replace("No unwrap outside tests.", "Avoid unwrap in tests.")
    assert not verify_merge(ORIGINAL, proposal, BLOCK).ok


def test_reflowing_a_paragraph_is_accepted():
    """Whitespace is not content; a re-wrapped line must not fail the merge."""
    proposal = _good_merge().replace(
        "Axum HTTP layer, a pricing core with no IO, and a Postgres store behind a repository trait.",
        "Axum HTTP layer, a pricing core with no IO,\nand a Postgres store behind a repository trait.",
    )
    assert verify_merge(ORIGINAL, proposal, BLOCK).ok


def test_an_edited_block_is_refused():
    proposal = _good_merge().replace("| `g.py` | PreToolUse | Denies. |", "| `g.py` | PreToolUse | Warns. |")
    check = verify_merge(ORIGINAL, proposal, BLOCK)
    assert not check.ok
    assert any("byte-equal" in f for f in check.failures)


def test_a_missing_block_is_refused():
    assert not verify_merge(ORIGINAL, ORIGINAL, BLOCK).ok


def test_a_duplicated_block_is_refused():
    assert not verify_merge(ORIGINAL, _good_merge() + "\n" + BLOCK, BLOCK).ok


def test_markers_out_of_order_are_refused():
    proposal = f"{ORIGINAL}\n{END}\nnot a block\n{BEGIN}\n"
    assert not verify_merge(ORIGINAL, proposal, BLOCK).ok


def test_an_echoed_brief_is_refused():
    """A model that returns its instructions instead of a document must not land."""
    proposal = _good_merge() + f"\n{BRIEF_SENTINEL} — Acme\n"
    check = verify_merge(ORIGINAL, proposal, BLOCK)
    assert not check.ok
    assert any("merge brief itself" in f for f in check.failures)


def test_an_empty_proposal_is_refused():
    assert not verify_merge(ORIGINAL, "   \n", BLOCK).ok


def test_an_introduced_barred_term_is_refused():
    proposal = _good_merge().replace("No unwrap outside tests.", "No unwrap outside tests. The log is immutable.")
    check = verify_merge(ORIGINAL, proposal, BLOCK)
    assert not check.ok
    assert any("barred terms" in f for f in check.failures)


def test_a_barred_term_the_original_already_had_is_kept():
    original = ORIGINAL + "\nOur ledger is immutable by design.\n"
    assert verify_merge(original, replace_block(original, BLOCK), BLOCK).ok


def test_the_recorded_model_merge_verifies():
    """A real `claude -p` merge of a real file, recorded so the suite runs offline."""
    original = (DATA / "claude_md_original.md").read_text(encoding="utf-8")
    merged = (DATA / "claude_md_merged_by_model.md").read_text(encoding="utf-8")
    block = f"{BEGIN}\n{block_body(merged)}\n{END}"
    assert verify_merge(original, merged, block).ok


# --- extracting the document -------------------------------------------------------


def test_a_fenced_reply_is_unwrapped():
    assert extract_document("Here:\n```markdown\n# Doc\n```\n") == "# Doc"


def test_the_largest_fence_wins():
    reply = "```\nshort\n```\ntext\n```markdown\n# Doc\n\nmuch longer body here\n```"
    assert "# Doc" in extract_document(reply)


def test_an_unfenced_reply_is_taken_whole():
    assert extract_document("\n# Doc\n\nbody\n") == "# Doc\n\nbody"


# --- the model call ----------------------------------------------------------------


def test_the_model_runs_with_no_tools_and_outside_the_repository(monkeypatch, tmp_path: Path):
    """The agent-writes-the-file defect: the allowlist and the scratch cwd are the fix."""
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout="# Doc\n", stderr="")

    monkeypatch.setattr("kerbtap.claude_md.shutil.which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr("kerbtap.claude_md.subprocess.run", fake_run)

    run_claude("brief", tmp_path)

    for flag in NO_TOOLS_ARGS:
        assert flag in seen["cmd"], f"{flag} missing — the merge could edit files"
    assert Path(seen["cwd"]) != tmp_path, "the model was run inside the repository"
    assert seen["input"] == "brief"


def test_a_missing_claude_cli_writes_nothing(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("kerbtap.claude_md.shutil.which", lambda _: None)
    with pytest.raises(MergeError, match="not on PATH"):
        run_claude("brief", tmp_path)


def test_a_failing_claude_cli_is_reported(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("kerbtap.claude_md.shutil.which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr(
        "kerbtap.claude_md.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="auth required"),
    )
    with pytest.raises(MergeError, match="auth required"):
        run_claude("brief", tmp_path)


# --- the merge ---------------------------------------------------------------------


def _repo_with_claude_md(tmp_path: Path, text: str = ORIGINAL) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text(text, encoding="utf-8")
    return repo


def test_a_verified_merge_is_applied_with_a_backup(tmp_path: Path):
    repo = _repo_with_claude_md(tmp_path)
    result = merge_claude_md(repo, BLOCK, runner=lambda *a, **k: _good_merge())

    assert result.applied
    assert result.backup and result.backup.read_text(encoding="utf-8") == ORIGINAL
    assert block_body((repo / "CLAUDE.md").read_text(encoding="utf-8")) == block_body(BLOCK)


def test_a_refused_merge_leaves_the_file_alone(tmp_path: Path):
    repo = _repo_with_claude_md(tmp_path)
    result = merge_claude_md(repo, BLOCK, runner=lambda *a, **k: "# Something else entirely\n")

    assert not result.applied
    assert (repo / "CLAUDE.md").read_text(encoding="utf-8") == ORIGINAL
    assert result.proposal_path and result.proposal_path.is_file()
    assert result.check and not result.check.ok


def test_a_second_merge_changes_nothing(tmp_path: Path):
    repo = _repo_with_claude_md(tmp_path, _good_merge())
    result = merge_claude_md(repo, BLOCK, runner=lambda *a, **k: pytest.fail("the model was called"))
    assert result.unchanged and not result.applied


def test_no_apply_writes_only_the_proposal(tmp_path: Path):
    repo = _repo_with_claude_md(tmp_path)
    result = merge_claude_md(repo, BLOCK, apply=False, runner=lambda *a, **k: _good_merge())

    assert not result.applied
    assert (repo / "CLAUDE.md").read_text(encoding="utf-8") == ORIGINAL
    assert result.proposal_path and result.proposal_path.is_file()


def test_a_runner_that_edits_the_file_itself_is_caught(tmp_path: Path):
    """What `claude -p` actually did: it wrote CLAUDE.md, outside verification and backup."""
    repo = _repo_with_claude_md(tmp_path)

    def agent_runner(brief, cwd, *a, **k):
        (repo / "CLAUDE.md").write_text("# rewritten by the agent\n", encoding="utf-8")
        return _good_merge()

    with pytest.raises(MergeError, match="changed while the merge was running"):
        merge_claude_md(repo, BLOCK, runner=agent_runner)


def test_merging_into_a_repo_with_no_claude_md_is_refused(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(MergeError, match="does not exist"):
        merge_claude_md(repo, BLOCK, runner=lambda *a, **k: _good_merge())


def test_the_brief_carries_the_block_and_the_original(tmp_path: Path):
    brief = merge_brief("acme", ORIGINAL, BLOCK)
    assert BRIEF_SENTINEL in brief
    assert BLOCK in brief
    assert "No unwrap outside tests." in brief


# --- install-time behaviour --------------------------------------------------------


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "docs").mkdir()
    return repo


def test_init_never_overwrites_an_existing_claude_md(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "CLAUDE.md").write_text(ORIGINAL, encoding="utf-8")

    result = init(repo, source_repo=REPO_ROOT, run_tests=False)

    assert (repo / "CLAUDE.md").read_text(encoding="utf-8") == ORIGINAL
    assert "CLAUDE.md" not in result.installed
    assert result.claude_md_pending and result.claude_md_pending.is_file()


def test_force_does_not_overwrite_an_existing_claude_md_either(tmp_path: Path):
    """`--force` means "overwrite the scaffold", never "replace the project's own document"."""
    repo = _git_repo(tmp_path)
    (repo / "CLAUDE.md").write_text(ORIGINAL, encoding="utf-8")
    init(repo, source_repo=REPO_ROOT, run_tests=False)

    init(repo, source_repo=REPO_ROOT, run_tests=False, force=True)

    assert (repo / "CLAUDE.md").read_text(encoding="utf-8") == ORIGINAL


def test_init_writes_the_template_when_there_is_no_claude_md(tmp_path: Path):
    repo = _git_repo(tmp_path)
    result = init(repo, source_repo=REPO_ROOT, run_tests=False)

    text = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "CLAUDE.md" in result.installed
    assert block_body(text) is not None, "the template must carry the markers a later merge needs"


def test_a_claude_md_already_carrying_the_block_needs_no_merge(tmp_path: Path):
    repo = _git_repo(tmp_path)
    block = render_claude_md_block(repo, REPO_ROOT)
    (repo / "CLAUDE.md").write_text(replace_block(ORIGINAL, block), encoding="utf-8")

    result = init(repo, source_repo=REPO_ROOT, run_tests=False)

    assert result.claude_md_current
    assert result.claude_md_pending is None


def test_the_rendered_block_matches_the_gates_installed(tmp_path: Path):
    repo = _git_repo(tmp_path)
    block = render_claude_md_block(repo, REPO_ROOT)
    for gate in ("precommit_review_guard.py", "precommit_doc_consistency_guard.py"):
        assert gate in block
