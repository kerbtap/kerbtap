"""``kerbtap review package`` — pin the change a reviewer is handed.

A review scoped to "the deliverable" reads whole files and the documents around them; the
cost grows with the repository, not with the change (dogfood O27). A package is the change
itself, pinned: the exact ``git diff`` a reviewer starts from, and a manifest naming what it
is a diff of.

Two modes:

* ``staged`` — the index against ``HEAD``: what the next commit would land. The manifest
  binds ``HEAD`` and a hash of the whole index (``git ls-files --stage``), so a package built
  before a later ``git add`` no longer matches.
* ``committed`` — ``merge-base(base, HEAD)..HEAD``: a branch already committed.

Files land in the gitignored ``<state folder>/review-packages/`` as
``<base7>-<head7>[-staged-<index7>].diff`` and ``.json``. Git's diff output is pinned with
explicit options and configuration so a user's diff settings cannot change the bytes.
Nothing here judges the change; it only fixes what was handed over.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from kerbtap.review.acceptance import load as load_acceptances
from kerbtap.review.acceptance import split_for_paths, utc_today
from kerbtap.review.record import ReviewError
from kerbtap.state import state_rel

logger = logging.getLogger(__name__)

SCHEMA = "kerbtap.review-package/1"
STAGED = "staged"
COMMITTED = "committed"

# Every git setting that changes `git diff` bytes, pinned.
_DIFF_CONFIG = (
    "-c", "color.diff=never",
    "-c", "core.quotePath=true",
    "-c", "diff.noprefix=false",
    "-c", "diff.mnemonicPrefix=false",
    "-c", "diff.relative=false",
    "-c", "diff.suppressBlankEmpty=false",
    "-c", "diff.context=3",
    "-c", "diff.interHunkContext=0",
    "-c", "diff.algorithm=myers",
    "-c", "diff.indentHeuristic=true",
)
# `-O/dev/null` overrides any diff.orderFile with an empty order: git's own path order.
DIFF_OPTIONS = (
    "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary", "--full-index", "-U3",
    "-O/dev/null",
)

_HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ReviewPackage:
    diff_path: Path
    manifest_path: Path
    manifest: dict


def _git(repo: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, check=True
        ).stdout
    except FileNotFoundError as exc:
        raise ReviewError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        command = next((a for i, a in enumerate(args) if a != "-c" and (i == 0 or args[i - 1] != "-c")), "")
        raise ReviewError(f"git {command} failed: {exc.stderr.decode('utf-8', 'replace').strip()}") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def head_oid(repo: Path) -> str:
    """``HEAD``'s commit id; refuses an unborn repository."""
    try:
        return _git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").decode().strip()
    except ReviewError as exc:
        raise ReviewError("HEAD has no commit yet; there is nothing to diff against") from exc


def index_hash(repo: Path) -> str:
    """A content hash of the whole index: every entry's mode, blob, stage and path."""
    return _sha256(_git(repo, "ls-files", "--stage", "-z"))


def _diff_args(mode: str, base: str, head: str) -> list[str]:
    if mode == STAGED:
        return ["--cached", head]
    return [base, head]


def diff_bytes(repo: Path, mode: str, base: str, head: str) -> bytes:
    """The pinned ``git diff`` for a package — the same bytes the package stores."""
    return _git(repo, *_DIFF_CONFIG, "diff", *DIFF_OPTIONS, *_diff_args(mode, base, head))


def _changed_paths(repo: Path, mode: str, base: str, head: str) -> list[str]:
    out = _git(
        repo, *_DIFF_CONFIG, "diff", "--no-ext-diff", "--no-renames", "--name-only", "-z",
        *_diff_args(mode, base, head),
    )
    return [p for p in out.decode("utf-8", "surrogateescape").split("\x00") if p]


def _hunks(diff: bytes, paths: list[str]) -> dict[str, list[list[int]]]:
    """Hunk ranges per path. Git emits one ``diff --git`` section per path, in the order
    ``--name-only`` lists them; a count mismatch means the pairing cannot be trusted."""
    sections = [s for s in re.split(rb"^diff --git ", diff, flags=re.MULTILINE) if s]
    if len(sections) != len(paths):
        raise ReviewError(f"diff has {len(sections)} file section(s) for {len(paths)} changed path(s)")
    ranges: dict[str, list[list[int]]] = {}
    for path, section in zip(paths, sections):
        ranges[path] = [
            [int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1)]
            for m in _HUNK_RE.finditer(section)
        ]
    return ranges


