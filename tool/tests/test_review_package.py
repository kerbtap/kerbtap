"""``kerbtap review package``: the pinned change a reviewer is handed (dogfood O27)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kerbtap.review.package import (
    DIFF_OPTIONS,
    SCHEMA,
    build_package,
    diff_bytes,
    index_hash,
    load_package,
)
from kerbtap.review.record import ReviewError, record
from kerbtap.state import state_rel
from tests.conftest import require_source
from tests.repo_factory import commit_files, git, init_repo

REPO = Path(__file__).resolve().parents[2]
TOOL_ROOT = Path(__file__).resolve().parents[1]

_LONG = "".join(f"line {i}\n" for i in range(40))


def _git_bytes(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = init_repo(tmp_path / "r")
    commit_files(r, "base", {"docs/a.md": _LONG, "gone.md": "old\n", "bin.dat": "x"})
    (r / "bin.dat").write_bytes(b"\x00\x01binary\x02")
    git(r, "add", "bin.dat")
    git(r, "commit", "-q", "-m", "binary")
    return r


def _edit_multi_hunk(repo: Path) -> None:
    lines = _LONG.splitlines(keepends=True)
    lines[2] = "changed early\n"
    lines[35] = "changed late\n"
    (repo / "docs/a.md").write_text("".join(lines))


class TestStaged:
    def test_reproduces_git_and_binds_head_and_index(self, repo: Path) -> None:
        _edit_multi_hunk(repo)
        (repo / "new.md").write_text("new\n")
        (repo / "bin.dat").write_bytes(b"\x00\x09changed")
        git(repo, "rm", "-q", "gone.md")
        git(repo, "add", "-A")
        package = build_package(repo, staged=True)
        m = package.manifest
        head = git(repo, "rev-parse", "HEAD").stdout.strip()
        expected = _git_bytes(repo, "diff", *DIFF_OPTIONS, "--cached", head)
        assert package.diff_path.read_bytes() == expected
        assert (m["schema"], m["mode"], m["base"], m["head"]) == (SCHEMA, "staged", head, head)
        assert m["index_hash"] == hashlib.sha256(_git_bytes(repo, "ls-files", "--stage", "-z")).hexdigest()
        assert m["paths"] == ["bin.dat", "docs/a.md", "gone.md", "new.md"]
        assert m["hunks"]["docs/a.md"] == [[1, 6, 1, 6], [33, 7, 33, 7]]
        assert m["hunks"]["new.md"] == [[0, 0, 1, 1]]
        assert m["hunks"]["gone.md"] == [[1, 1, 0, 0]]
        assert m["hunks"]["bin.dat"] == []  # binary: no text hunks
        assert m["diff_sha256"] == hashlib.sha256(expected).hexdigest()
        assert package.diff_path.parent == repo.resolve() / state_rel(repo, "review-packages")
        assert package.diff_path.name == f"{head[:7]}-{head[:7]}-staged-{m['index_hash'][:7]}.diff"

    def test_user_diff_settings_do_not_change_the_bytes(self, repo: Path) -> None:
        _edit_multi_hunk(repo)
        git(repo, "add", "-A")
        plain = build_package(repo, staged=True).diff_path.read_bytes()
        git(repo, "config", "diff.noprefix", "true")
        git(repo, "config", "diff.context", "10")
        git(repo, "config", "diff.algorithm", "histogram")
        assert diff_bytes(repo, "staged", "", git(repo, "rev-parse", "HEAD").stdout.strip()) == plain

    def test_rebuild_is_idempotent_and_a_new_stage_is_a_new_package(self, repo: Path) -> None:
        _edit_multi_hunk(repo)
        git(repo, "add", "-A")
        first = build_package(repo, staged=True)
        again = build_package(repo, staged=True)
        assert (first.diff_path, first.manifest) == (again.diff_path, again.manifest)
        (repo / "more.md").write_text("more\n")
        git(repo, "add", "more.md")
        later = build_package(repo, staged=True)
        assert later.diff_path != first.diff_path
        assert later.manifest["index_hash"] != first.manifest["index_hash"]


class TestCommitted:
    def test_reproduces_merge_base_to_head(self, repo: Path) -> None:
        git(repo, "checkout", "-q", "-b", "feat")
        _edit_multi_hunk(repo)
        git(repo, "commit", "-q", "-am", "edit")
        commit_files(repo, "add", {"docs/b.md": "b\n"})
        package = build_package(repo, base="main")
        base = git(repo, "merge-base", "main", "HEAD").stdout.strip()
        head = git(repo, "rev-parse", "HEAD").stdout.strip()
        assert package.diff_path.read_bytes() == _git_bytes(repo, "diff", *DIFF_OPTIONS, base, head)
        assert (package.manifest["mode"], package.manifest["base"], package.manifest["head"]) == ("committed", base, head)
        assert "index_hash" not in package.manifest
        assert package.manifest["paths"] == ["docs/a.md", "docs/b.md"]


class TestRefusals:
    def test_invalid_base_ref(self, repo: Path) -> None:
        with pytest.raises(ReviewError, match="does not name a commit"):
            build_package(repo, base="no-such-branch")

    def test_unborn_head(self, tmp_path: Path) -> None:
        r = init_repo(tmp_path / "unborn")
        (r / "a.md").write_text("a\n")
        git(r, "add", "a.md")
        with pytest.raises(ReviewError, match="no commit yet"):
            build_package(r, staged=True)

    def test_empty_change(self, repo: Path) -> None:
        with pytest.raises(ReviewError, match="empty"):
            build_package(repo, staged=True)

    def test_existing_diff_with_other_bytes_is_not_overwritten(self, repo: Path) -> None:
        _edit_multi_hunk(repo)
        git(repo, "add", "-A")
        package = build_package(repo, staged=True)
        package.diff_path.write_bytes(b"tampered\n")
        with pytest.raises(ReviewError, match="different package diff"):
            build_package(repo, staged=True)

    def test_existing_manifest_with_other_bytes_is_not_overwritten(self, repo: Path) -> None:
        _edit_multi_hunk(repo)
        git(repo, "add", "-A")
        package = build_package(repo, staged=True)
        package.manifest_path.write_text("{}\n")
        with pytest.raises(ReviewError, match="different package manifest"):
            build_package(repo, staged=True)

    def test_symlinked_package_directory(self, repo: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / ".governor").mkdir()
        os.symlink(outside, repo / ".governor" / "review-packages")
        _edit_multi_hunk(repo)
        git(repo, "add", "-A")
        with pytest.raises(ReviewError, match="symlinked"):
            build_package(repo, staged=True)
        assert not any(outside.iterdir())

    def test_load_refuses_a_diff_that_no_longer_matches(self, repo: Path) -> None:
        _edit_multi_hunk(repo)
        git(repo, "add", "-A")
        package = build_package(repo, staged=True)
        assert load_package(package.manifest_path).manifest == package.manifest
        package.diff_path.write_bytes(package.diff_path.read_bytes() + b"+smuggled\n")
        with pytest.raises(ReviewError, match="does not match its manifest"):
            load_package(package.manifest_path)


class TestVerdictBinding:
    def _verdict(self, tmp_path: Path, package: dict) -> Path:
        path = tmp_path / "v.json"
        path.write_text(json.dumps({"verdict": "PASS", "subject": ["docs/a.md"], "package": package}))
        return path

    def _pkg(self, repo: Path) -> dict:
        m = build_package(repo, staged=True).manifest
        return {"base": m["base"], "head": m["head"], "index_hash": m["index_hash"]}

    def test_verdict_for_the_current_package_records(self, repo: Path, tmp_path: Path) -> None:
        _edit_multi_hunk(repo)
        git(repo, "add", "-A")
        pkg = self._pkg(repo)
        artifact, _ = record(self._verdict(tmp_path, pkg), "PASS", "reviewer", repo)
        assert json.loads(artifact.read_text())["package"] == pkg

    def test_index_changed_after_the_package_is_refused(self, repo: Path, tmp_path: Path) -> None:
        _edit_multi_hunk(repo)
        git(repo, "add", "-A")
        pkg = self._pkg(repo)
        (repo / "late.md").write_text("late\n")
        git(repo, "add", "late.md")
        with pytest.raises(ReviewError, match="index changed"):
            record(self._verdict(tmp_path, pkg), "PASS", "reviewer", repo)

    def test_head_moved_after_the_package_is_refused(self, repo: Path, tmp_path: Path) -> None:
        _edit_multi_hunk(repo)
        git(repo, "add", "-A")
        pkg = self._pkg(repo)
        git(repo, "commit", "-q", "-m", "moved")
        with pytest.raises(ReviewError, match="not HEAD"):
            record(self._verdict(tmp_path, pkg), "PASS", "reviewer", repo)

    @pytest.mark.parametrize(
        "package",
        [
            pytest.param("abc", id="not-object"),
            pytest.param({"base": "abc1234", "head": "abc1234"}, id="short-oids"),
            pytest.param({"base": "a" * 40, "head": "b" * 40, "index_hash": "short"}, id="bad-index-hash"),
        ],
    )
    def test_malformed_package_field_is_refused(self, repo: Path, tmp_path: Path, package: object) -> None:
        with pytest.raises(ReviewError):
            record(self._verdict(tmp_path, package), "PASS", "reviewer", repo)


def test_cli_prints_the_two_paths(repo: Path) -> None:
    _edit_multi_hunk(repo)
    git(repo, "add", "-A")
    proc = subprocess.run(
        [sys.executable, "-m", "kerbtap", "review", "package", "--staged", "--repo", str(repo)],
        cwd=TOOL_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    diff_path, manifest_path = proc.stdout.split()
    assert diff_path.endswith(".diff") and manifest_path.endswith(".json")
    assert Path(diff_path).is_file() and Path(manifest_path).is_file()


def test_cli_refuses_with_exit_2(repo: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "kerbtap", "review", "package", "--base", "nope", "--repo", str(repo)],
        cwd=TOOL_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "does not name a commit" in proc.stderr


def test_packages_are_ignored_and_records_are_not() -> None:
    require_source(".gitignore")
    ignored = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "-q", ".governor/review-packages/x.diff"]
    )
    tracked = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "-q", ".governor/reviews/x.json"]
    )
    assert ignored.returncode == 0
    assert tracked.returncode == 1


@pytest.mark.parametrize(
    "contract",
    [REPO / ".claude/agents/architect/SKILL.md", REPO / "starter/.claude/agents/reviewer/SKILL.md"],
    ids=["architect", "reviewer"],
)
def test_contracts_take_the_package_first(contract: Path) -> None:
    if contract == REPO / ".claude/agents/architect/SKILL.md":
        require_source(".claude/agents/architect/SKILL.md")
    text = contract.read_text(encoding="utf-8")
    assert "kerbtap review package --staged" in text
    assert "The review package — your primary input." in text
    assert "named invariant" in text
    assert '"package":' in text
