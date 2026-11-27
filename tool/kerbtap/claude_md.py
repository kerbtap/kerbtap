"""Merge kerbtap's gate documentation into a repository's own ``CLAUDE.md``.

``CLAUDE.md`` states what a repository is. Overwriting it to document five hooks destroys
the file's reason to exist, so ``init`` never writes over one. What it writes instead is a
*brief*: the kerbtap-owned block, plus the rules a merge has to respect.

The merge itself is done by the model, because the repository kerbtap installs into is by
definition running Claude Code, and placing a gate procedure where a document's own logic
says it belongs is exactly the judgement a parser does not have. What a model must not be
trusted with is the silent loss of someone's prose, so the proposal is gated before it
lands:

* every heading of the original still exists;
* every paragraph of the original still exists, whitespace-normalized;
* the kerbtap block appears once, and its body is byte-equal to what ``init`` renders;
* no barred honesty term is introduced that the original did not already carry.

A proposal failing any of those is refused and kept under ``<state folder>/pending/`` for a
human to read. The original is never touched except through a backup.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from kerbtap.honesty import BARRED_TERMS
from kerbtap.state import state_rel

BEGIN = "<!-- GOVERNOR:BEGIN -->"
END = "<!-- GOVERNOR:END -->"

# The pending brief carries this sentinel so a model that echoes its instructions back
# instead of merging is caught rather than committed.
BRIEF_SENTINEL = "GOVERNOR-MERGE-BRIEF"

def pending_rel(repo: Path) -> str:
    return state_rel(repo, "pending", "claude-md.md").as_posix()


def proposal_rel(repo: Path) -> str:
    return state_rel(repo, "pending", "claude-md.proposal.md").as_posix()

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_TIMEOUT = 300

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S.*$", re.MULTILINE)
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.DOTALL)


class MergeError(Exception):
    """The merge could not be produced or could not be trusted."""


def governor_block(prefix: str, hook_table: str) -> str:
    """The kerbtap-owned section, markers included.

    Everything outside the markers belongs to the repository. Everything inside is
    regenerated from the gates actually installed, so a hand-edit here is lost on the next
    merge — which is the point: a hand-written gate table drifts from the wiring.
    """
    body = "\n".join(
        (
            "## Governance gates",
            "",
            "Installed by `kerbtap init` from the gates actually wired. Regenerated on install;",
            "edit outside the markers, never inside them.",
            "",
            hook_table,
            "",
            f"Each gate is cleared by its own env-assignment prefix — `{prefix}_REVIEWED=1`,",
            f"`{prefix}_DOC_REVIEWED=1`, `{prefix}_CLAIM_VERIFIED=1` —",
            "placed before the command, never quoted inside an argument.",
            "",
            "Each gate ships a `test_*.py` beside it that drives the real hook and asserts it",
            "fires on a violation. Run them after changing a gate's constants; a gate that can no",
            "longer fire is the failure worth catching.",
        )
    )
    return f"{BEGIN}\n{body}\n{END}"


def block_body(text: str) -> str | None:
    """The content between the markers, or ``None`` when the block is absent or malformed."""
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        return None
    start = text.index(BEGIN)
    end = text.index(END)
    if end < start:
        return None
    return text[start + len(BEGIN) : end].strip("\n")


def replace_block(text: str, block: str) -> str:
    """``text`` with its kerbtap block replaced by ``block``; appended when absent."""
    if block_body(text) is None:
        separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return f"{text}{separator}\n{block}\n"
    start = text.index(BEGIN)
    end = text.index(END) + len(END)
    return text[:start] + block + text[end:]


def headings(text: str) -> list[str]:
    """Heading lines, whitespace-normalized, outside the kerbtap block."""
    return [" ".join(line.split()) for line in _HEADING_RE.findall(_without_block(text))]


def paragraphs(text: str) -> list[str]:
    """Whitespace-normalized non-empty blocks outside the kerbtap block."""
    return [
        " ".join(chunk.split())
        for chunk in re.split(r"\n\s*\n", _without_block(text))
        if chunk.strip()
    ]


def _without_block(text: str) -> str:
    if block_body(text) is None:
        return text
    start = text.index(BEGIN)
    end = text.index(END) + len(END)
    return text[:start] + text[end:]


def _normalized(text: str) -> str:
    return " ".join(_without_block(text).split())


@dataclass
class MergeCheck:
    """Why a proposed merge may or may not replace the original."""

    ok: bool
    failures: list[str] = field(default_factory=list)


def verify_merge(original: str, proposal: str, block: str) -> MergeCheck:
    """Gate a proposed ``CLAUDE.md`` against the original and the kerbtap block."""
    failures: list[str] = []

    if not proposal.strip():
        return MergeCheck(False, ["the proposal is empty"])

    if BRIEF_SENTINEL in proposal:
        failures.append("the proposal contains the merge brief itself, not a merged document")

    body = block_body(proposal)
    expected = block_body(block)
    if body is None:
        failures.append(f"the kerbtap block is missing or malformed ({BEGIN} / {END} once each, in order)")
    elif body != expected:
        failures.append("the kerbtap block was edited; its body must be byte-equal to what init renders")

    normalized_proposal = _normalized(proposal)

    missing_headings = [h for h in headings(original) if " ".join(h.split()) not in _normalized(proposal)]
    if missing_headings:
        failures.append(f"headings dropped from the original: {missing_headings[:5]}")

    missing_paragraphs = [p for p in paragraphs(original) if p not in normalized_proposal]
    if missing_paragraphs:
        shown = [p[:70] + ("…" if len(p) > 70 else "") for p in missing_paragraphs[:3]]
        failures.append(
            f"{len(missing_paragraphs)} paragraph(s) of the original are not in the proposal: {shown}"
        )

    lowered_original = original.lower()
    lowered_proposal = proposal.lower()
    introduced = [t for t in BARRED_TERMS if t in lowered_proposal and t not in lowered_original]
    if introduced:
        failures.append(f"barred terms introduced: {introduced}")

    return MergeCheck(not failures, failures)


def merge_brief(repo_name: str, original: str, block: str) -> str:
    """What the model is asked to do. Written to ``<state folder>/pending/`` verbatim."""
    return "\n".join(
        (
            f"# {BRIEF_SENTINEL} — {repo_name}",
            "",
            "Merge the block below into this repository's CLAUDE.md.",
            "",
            "Rules, each one checked mechanically before anything is written:",
            "",
            "1. Keep every heading and every paragraph of the existing file. Do not reword, reorder",
            "   or summarise them. Content may move; it may not disappear or change.",
            f"2. Include the block between {BEGIN} and {END} exactly as given, byte for byte,",
            "   markers included.",
            "3. Place the block where this document's own structure says it belongs — beside the",
            "   sections on tooling, workflow or conventions if it has them, rather than at whichever",
            "   end is convenient. Add no commentary of your own.",
            "4. Introduce no claim about tamper-proofing, immutability or certification.",
            "",
            "Return the complete merged CLAUDE.md and nothing else: no explanation, no diff.",
            "",
            "---",
            "",
            "## The block to merge in",
            "",
            block,
            "",
            "---",
            "",
            "## The existing CLAUDE.md",
            "",
            original,
        )
    )


def extract_document(output: str) -> str:
    """The merged document from a model's reply.

    A fenced block is taken when the reply is wrapped in one — the common shape — and the
    largest such block wins, since a merged ``CLAUDE.md`` dwarfs any example inside it.
    """
    fences = _FENCE_RE.findall(output)
    if fences:
        return max(fences, key=len).strip("\n")
    return output.strip("\n")


def claude_available() -> bool:
    return shutil.which("claude") is not None


# `claude -p` is an agent, not a text transform: given a merge brief in a repository it
# will happily edit CLAUDE.md itself, outside this module's verification and backup. It did
# exactly that the first time this ran. The merge therefore runs with an empty tool
# allowlist, with anything that would prompt denied outright, and from an empty scratch
# directory rather than the repository.
NO_TOOLS_ARGS = ("--allowed-tools", "", "--permission-prompts", "none")


def run_claude(brief: str, cwd: Path, model: str = DEFAULT_MODEL, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Ask the local Claude Code CLI for the merged document, with no tools of any kind.

    ``cwd`` is ignored deliberately: the model is run in a scratch directory so that a
    proposal is the only thing it can produce.
    """
    if not claude_available():
        raise MergeError("the `claude` CLI is not on PATH — nothing was written")
    with tempfile.TemporaryDirectory(prefix="governor-merge-") as scratch:
        try:
            completed = subprocess.run(
                ["claude", "-p", "--model", model, *NO_TOOLS_ARGS],
                input=brief,
                cwd=scratch,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - timing dependent
            raise MergeError(f"the merge timed out after {timeout}s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise MergeError(f"`claude -p` exited {completed.returncode}: {detail[-1] if detail else 'no output'}")
    return extract_document(completed.stdout)


@dataclass
class MergeResult:
    """What ``merge_claude_md`` did, for the caller to report."""

    applied: bool = False
    backup: Path | None = None
    proposal_path: Path | None = None
    pending_path: Path | None = None
    check: MergeCheck | None = None
    unchanged: bool = False


def write_pending(repo: Path, brief: str) -> Path:
    """Park the brief for a later merge; returns its path."""
    pending = repo / pending_rel(repo)
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(brief, encoding="utf-8")
    return pending


def _backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.governor-backup-{stamp}")
    shutil.copy2(path, backup)
    return backup


def merge_claude_md(
    repo: Path,
    block: str,
    *,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    apply: bool = True,
    runner=run_claude,
) -> MergeResult:
    """Merge ``block`` into ``repo/CLAUDE.md`` through the model, gated by ``verify_merge``.

    ``runner`` is injected so the gate can be tested without a model call.
    """
    target = repo / "CLAUDE.md"
    if not target.is_file():
        raise MergeError(f"{target} does not exist — `kerbtap init` writes one when a repo has none")

    original = target.read_text(encoding="utf-8")
    result = MergeResult()

    if block_body(original) == block_body(block):
        result.unchanged = True
        return result

    brief = merge_brief(repo.name, original, block)
    result.pending_path = write_pending(repo, brief)

    proposal = runner(brief, repo, model, timeout)

    # Defence in depth against the runner being an agent rather than a transform: if the
    # file moved under us, this process is no longer the only writer and its backup would
    # preserve the wrong bytes.
    if target.read_text(encoding="utf-8") != original:
        raise MergeError(
            f"{target} changed while the merge was running — the merge tool wrote to it directly. "
            "Nothing further was written; inspect the file and its git history."
        )

    check = verify_merge(original, proposal, block)
    result.check = check

    if not check.ok:
        proposal_path = repo / proposal_rel(repo)
        proposal_path.parent.mkdir(parents=True, exist_ok=True)
        proposal_path.write_text(proposal, encoding="utf-8")
        result.proposal_path = proposal_path
        return result

    if not apply:
        proposal_path = repo / proposal_rel(repo)
        proposal_path.parent.mkdir(parents=True, exist_ok=True)
        proposal_path.write_text(proposal, encoding="utf-8")
        result.proposal_path = proposal_path
        return result

    result.backup = _backup(target)
    target.write_text(proposal if proposal.endswith("\n") else proposal + "\n", encoding="utf-8")
    result.applied = True
    result.pending_path.unlink(missing_ok=True)
    return result


def diff(before: str, after: str, label: str = "CLAUDE.md") -> str:
    """A unified diff of the merge, for the install report."""
    import difflib

    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{label} (before)",
            tofile=f"{label} (merged)",
        )
    )