def _output_dir(repo: Path, out_dir: Path | None) -> Path:
    """The package directory, refusing a symlinked component.

    Inside the repository every component below the root is checked; outside it, the
    directory itself (system paths above it are the caller's business).
    """
    root = repo.resolve()
    target = (out_dir if out_dir is not None else root / state_rel(root, "review-packages")).absolute()
    if root in target.parents:
        current = target
        while current != root:
            if current.is_symlink():
                raise ReviewError(f"refusing a symlinked package directory component: {current}")
            current = current.parent
    elif target.is_symlink():
        raise ReviewError(f"refusing a symlinked package directory: {target}")
    return target


def _write_once(path: Path, data: bytes, what: str) -> None:
    if path.is_symlink():
        raise ReviewError(f"refusing to write a package through a symlink: {path}")
    if path.exists():
        if path.read_bytes() != data:
            raise ReviewError(f"a different {what} already exists at {path}; refusing to overwrite it")
        return
    path.write_bytes(data)


def build_package(
    repo: Path, *, base: str = "main", staged: bool = False, out_dir: Path | None = None, today: date | None = None
) -> ReviewPackage:
    """Write (or confirm) the package for the current change and return it."""
    repo = Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip())
    directory = _output_dir(repo, out_dir)
    head = head_oid(repo)
    if staged:
        mode, base_oid, idx = STAGED, head, index_hash(repo)
    else:
        try:
            _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
        except ReviewError as exc:
            raise ReviewError(f"base {base!r} does not name a commit") from exc
        mode, idx = COMMITTED, None
        base_oid = _git(repo, "merge-base", base, head).decode().strip()

    diff = diff_bytes(repo, mode, base_oid, head)
    paths = _changed_paths(repo, mode, base_oid, head)
    if not paths:
        raise ReviewError("the change is empty; there is nothing to review")
    live, dropped = split_for_paths(load_acceptances(repo), paths, today or utc_today())
    stem = f"{base_oid[:7]}-{head[:7]}" + (f"-staged-{idx[:7]}" if idx else "")
    if live or dropped:
        block = json.dumps({"acceptances": live, "acceptances_dropped": dropped}, sort_keys=True)
        stem += f"-acc-{_sha256(block.encode('utf-8'))[:7]}"
    manifest: dict = {
        "schema": SCHEMA,
        "mode": mode,
        "base": base_oid,
        "head": head,
        "diff_file": f"{stem}.diff",
        "diff_sha256": _sha256(diff),
        "diff_options": [*DIFF_OPTIONS],
        "paths": paths,
        "hunks": _hunks(diff, paths),
    }
    if live or dropped:
        manifest["acceptances"] = live
        manifest["acceptances_dropped"] = dropped
    if idx:
        manifest["index_hash"] = idx

    directory.mkdir(parents=True, exist_ok=True)
    diff_path = directory / f"{stem}.diff"
    manifest_path = directory / f"{stem}.json"
    _write_once(diff_path, diff, "package diff")
    _write_once(
        manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"),
        "package manifest",
    )
    logger.info(
        "Review package %s: %d path(s), %d byte diff, %d acceptance(s), %d dropped",
        stem, len(paths), len(diff), len(live), dropped,
    )
    return ReviewPackage(diff_path, manifest_path, manifest)


def load_package(manifest_path: Path) -> ReviewPackage:
    """Read a package and refuse it unless its diff still matches its manifest."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewError(f"package manifest unreadable: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ReviewError("not a review-package manifest")
    diff_path = manifest_path.parent / str(manifest.get("diff_file", ""))
    try:
        diff = diff_path.read_bytes()
    except OSError as exc:
        raise ReviewError(f"package diff unreadable: {exc}") from exc
    if _sha256(diff) != manifest.get("diff_sha256"):
        raise ReviewError("the package diff does not match its manifest")
    return ReviewPackage(diff_path, manifest_path, manifest)


def check_verdict_package(repo: Path, package: object) -> None:
    """Refuse a verdict whose ``package`` does not describe the change being recorded.

    The verdict copies ``base``, ``head`` (and, for a staged package, ``index_hash``) from
    the manifest it reviewed. Recording happens before the commit that carries the
    trailer, so ``head`` must still be ``HEAD`` and a staged package's index must still be
    the index — otherwise the review is of something else.
    """
    if not isinstance(package, dict):
        raise ReviewError("the verdict's `package` must be an object")
    base, head, idx = package.get("base"), package.get("head"), package.get("index_hash")
    if not (isinstance(base, str) and _OID_RE.match(base) and isinstance(head, str) and _OID_RE.match(head)):
        raise ReviewError("the verdict's `package` needs full `base` and `head` commit ids")
    if idx is not None and not (isinstance(idx, str) and _HEX64_RE.match(idx)):
        raise ReviewError("the verdict's `package.index_hash` must be 64 hex characters")
    if head != head_oid(repo):
        raise ReviewError("the reviewed package's head is not HEAD; the change moved since the review")
    if idx is not None and idx != index_hash(repo):
        raise ReviewError("the index changed since the reviewed package was built; review the new change")
