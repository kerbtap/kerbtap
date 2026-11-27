"""``kerbtap review record`` / ``kerbtap review verify`` — the repo-aware half.

``record`` ingests the reviewer's structured verdict file, stores it as a
content-addressed artifact under ``<state folder>/reviews/<sha256>.json``, and prints the git
trailer block that binds the reviewed commit to it. ``verify`` re-derives the signal from
a commit's body and confirms the referenced artifact exists and hashes to the ref.

A verdict names its ``subject`` — the repo-relative paths it reviewed — and ``record``
binds each to the content staged at record time (its index blob id, or ``null`` for a
deletion). Verification then requires the subject to cover the commit *at the committed
content*: every path the commit's review gate guarded when it touched any, otherwise every
path the commit changed (review records under ``<state folder>/reviews/`` excepted). A merge or
an empty commit has no changed paths of its own, so a record there never counts. A verdict
therefore cannot be replayed onto a different change or a later revision of the same
files. An artifact that names no files (``record`` no longer writes one; older ones exist)
still resolves on a commit with changes of its own and is reported as "scope not
recorded" — the pack cannot tell when it was written.

The resolved state is called *integrity-checked*, never *verified*: the trailer and its
artifact agree and name the committed bytes. It does not prove *who* produced the
artifact — that needs identity/signing, which is out of scope by design (see
:mod:`kerbtap.review`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from kerbtap.collect import gate_surface
from kerbtap.collect.git import commit_changed_paths
from kerbtap.init import default_source_repo
from kerbtap.state import NEW_NAME, OLD_NAME, state_rel
from kerbtap.review.trailer import (
    CLAIMED,
    CONTRACT_CURRENT,
    CONTRACT_DIFFERS,
    CONTRACT_NOT_RECORDED,
    CONTRACT_UNREADABLE,
    INTEGRITY_CHECKED,
    REASON_ARTIFACT_UNVERIFIED,
    REASON_SUBJECT_MISMATCH,
    SCOPE_CONFIRMED,
    SCOPE_NOT_RECORDED,
    VALID_VERDICTS,
    ReviewSignal,
    classify_review,
    format_trailers,
)

logger = logging.getLogger(__name__)

BINDING_KEY = "subject_binding"
CONTRACT_KEY = "review_contract"


class ReviewError(RuntimeError):
    """Raised when a review artifact cannot be recorded or checked."""


@dataclass(frozen=True, slots=True)
class ArtifactCheck:
    """The outcome of checking one integrity-checked-shaped trailer against its artifact."""

    ok: bool
    message: str
    reason: str  # REASON_ARTIFACT_UNVERIFIED or REASON_SUBJECT_MISMATCH when not ok
    scope: str | None = None  # SCOPE_CONFIRMED | SCOPE_NOT_RECORDED when ok
    contract: str = ""  # the review-contract hash the artifact recorded, "" when none
    contract_state: str = ""  # CONTRACT_* when ok


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def contract_path() -> Path:
    """The review contract of the installed kerbtap (not of the target repository)."""
    return default_source_repo() / "global/review/review-contract.md"


def _contract_hash() -> str:
    """``sha256:<hex>`` of the review contract; raise ``ReviewError`` naming the path if unreadable."""
    path = contract_path()
    try:
        return f"sha256:{_sha256_hex(path.read_bytes())}"
    except OSError as exc:
        logger.error("Cannot read the review contract %s: %s", path, exc)
        raise ReviewError(f"cannot read the review contract at {path}: {exc}") from exc


def current_contract_hash() -> str | None:
    """The current contract's hash, or ``None`` when it cannot be read (already logged)."""
    try:
        return _contract_hash()
    except ReviewError:
        return None


class _ReadContract:
    """Sentinel: the caller did not supply the current contract hash; read it now."""


READ_CONTRACT = _ReadContract()
CONTRACT_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _artifact_path(repo: Path, digest: str) -> Path:
    """Resolve ``<repo>/<state folder>/reviews/<digest>.json``, refusing symlink traversal.

    Every component under the repo (the state folder, ``reviews``, and the artifact file)
    must be a real path — a symlinked directory could redirect storage or verification
    outside the repo (audit M5). The resolved path is also required to stay inside the
    repo. Missing components are fine (nothing to follow yet).
    """
    return real_path_under(repo, (*state_rel(repo, "reviews").parts, f"{digest}.json"))


