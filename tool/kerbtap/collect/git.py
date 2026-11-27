"""Collect governance evidence from a repository's git history.

Everything here reads what ``git log`` already emitted — no evidence is invented. The
signal-parsing helpers (:func:`detect_review`, :func:`extract_jira_refs`,
:func:`classify_landing`, :func:`extract_pr_number`) are pure functions so they can be
unit-tested against real commit-body text.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import replace
from pathlib import Path

from kerbtap.honesty import redact_quoted
from kerbtap.model import Commit
from kerbtap.review.trailer import ReviewSignal, classify_review

logger = logging.getLogger(__name__)

# Separators built on NUL, which is the ONE byte a git commit message can never contain
# (\x1f/\x1e can legitimately appear in hostile message bytes and previously allowed a
# crafted message to shift fields or drop records). The record separator embeds a NUL on
# each side, so no message content can ever fabricate it.
_FIELD_SEP = "\x00"
_RECORD_SEP = "\x00\x1e\x00"
_GIT_FORMAT = "%x00".join(["%H", "%an", "%ae", "%aI", "%P", "%s", "%b"]) + "%x00%x1e%x00"

# A Jira-style key: 2 to 10 characters of project prefix, a hyphen, digits. The denylist holds
# the prefixes of tokens that have this shape and are not tickets (SHA-256, UTF-8, ...).
_JIRA_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,9}-\d+)\b")
_DENYLIST_FILE = Path(__file__).resolve().parent.parent / "data" / "ticket_key_denylist.json"
# Read once at import; a missing or bad file raises, so a broken install fails loudly.
_DENIED_PREFIXES = frozenset(json.loads(_DENYLIST_FILE.read_text(encoding="utf-8"))["prefixes"])
_MERGE_PR_RE = re.compile(r"Merge pull request #(\d+)")
_SQUASH_PR_RE = re.compile(r"\(#(\d+)\)\s*$")
_MERGE_BRANCH_RE = re.compile(r"Merge pull request #\d+ from [\w.-]+/(\S+)")


class GitError(RuntimeError):
    """Raised when the target path is not a usable git repository."""


def _run_git(repo_path: Path, *args: str) -> str:
    """Run a git command in ``repo_path`` and return its stdout."""
    try:
        # Explicit UTF-8 with replacement: commit messages are raw bytes and may hold
        # invalid UTF-8; collection must degrade to U+FFFD, never crash on decode.
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except FileNotFoundError as exc:  # git binary missing
        raise GitError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise GitError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    return result.stdout


def is_git_repo(repo_path: Path) -> bool:
    """Return whether ``repo_path`` is inside a git work tree."""
    try:
        out = _run_git(repo_path, "rev-parse", "--is-inside-work-tree")
    except GitError:
        return False
    return out.strip() == "true"


def has_commits(repo_path: Path) -> bool:
    """Return whether the repo has any commit (False on an unborn/just-initialized HEAD)."""
    try:
        _run_git(repo_path, "rev-parse", "--verify", "--quiet", "HEAD")
    except GitError:
        return False
    return True


def is_shallow(repo_path: Path) -> bool:
    """Return whether the repo is a shallow clone (history truncated at a depth boundary).

    A shallow clone must never be described as carrying the full history — the pack's
    source-system evidence line depends on this flag to stay honest.
    """
    try:
        out = _run_git(repo_path, "rev-parse", "--is-shallow-repository")
    except GitError:
        return False
    return out.strip() == "true"


def first_parent_shas(repo_path: Path) -> set[str]:
    """Return the set of commit SHAs on the default branch's first-parent history.

    A commit on this line is on the repository mainline. A non-merge commit here with no
    pull-request reference is a genuine direct-to-main commit. A commit *not* on this line
    entered the mainline only through a merge (a feature-branch commit of a PR).
    """
    if not has_commits(repo_path):
        return set()
    out = _run_git(repo_path, "log", "--first-parent", "--format=%H")
    return set(out.split())


def merge_branch_map(repo_path: Path, commits: list[Commit]) -> dict[str, tuple[int | None, str | None]]:
    """Map each feature-branch commit to the PR (number, branch) that merged it.

    For every merge commit ``M`` with parents ``[base, head]``, the commits reachable from
    ``head`` but not from ``base`` are the PR's feature-branch commits.
    """
    mapping: dict[str, tuple[int | None, str | None]] = {}
    for commit in commits:
        if not commit.is_merge or len(commit.parents) < 2:
            continue
        pr_number = extract_pr_number(commit)
        branch = extract_branch(commit)
        base, head = commit.parents[0], commit.parents[1]
        try:
            out = _run_git(repo_path, "rev-list", head, f"^{base}")
        except GitError as exc:
            logger.warning("rev-list for merge %s failed: %s", commit.short_sha, exc)
            continue
        for sha in out.split():
            mapping.setdefault(sha, (pr_number, branch))
    return mapping


def has_commit(repo_path: Path, sha: str) -> bool:
    """Whether the object store of ``repo_path`` holds the commit ``sha``, reachable from HEAD or not."""
    return subprocess.run(
        ["git", "-C", str(repo_path), "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True, check=False
    ).returncode == 0


def collect_commits(repo_path: Path, shas: list[str] | None = None) -> list[Commit]:
    """Read the reachable commit history of ``repo_path`` (newest first), or just the commits ``shas``."""
    if not is_git_repo(repo_path):
        raise GitError(f"{repo_path} is not a git repository")
    if not has_commits(repo_path):
        # A just-initialized repository (unborn HEAD) has zero commits — that is an
        # honest, assessable state, not an error.
        logger.info("Repository has no commits yet (unborn HEAD): %s", repo_path)
        return []

    if shas is not None:
        raw = _run_git(repo_path, "log", "--no-walk", f"--format={_GIT_FORMAT}", *shas) if shas else ""
    else:
        raw = _run_git(repo_path, "log", f"--format={_GIT_FORMAT}")
    commits: list[Commit] = []
    for record in raw.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split(_FIELD_SEP)
        if len(fields) < 7:
            logger.warning("Skipping malformed git record: %r", record[:80])
            continue
        sha, an, ae, adate, parents, subject, body = fields[:7]
        commits.append(
            Commit(
                sha=sha,
                author_name=an,
                author_email=ae,
                authored_date=adate,
                subject=subject,
                body=body,
                parents=parents.split() if parents.strip() else [],
            )
        )
    logger.info("Collected %d commits from %s", len(commits), repo_path)
    return commits


def changed_paths(repo_path: Path) -> dict[str, tuple[str, ...]]:
    """Map each non-merge commit to the repo-relative paths it changed, in one ``git log``.

    Renames are reported as a deletion plus an addition (``--no-renames``), so both the old
    and the new path count as changed. Merge commits are deliberately absent: the review
    gate evaluates ``git commit``, and a merge is assessed through the commits it brings in.
    A root commit lists every file it adds.

    ``-z`` keeps paths byte-exact (no C-style quoting). Each record opens with two NULs —
    an empty token can never be a path — followed by the SHA, then the NUL-separated paths.
    """
    if not has_commits(repo_path):
        return {}
    raw = _run_git(
        repo_path,
        "-c", "core.quotePath=false",
        "log", "--no-merges", "--no-renames", "--name-only", "-z", "--format=%x00%x00%H",
    )
    result: dict[str, tuple[str, ...]] = {}
    tokens = raw.split("\x00")
    i = 0
    current: str | None = None
    paths: list[str] = []
    while i < len(tokens):
        if tokens[i] == "" and i + 2 < len(tokens) and tokens[i + 1] == "" and _is_sha(tokens[i + 2]):
            if current is not None:
                result[current] = tuple(paths)
            current, paths = tokens[i + 2], []
            i += 3
            continue
        token = tokens[i]
        if not paths and token.startswith("\n"):
            token = token[1:]  # git separates the format line from the first path with one newline
        if token and current is not None:
            paths.append(token)
        i += 1
    if current is not None:
        result[current] = tuple(paths)
    return result


def commit_changed_paths(repo_path: Path, sha: str) -> tuple[str, ...]:
    """One commit's changed paths, with exactly :func:`changed_paths`' semantics.

    A merge commit returns ``()`` (outside the gate's population); a root commit lists
    what it adds; renames count as both paths.
    """
    parents = _run_git(repo_path, "rev-list", "--parents", "-n", "1", sha).split()
    if len(parents) > 2:
        return ()
    raw = _run_git(
        repo_path,
        "-c", "core.quotePath=false",
        "diff-tree", "--root", "--no-commit-id", "--no-renames", "-r", "--name-only", "-z", sha,
    )
    return tuple(p for p in raw.split("\x00") if p)


_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _is_sha(token: str) -> bool:
    return bool(_SHA_RE.match(token))


# --- pure signal-parsing helpers (unit-tested) --------------------------------------


def detect_review(commit: Commit, override_names: tuple[str, ...] | None = None) -> ReviewSignal:
    """Classify the review signal on a commit for the Evidence Pack — trailer-first.

    A change is ``attested`` only when it carries a structured ``Governor-Review`` PASS
    trailer bound to a review artifact by content hash (see
    :mod:`kerbtap.review.trailer`). A free-text "adversarial review … PASS" note is
    demoted to ``claimed`` and never attested. Pure function of the commit body, so it
    works unchanged against any repository, including those that never adopted the trailer.

    The classifier guarantees ``evidence`` is derived only from a reason code and a
    validated ref, so the pack's ONLY job here is to redact the single raw field —
    ``reviewer`` — through the same unified quoted-text redaction as every other
    repo-sourced field. No other value can carry an absolute path, email, or barred term.

    Trailer detection runs over the reconstructed full message (subject + body), matching
    what ``git log --format=%B`` — and therefore ``kerbtap review verify`` — sees. The
    collector stores subject (``%s``) and body (``%b``) separately, and ``%b`` drops the
    blank line after the subject; classifying ``%b`` alone would miss a footer on a commit
    whose body is only the trailer block (e.g. ``git commit -m subject -m "<trailers>"``),
    splitting the pack's verdict from ``verify``'s.

    ``override_names`` are the bypass-variable names that mark an explicit override at this
    commit — installs rename the gate's variable per repository, so the caller passes the
    name read from the commit's own hook.
    """
    text = f"{commit.subject}\n\n{commit.body}"
    signal = classify_review(text) if override_names is None else classify_review(text, override_names)
    if signal.reviewer is None:
        return signal
    return replace(signal, reviewer=redact_quoted(signal.reviewer)[0])


def ticket_keys(text: str) -> list[str]:
    """Unique Jira-style ticket keys in ``text``, in order, minus the denylisted prefixes."""
    seen: dict[str, None] = {}
    for match in _JIRA_RE.finditer(text):
        key = match.group(1)
        if key.rsplit("-", 1)[0] not in _DENIED_PREFIXES:
            seen.setdefault(key, None)
    return list(seen)


def extract_jira_refs(commit: Commit) -> list[str]:
    """Extract unique ticket keys (``PROJ-<n>``) from a commit's subject and body."""
    return ticket_keys(f"{commit.subject}\n{commit.body}")


def extract_pr_number(commit: Commit) -> int | None:
    """Return the PR number a commit landed through, or ``None`` if not detectable."""
    merge = _MERGE_PR_RE.search(commit.subject)
    if merge:
        return int(merge.group(1))
    squash = _SQUASH_PR_RE.search(commit.subject)
    if squash:
        return int(squash.group(1))
    return None


def extract_branch(commit: Commit) -> str | None:
    """Return the source branch of a merge commit, if present in its subject."""
    match = _MERGE_BRANCH_RE.search(commit.subject)
    return match.group(1) if match else None


def classify_landing(commit: Commit, first_parent: set[str] | None = None) -> str:
    """Classify how a commit entered the default branch.

    Args:
        commit: The commit to classify.
        first_parent: The default branch's first-parent SHA set (from
            :func:`first_parent_shas`). When provided, a non-merge/non-squash commit that
            is *not* on this line is a feature-branch commit of a pull request, not a
            direct commit. When ``None``, mainline membership cannot be determined and a
            bare non-merge/non-squash commit is conservatively labeled direct-to-main.

    Returns:
        One of ``"merge-pr"``, ``"squash-pr"``, ``"pr-branch-commit"``, or
        ``"direct-to-main"``.
    """
    if commit.is_merge:
        return "merge-pr"
    if _SQUASH_PR_RE.search(commit.subject):
        return "squash-pr"
    if first_parent is not None and commit.sha not in first_parent:
        return "pr-branch-commit"
    return "direct-to-main"
