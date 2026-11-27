"""The scaffold a portable wheel has to carry, and the copy that puts it there.

``init`` reads two trees that live outside ``tool/``: ``starter/`` (the vendored artifact,
gates included) and ``global/`` (the machine tier). A wheel built without them installs, runs,
and then fails with "scaffold not found" on another machine.

Imported by ``setup.py`` at build time and by ``tests/test_wheel_portability.py``. It
imports nothing but the standard library, so the tests need neither setuptools nor a build.
"""

from __future__ import annotations

import shutil
from pathlib import Path

# tool/bundle_artifact.py -> repository root.
REPO_ROOT = Path(__file__).resolve().parent.parent

# (source relative to the repository root, destination relative to kerbtap/_artifact/).
BUNDLED_TREES = (
    ("starter", "starter"),
    ("global", "global"),
    ("research", "research"),
    (".claude-plugin", ".claude-plugin"),  # the marketplace; it points at ./global and ./research
)

_IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc")


class BundleError(Exception):
    """The checkout cannot produce a portable wheel."""


def bundle_artifact(build_lib: Path | str, repo_root: Path | str = REPO_ROOT) -> Path:
    """Copy the scaffold into ``<build_lib>/kerbtap/_artifact/``; return that directory.

    The copy lands in the build directory, never in the source tree, so building leaves a
    checkout unchanged and an editable install keeps resolving the checkout itself.
    """
    artifact = Path(build_lib) / "kerbtap" / "_artifact"
    if artifact.exists():
        shutil.rmtree(artifact)
    for src_rel, dest_rel in BUNDLED_TREES:
        src = Path(repo_root) / src_rel
        if not src.is_dir():
            raise BundleError(
                f"cannot build a portable wheel: {src} is missing. "
                "Build from a Governor checkout, not from an sdist."
            )
        shutil.copytree(src, artifact / dest_rel, ignore=_IGNORED)
    return artifact