def real_path_under(repo: Path, parts: tuple[str, ...]) -> Path:
    """``repo/<parts>``, refusing a symlink in any component below the repo or an escape from it."""
    repo_root = repo.resolve()
    current = repo_root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ReviewError(f"refusing to traverse a symlink under the repository: {current}")
    resolved_parent = current.parent.resolve()
    if repo_root != resolved_parent and repo_root not in resolved_parent.parents:
        raise ReviewError(f"path escapes the repository: {current}")
    return current


def _canonical_bytes(verdict_output: object) -> bytes:
    """Serialize a parsed verdict to stable, canonical JSON bytes.

    Canonicalizing (sorted keys, fixed separators, trailing newline) means the same
    logical verdict always hashes identically regardless of the input file's incidental
    whitespace or key order — the ref is a fact about content, not formatting.
    """
    text = json.dumps(verdict_output, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def _git(repo: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(repo), *args],
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise ReviewError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", "replace").strip()
        raise ReviewError(f"git {args[0]} failed: {stderr}") from exc
    return result.stdout


def normalize_subject(value: object) -> list[str]:
    """Validate a verdict's ``subject``: a non-empty list of repo-relative POSIX paths.

    Refuses absolute paths, ``..`` or ``.`` components, empty or duplicate entries, and
    any spelling that is not already normal — a path is compared byte for byte later.
    """
    if not isinstance(value, list) or not value:
        raise ReviewError(
            "the verdict must name its subject: a non-empty `subject` list of the repo-relative "
            "paths the review covered"
        )
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item:
            raise ReviewError(f"subject entries must be non-empty strings, got {item!r}")
        parts = item.split("/")
        if (
            item.startswith("/")
            or "\\" in item
            or "\x00" in item
            or any(part in ("", ".", "..") for part in parts)
            or PurePosixPath(item).as_posix() != item
        ):
            raise ReviewError(f"subject path must be repo-relative and normalized, got {item!r}")
        if item in seen:
            raise ReviewError(f"subject lists {item!r} twice")
        seen.add(item)
    return list(value)


def _index_blobs(repo: Path, paths: list[str]) -> dict[str, str | None]:
    """The staged blob id of each path, or ``None`` when the path is not in the index."""
    out = _git(repo, "ls-files", "--stage", "-z", "--", *paths)
    blobs: dict[str, str | None] = {p: None for p in paths}
    for entry in out.decode("utf-8", "surrogateescape").split("\x00"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if path in blobs and len(fields) == 3 and fields[2] == "0":
            blobs[path] = fields[1]
    return blobs


def _tree_blobs(repo: Path, commit: str, paths: tuple[str, ...]) -> dict[str, str | None]:
    """The blob id of each path in ``commit``'s tree, or ``None`` when absent there."""
    if not paths:
        return {}
    out = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit, "--", *paths)
    blobs: dict[str, str | None] = {p: None for p in paths}
    for entry in out.decode("utf-8", "surrogateescape").split("\x00"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if path in blobs and len(fields) == 3 and fields[1] == "blob":
            blobs[path] = fields[2]
    return blobs


_JSON_FENCE_RE = re.compile(r"^```json[ \t]*\n(.*?)\n```[ \t]*$", re.DOTALL | re.MULTILINE)


def extract_verdict(message: str) -> dict:
    """The verdict object from a reviewer's final message.

    Reviewers do not write files: they end their message with the verdict as a fenced
    ``json`` block. The LAST such block is the verdict — an earlier block is an example or a
    quotation, and a malformed last block is refused rather than skipped. A message that is
    nothing but a JSON object (the starter reviewer's contract) is accepted as-is.
    """
    blocks = _JSON_FENCE_RE.findall(message)
    candidate = blocks[-1] if blocks else message.strip()
    if not blocks and not candidate.startswith("{"):
        raise ReviewError("no verdict found: the message has no fenced json block and is not a JSON object")
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ReviewError(f"the verdict block is not readable JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ReviewError("the verdict block must be a JSON object")
    return parsed


def record(
    review_output: Path, verdict: str, reviewer: str, repo: Path, *, from_message: bool = False
) -> tuple[Path, str]:
    """Store the reviewer's verdict as a content-addressed artifact; return (path, trailers).

    The artifact's own ``verdict`` field must equal ``verdict`` — the trailer must never
    claim a PASS while the review it references records something weaker. This binds the
    trailer verdict to the artifact content, not just to its bytes.

    The verdict must name its ``subject``. Each subject path is bound to the blob staged in
    the index right now (``null`` for a path not in the index — a staged deletion), so
    record after staging exactly what was reviewed. Any ``subject_binding`` already in the
    file is replaced, never trusted. A verdict that names the review ``package`` it was
    given is refused unless that package still describes ``HEAD`` (and, when staged, the
    current index).

    Args:
        review_output: Path to the reviewer's structured verdict JSON — or, with
            ``from_message``, to the reviewer's saved final message (see
            :func:`extract_verdict`). The verdict must contain a ``verdict`` field matching
            the ``verdict`` argument and a ``subject`` list.
        verdict: ``PASS`` or ``NEEDS-CHANGES``.
        reviewer: Reviewer label recorded in the trailer (e.g. ``architect``).
        repo: Repository root under which ``<state folder>/reviews/`` lives.
        from_message: Read ``review_output`` as a reviewer message, not a JSON file.

    Returns:
        ``(artifact_path, trailer_block)`` — append ``trailer_block`` to the commit that
        carries the reviewed change.
    """
    if verdict not in VALID_VERDICTS:
        raise ReviewError(f"verdict must be one of {VALID_VERDICTS}, got {verdict!r}")
    if not review_output.is_file():
        raise ReviewError(f"review-output file not found: {review_output}")
    try:
        text = review_output.read_text(encoding="utf-8")
        parsed = extract_verdict(text) if from_message else json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewError(f"review-output must be readable JSON: {exc}") from exc
    if not isinstance(parsed, dict) or parsed.get("verdict") != verdict:
        raise ReviewError(
            f"review-output verdict {parsed.get('verdict') if isinstance(parsed, dict) else None!r} "
            f"does not match --verdict {verdict!r}; the trailer must reference an artifact that "
            f"records the same verdict."
        )
    subject = normalize_subject(parsed.get("subject"))
    if "package" in parsed:
        # Imported here: the package module raises this module's ReviewError.
        from kerbtap.review.package import check_verdict_package

        check_verdict_package(repo, parsed["package"])
    staged = _index_blobs(repo, subject)
    parsed = {**parsed, BINDING_KEY: [{"path": p, "blob": staged[p]} for p in subject]}
    parsed[CONTRACT_KEY] = _contract_hash()

    canonical = _canonical_bytes(parsed)
    digest = _sha256_hex(canonical)
    ref = f"sha256:{digest}"

    artifact_path = _artifact_path(repo, digest)
    # Content-addressed: if it already exists with matching bytes, recording is idempotent.
    if artifact_path.exists():
        if artifact_path.read_bytes() != canonical:
            raise ReviewError(
                f"hash collision or corrupt artifact at {artifact_path}: "
                "content does not match its own reference"
            )
    else:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(canonical)

    trailers = format_trailers(verdict, reviewer, ref)
    logger.info(
        "Recorded review artifact %s (verdict=%s, %d subject path(s), contract=%s)",
        artifact_path,
        verdict,
        len(subject),
        parsed[CONTRACT_KEY],
    )
    return artifact_path, trailers


def _commit_body(repo: Path, commit: str) -> str:
    """Return the full message (subject + body) of ``commit`` via git."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%B", commit],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except FileNotFoundError as exc:
        raise ReviewError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise ReviewError(f"cannot read commit {commit}: {exc.stderr.strip()}") from exc
    return result.stdout


def _artifact_bytes_from_commit(repo: Path, commit: str, digest: str) -> bytes | None:
    """Return the review-artifact blob as it exists in ``commit``'s *tree*, or ``None``.

    Reads via ``git show <commit>:<path>`` — the durable evidence is what git records at
    the attested commit, NOT whatever happens to sit in the working-tree checkout. An
    untracked or later-added artifact therefore cannot make an old trailer verify, and the
    same commit verifies identically in any clone. A symlink tree entry yields its target
    string as content, whose hash will not match the ref — so it fails closed.
    """
    # The commit's own tree decides, never the checkout: try the old folder, then the new.
    for name in (OLD_NAME, NEW_NAME):
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), "show", f"{commit}:{name}/reviews/{digest}.json"],
                capture_output=True,  # bytes (no text decode) — the hash is over raw bytes
                check=True,
            )
        except FileNotFoundError as exc:
            raise ReviewError("git executable not found on PATH") from exc
        except subprocess.CalledProcessError:
            continue  # path absent from that commit's tree
        return result.stdout
    return None


def coverage_from(paths: tuple[str, ...], guarded: tuple[str, ...] | None) -> tuple[str, ...]:
    """The paths a review record must name, given a commit's own changed paths.

    ``paths`` are the commit's changed paths (``()`` for a merge or an empty commit);
    ``guarded`` the subset its review gate guarded (``None`` when that cannot be read).
    Review records are never part of what was reviewed. The guarded paths when there are
    any; otherwise every changed path — a superset, so an unreadable gate configuration
    asks for more, never less. ``()`` means the record can never count.
    """
    own = tuple(p for p in paths if not p.startswith((f"{OLD_NAME}/reviews/", f"{NEW_NAME}/reviews/")))
    if not own:
        return ()
    return guarded if guarded else own


def coverage_paths(repo: Path, commit: str) -> tuple[str, ...]:
    """:func:`coverage_from` for ``commit``, read from the repository."""
    sha = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    paths = commit_changed_paths(repo, sha)
    surface = gate_surface.surfaces_by_commit(repo, [sha])[sha]
    if surface.state == gate_surface.ACTIVE:
        guarded: tuple[str, ...] | None = tuple(p for p in paths if surface.guards(p))
    elif surface.state in (gate_surface.ABSENT, gate_surface.UNWIRED):
        guarded = ()
    else:
        guarded = None
    return coverage_from(paths, guarded)


def _check_subject(repo: Path, parsed: dict, commit: str, coverage: tuple[str, ...]) -> ArtifactCheck | None:
    """``None`` when the subject covers ``coverage`` at the committed content; else the failing check."""
    binding = parsed.get(BINDING_KEY)
    entries: dict[str, str | None] = {}
    valid = isinstance(binding, list)
    for entry in binding if valid else ():
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not (entry.get("blob") is None or isinstance(entry.get("blob"), str))
        ):
            valid = False
            break
        entries[entry["path"]] = entry["blob"]
    if not valid:
        return ArtifactCheck(False, "review artifact subject binding is malformed", REASON_SUBJECT_MISMATCH)
    missing = [p for p in coverage if p not in entries]
    committed = _tree_blobs(repo, commit, tuple(p for p in coverage if p in entries))
    drifted = [p for p, blob in committed.items() if entries[p] != blob]
    if missing or drifted:
        return ArtifactCheck(
            False,
            f"review artifact subject does not cover this commit: {len(missing)} required path(s) "
            f"not named, {len(drifted)} named at different content than committed",
            REASON_SUBJECT_MISMATCH,
        )
    return None


class _ReadFromCommit:
    """Sentinel: the caller did not supply the coverage paths; read them from the commit."""


READ_FROM_COMMIT = _ReadFromCommit()
Coverage = tuple[str, ...] | _ReadFromCommit


def verify_artifact(
    repo: Path,
    signal: ReviewSignal,
    commit: str,
    coverage: Coverage = READ_FROM_COMMIT,
    current_contract: str | None | _ReadContract = READ_CONTRACT,
) -> ArtifactCheck:
    """Check ``signal``'s artifact *as recorded in ``commit``'s tree*.

    It must be present there, hash to the ref, record the same verdict, and — the commit
    having changed paths of its own — cover them as :func:`coverage_from` defines, at the
    committed content, when it names a subject. ``coverage`` may be passed by a caller that
    already computed it; by default it is read from the commit.

    Reading from the commit tree (not the working checkout) is what makes the record
    durable and re-checkable. Messages reference the artifact by content hash /
    repo-relative path only (never an absolute path) since this string can flow into the
    external-safe pack evidence on the CLI path.
    """
    if signal.verification != INTEGRITY_CHECKED or not signal.ref:
        return ArtifactCheck(
            False, f"not an integrity-checked trailer ({signal.verification})", REASON_ARTIFACT_UNVERIFIED
        )
    digest = signal.ref.split(":", 1)[1]
    rel = f"{OLD_NAME}/reviews/{digest}.json or {NEW_NAME}/reviews/{digest}.json"

    raw =_artifact_bytes_from_commit(repo, commit, digest)
    if raw is None:
        return ArtifactCheck(
            False, f"review artifact not present in the commit's tree: {rel}", REASON_ARTIFACT_UNVERIFIED
        )
    actual = _sha256_hex(raw)
    if actual != digest:
        return ArtifactCheck(
            False,
            f"review artifact hash mismatch for {signal.ref}: content hashes to sha256:{actual}",
            REASON_ARTIFACT_UNVERIFIED,
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ArtifactCheck(False, f"review artifact {rel} is not readable JSON: {exc}", REASON_ARTIFACT_UNVERIFIED)
    if not isinstance(parsed, dict) or parsed.get("verdict") != signal.verdict:
        got = parsed.get("verdict") if isinstance(parsed, dict) else None
        return ArtifactCheck(
            False,
            f"review artifact verdict {got!r} does not match trailer verdict {signal.verdict!r}",
            REASON_ARTIFACT_UNVERIFIED,
        )

    if isinstance(coverage, _ReadFromCommit):
        coverage = coverage_paths(repo, commit)
    if not coverage:
        return ArtifactCheck(
            False,
            "the commit has no changed paths of its own (a merge or an empty commit), so no review record counts",
            REASON_SUBJECT_MISMATCH,
        )
    recorded = parsed.get(CONTRACT_KEY)
    if not isinstance(recorded, str) or not CONTRACT_HASH_RE.fullmatch(recorded):
        # Absent or not a well-formed hash: copy nothing from the artifact into the pack.
        contract, state = "", CONTRACT_NOT_RECORDED
    else:
        contract = recorded
        if isinstance(current_contract, _ReadContract):
            current_contract = current_contract_hash()
        if current_contract is None:
            state = CONTRACT_UNREADABLE  # nobody compared the hashes
        else:
            state = CONTRACT_CURRENT if contract == current_contract else CONTRACT_DIFFERS
    checked = f"integrity-checked: {signal.verdict} by {signal.reviewer}, artifact {signal.ref}"
    if "subject" not in parsed and BINDING_KEY not in parsed:
        return ArtifactCheck(
            True, f"{checked} (the record names no reviewed files; scope not recorded)", "", SCOPE_NOT_RECORDED, contract, state
        )
    failure = _check_subject(repo, parsed, commit, coverage)
    if failure is not None:
        return failure
    return ArtifactCheck(True, f"{checked}, subject covers the commit at the committed content", "", SCOPE_CONFIRMED, contract, state)


def verify(repo: Path, commit: str) -> tuple[bool, str, ReviewSignal]:
    """Check a commit's review trailer against the artifact recorded in its tree.

    Returns ``(ok, message, signal)``. ``ok`` is true only when the commit carries a PASS
    trailer with a well-formed ref AND the referenced artifact is present in that commit's
    tree, hashes to the ref, records the same verdict, and covers the commit when it names
    a subject.
    """
    signal = classify_review(_commit_body(repo, commit))
    if signal.verification != INTEGRITY_CHECKED:
        return False, f"not an integrity-checked review record ({signal.verification}): {signal.evidence}", signal
    check = verify_artifact(repo, signal, commit)
    return check.ok, check.message, signal


_DOWNGRADE_EVIDENCE = {
    REASON_ARTIFACT_UNVERIFIED: (
        "A structured review trailer is present but its review artifact did not "
        "validate (missing, altered, or a different verdict); recorded as an "
        "unchecked claim."
    ),
    REASON_SUBJECT_MISMATCH: (
        "A structured review trailer is present but its review artifact does not cover "
        "this change at the committed content (or the change has no changed paths of its "
        "own); recorded as an unchecked claim."
    ),
}


def resolve_pack_signal(
    repo: Path,
    signal: ReviewSignal,
    commit: str,
    coverage: Coverage = READ_FROM_COMMIT,
    current_contract: str | None | _ReadContract = READ_CONTRACT,
) -> ReviewSignal:
    """Downgrade an integrity-checked-shaped signal to ``claimed`` unless its artifact
    resolves *in ``commit``'s tree* and covers the commit; record the scope when it does.

    The Evidence Pack must not count a review record from trailer syntax alone, nor from an
    artifact that is merely sitting in the working-tree checkout, nor from an artifact that
    reviewed different files or different content — each would overclaim. Signals that are
    not structured PASS trailers (prose claims, overrides, none) pass through unchanged, so
    this stays a no-op on repos that never adopted the trailer.

    The downgrade ``evidence`` is a fixed, path-free string (the specific failure reason
    stays in the CLI ``verify`` output) so nothing repo-sourced enters the pack here.
    """
    if signal.verification != INTEGRITY_CHECKED:
        return signal
    check = verify_artifact(repo, signal, commit, coverage, current_contract)
    if check.ok:
        return replace(signal, scope=check.scope, contract=check.contract, contract_state=check.contract_state)
    return replace(
        signal,
        verification=CLAIMED,
        attested=False,
        reason=check.reason,
        evidence=_DOWNGRADE_EVIDENCE[check.reason],
    )
