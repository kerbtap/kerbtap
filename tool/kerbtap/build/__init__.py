"""Render and write the six-component Evidence Pack.

Every component is run through :func:`kerbtap.honesty.assert_clean` before it is written,
so a barred integrity term leaking into any output fails the build loudly.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from kerbtap.build import (
    control_map,
    executive_summary,
    exception_log,
    manifest,
    remediation,
    report,
)
from kerbtap.honesty import assert_clean
from kerbtap.model import EvidenceBundle

logger = logging.getLogger(__name__)

# (filename, renderer) for the six components.
_COMPONENTS = (
    ("report.md", report.render),
    ("manifest.json", manifest.render),
    ("control-map.md", control_map.render),
    ("exception-log.md", exception_log.render),
    ("remediation-plan.md", remediation.render),
    ("executive-summary.md", executive_summary.render),
)


def render_pack(bundle: EvidenceBundle) -> dict[str, str]:
    """Render all six components to strings, asserting honesty on each."""
    rendered: dict[str, str] = {}
    for filename, renderer in _COMPONENTS:
        text = renderer(bundle)
        assert_clean(text, context=filename)
        rendered[filename] = text
    return rendered


class PackWriteError(RuntimeError):
    """The output location is unsafe to write (symlink or non-regular file).

    The default output directory lives inside the assessed repository, which is
    untrusted input: a pre-planted symlink at ``evidence-pack/manifest.json``
    would otherwise redirect the write onto an arbitrary file.
    """


def _write_pack_file(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` without ever following a symlink."""
    if path.is_symlink():
        raise PackWriteError(f"refusing to write through symlink: {path}")
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    # O_EXCL + O_NOFOLLOW: create fresh, and fail rather than traverse anything
    # racing to replace the path between the unlink and the open.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def generate_pack(bundle: EvidenceBundle, out_dir: Path) -> list[Path]:
    """Render and write the pack to ``out_dir``; return the written paths."""
    rendered = render_pack(bundle)
    if out_dir.is_symlink():
        raise PackWriteError(f"refusing to write into symlinked output dir: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, text in rendered.items():
        path = out_dir / filename
        _write_pack_file(path, text)
        written.append(path)
        logger.info("Wrote %s (%d bytes)", path, len(text))
    return written
