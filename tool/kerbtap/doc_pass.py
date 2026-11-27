"""`kerbtap doc-pass record`: bind a finished `/verify-claims doc-pass` to the content passed.

Each record is one JSON line `{"ts", "path", "blob"}` in
`<main worktree>/<state folder>/doc-pass/records.jsonl`. `blob` is the `git hash-object` of the
working-tree file. The doc gate reads the file and allows a commit when every tracked doc
in it has a record of the blob that lands. The record is self-attested: it proves the
command ran on those bytes, not that the pass ran.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from kerbtap.review.record import ReviewError, real_path_under
from kerbtap.state import state_rel

logger = logging.getLogger(__name__)

_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class DocPassError(Exception):
    """A path cannot be recorded; nothing was written."""


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=repo, stderr=subprocess.PIPE, text=True, timeout=10
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise DocPassError(f"git {args[0]} failed: {(exc.stderr or '').strip()}") from exc
    except (subprocess.SubprocessError, OSError) as exc:
        raise DocPassError(f"git {args[0]} could not run: {exc}") from exc


def record(repo: Path, paths: list[Path]) -> list[dict[str, str]]:
    """Append one record per path, all in one write, and return them.

    A path is relative to ``repo``, or absolute under it. Every path must be a regular
    file under the repository, not a symlink. One refusal writes nothing.
    """
    if not paths:
        raise DocPassError("no path given")
    top = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    common = Path(_git(repo, "rev-parse", "--git-common-dir"))
    root = (common if common.is_absolute() else repo / common).resolve().parent
    ts = datetime.now(timezone.utc).strftime(_UTC_FORMAT)
    lines: list[dict[str, str]] = []
    for given in paths:
        absolute = Path(os.path.normpath(given if given.is_absolute() else repo.resolve() / given))
        try:
            relative = absolute.relative_to(top)
        except ValueError as exc:
            raise DocPassError(f"{given} is outside the repository") from exc
        try:
            real = real_path_under(top, relative.parts)
        except ReviewError as exc:
            raise DocPassError(str(exc)) from exc
        if not real.is_file():
            raise DocPassError(f"{given} is not a regular file")
        blob = _git(top, "hash-object", "--", relative.as_posix())
        lines.append({"ts": ts, "path": relative.as_posix(), "blob": blob})

    target = root / state_rel(root, "doc-pass", "records.jsonl")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        ignore = target.parent / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        with target.open("a", encoding="utf-8") as handle:
            handle.write("".join(json.dumps(line) + "\n" for line in lines))
    except OSError as exc:
        raise DocPassError(f"cannot write {target}: {exc}") from exc
    for line in lines:
        logger.info("Recorded %s at %s", line["path"], line["blob"])
    return lines
