"""The wheel carries its own scaffold, and finds it with no checkout anywhere above it.

The failure this file exists to catch, silent on the machine that built the wheel: a wheel
with no ``_artifact/`` — ``governor init`` dies with "scaffold not found".

The copy that ``setup.py`` runs is ``bundle_artifact.bundle_artifact``, imported here and
run into a temporary directory, so these tests need no network, no ``pip``, no setuptools
and no build isolation.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kerbtap.init import artifact_root, default_source_repo, init

TOOL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TOOL_DIR.parent


def _load_bundler():
    """``tool/bundle_artifact.py`` — outside the ``governor`` package, so load it by path."""
    spec = importlib.util.spec_from_file_location("governor_bundler", TOOL_DIR / "bundle_artifact.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bundled(tmp_path_factory) -> Path:
    """``kerbtap/_artifact/`` as the build hook writes it into a wheel."""
    build_lib = tmp_path_factory.mktemp("build_lib")
    return _load_bundler().bundle_artifact(build_lib)


def test_the_bundle_carries_every_tree_the_installer_reads(bundled: Path):
    assert (bundled / "starter" / ".claude" / "hooks").is_dir()
    assert (bundled / "global" / "hooks").is_dir()
    assert (bundled / "global" / "settings.hooks.json").is_file()


def test_bundle_has_no_dogfood_hooks(bundled: Path, tmp_path: Path):
    """The wheel carries no ``.claude/hooks/``, and an ``init`` from it installs the six commit-gate files (GS-003)."""
    assert not (bundled / ".claude" / "hooks").exists()
    repo = tmp_path / "target"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "docs").mkdir()
    init(repo, source_repo=bundled, run_tests=False)
    hooks = repo / ".claude" / "hooks"
    for name in (
        "commit_scope.py",
        "precommit_review_guard.py",
        "precommit_doc_consistency_guard.py",
        "test_commit_scope.py",
        "test_precommit_review_guard.py",
        "test_precommit_doc_consistency_guard.py",
    ):
        assert (hooks / name).is_file(), f"not installed from the bundle: {name}"


def test_no_pycache_rides_along(bundled: Path):
    assert not list(bundled.rglob("__pycache__"))


def test_artifact_root_is_none_without_a_bundle(tmp_path: Path):
    (tmp_path / "_artifact").mkdir()
    assert artifact_root(tmp_path) is None


def test_artifact_root_finds_the_bundle(bundled: Path):
    assert artifact_root(bundled.parent) == bundled


def test_a_source_checkout_still_resolves_to_the_checkout():
    """An editable install has no bundle and must keep finding starter/ two levels up."""
    assert default_source_repo() == REPO_ROOT
    assert (default_source_repo() / "starter" / ".claude").is_dir()


def test_init_works_from_a_bundle_with_no_checkout_above_it(bundled: Path, tmp_path: Path):
    """The portability claim itself: install from the bundle alone, into a repo elsewhere.

    ``bundled`` lives under pytest's temporary directory, so nothing above it is a Governor
    checkout — the state of the work laptop the wheel is carried to.
    """
    repo = tmp_path / "target"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "docs").mkdir()

    result = init(repo, source_repo=bundled, run_tests=False)

    assert result.installed, "init installed nothing"
    guard = (repo / ".claude" / "hooks" / "precommit_review_guard.py").read_text(encoding="utf-8")
    assert '"deny"' in guard, "installed the starter's ask-mode guard, which does not gate"
    assert (repo / ".claude" / "settings.json").is_file()


def test_install_machine_reads_no_bundle_and_ships_its_legacy_list():
    """The cutover installs a plugin; it needs no copy of ``global/``. Its legacy list is package data."""
    from kerbtap.machine import LEGACY_FILES_PATH, legacy_files

    assert LEGACY_FILES_PATH.parent.name == "data" and LEGACY_FILES_PATH.is_file()
    assert legacy_files(), "the legacy file list is empty"
    pyproject = (TOOL_DIR / "pyproject.toml").read_text(encoding="utf-8")
    assert '"kerbtap.data" = ["*.json"]' in pyproject, "package data does not cover the legacy list"


def test_a_bundle_missing_a_tree_refuses_to_build(tmp_path: Path):
    """The build hook fails loudly rather than producing a wheel that cannot scaffold."""
    bundler = _load_bundler()
    incomplete = tmp_path / "repo_root"
    (incomplete / "starter" / ".claude").mkdir(parents=True)
    with pytest.raises(bundler.BundleError):
        bundler.bundle_artifact(tmp_path / "build", repo_root=incomplete)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required to scaffold a repo")
def test_the_bundle_is_what_the_checkout_ships(bundled: Path):
    """Byte-for-byte, so a stale bundle cannot ship yesterday's gates."""
    for rel in ("starter/.claude/hooks", "global/hooks"):
        for src in sorted((REPO_ROOT / rel).glob("*.py")):
            copy = bundled / rel / src.name
            assert copy.is_file(), f"{rel}/{src.name} is not in the wheel"
            assert copy.read_bytes() == src.read_bytes(), f"{rel}/{src.name} differs from the checkout"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
