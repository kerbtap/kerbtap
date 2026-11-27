"""Bundle the scaffold into the wheel.

Metadata lives in ``pyproject.toml``; this file exists only for the build step below.
What is copied, and why a wheel without it is worse than useless: ``bundle_artifact.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bundle_artifact import bundle_artifact  # noqa: E402  (needs the path above)


class build_py(_build_py):
    """``build_py`` plus the scaffold, copied into ``kerbtap/_artifact/``."""

    def run(self) -> None:
        super().run()
        bundle_artifact(Path(self.build_lib))


setup(cmdclass={"build_py": build_py})
