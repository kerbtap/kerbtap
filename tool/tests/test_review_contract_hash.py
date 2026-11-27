"""The review artifact records the review-contract hash and the pack flags a stale one."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from kerbtap.assemble import build_bundle
from kerbtap.build import render_pack
from kerbtap.collect import github as github_mod
from kerbtap.review import record as record_mod
from kerbtap.review.record import record
from tests.repo_factory import git, init_repo, install_review_gate
from tests.test_review_subject import _commit_hand_written_artifact, _record_and_commit

ZEROS = "sha256:" + "0" * 64


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(github_mod, "gh_available", lambda: False)
    r = init_repo(tmp_path / "r")
    install_review_gate(r)
    return r


@pytest.fixture
def contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "review-contract.md"
    path.write_text("contract v1\n", encoding="utf-8")
    monkeypatch.setattr(record_mod, "contract_path", lambda: path)
    return path


def _git_blob(text: str) -> str:
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 - git object id


def _change(repo: Path, sha: str):
    return next(c for c in build_bundle(repo).changes if c.change_id == sha[:12])


def test_record_stores_contract_hash(repo: Path, tmp_path: Path, contract: Path) -> None:
    (repo / "docs/plan").mkdir(parents=True)
    (repo / "docs/plan/a.md").write_text("a\n", encoding="utf-8")
    git(repo, "add", "docs/plan/a.md")
    verdict = tmp_path / "verdict.json"
    verdict.write_text(
        json.dumps({"verdict": "PASS", "reviewer": "r", "subject": ["docs/plan/a.md"], "review_contract": ZEROS})
    )
    artifact, _ = record(verdict, "PASS", "r", repo)
    stored = json.loads(artifact.read_text(encoding="utf-8"))
    assert stored["review_contract"] == "sha256:" + hashlib.sha256(contract.read_bytes()).hexdigest()


def test_pack_flags_old_contract_hash(repo: Path, tmp_path: Path, contract: Path) -> None:
    sha = _record_and_commit(repo, tmp_path, ["docs/plan/a.md"], {"docs/plan/a.md": "a\n"})
    old = "sha256:" + hashlib.sha256(contract.read_bytes()).hexdigest()
    assert _change(repo, sha).review.contract_state == "current"

    contract.write_text("contract v2\n", encoding="utf-8")
    change = _change(repo, sha)
    assert change.review.attested
    assert (change.review.contract, change.review.contract_state) == (old, "differs")

    rendered = render_pack(build_bundle(repo))
    entry = next(c for c in json.loads(rendered["manifest.json"])["changes"] if c["change_id"] == sha[:12])
    assert entry["review"]["contract"] == old
    assert entry["review"]["contract_state"] == "differs"
    assert "1 of them were recorded under a different review contract" in rendered["report.md"]


def test_pack_ignores_malformed_recorded_hash(repo: Path, contract: Path) -> None:
    sha = _commit_hand_written_artifact(
        repo,
        {
            "verdict": "PASS",
            "reviewer": "architect",
            "subject": ["docs/plan/a.md"],
            "subject_binding": [{"path": "docs/plan/a.md", "blob": _git_blob("a\n")}],
            "review_contract": "owner@example.com /etc/passwd",
        },
        {"docs/plan/a.md": "a\n"},
    )
    change = _change(repo, sha)
    assert (change.review.contract, change.review.contract_state) == ("", "not-recorded")
    assert "owner@example.com" not in render_pack(build_bundle(repo))["manifest.json"]


def test_same_size_same_mtime_edit_is_flagged(repo: Path, tmp_path: Path, contract: Path) -> None:
    sha = _record_and_commit(repo, tmp_path, ["docs/plan/a.md"], {"docs/plan/a.md": "a\n"})
    before = contract.stat()
    contract.write_text("contract v2\n", encoding="utf-8")  # same size as "contract v1\n"
    os.utime(contract, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert contract.stat().st_size == before.st_size
    assert _change(repo, sha).review.contract_state == "differs"


def test_pack_reports_unreadable_current_contract(repo: Path, tmp_path: Path, contract: Path) -> None:
    sha = _record_and_commit(repo, tmp_path, ["docs/plan/a.md"], {"docs/plan/a.md": "a\n"})
    contract.unlink()
    change = _change(repo, sha)
    assert change.review.attested
    assert change.review.contract_state == "current contract unreadable"
    rendered = render_pack(build_bundle(repo))
    assert "could not be compared with the current review contract" in rendered["report.md"]


def test_pack_reports_contract_not_recorded(repo: Path, contract: Path) -> None:
    sha = _commit_hand_written_artifact(
        repo,
        {
            "verdict": "PASS",
            "reviewer": "architect",
            "subject": ["docs/plan/a.md"],
            "subject_binding": [{"path": "docs/plan/a.md", "blob": _git_blob("a\n")}],
        },
        {"docs/plan/a.md": "a\n"},
    )
    change = _change(repo, sha)
    assert change.review.attested
    assert change.review.contract_state == "not-recorded"

    rendered = render_pack(build_bundle(repo))
    entry = next(c for c in json.loads(rendered["manifest.json"])["changes"] if c["change_id"] == sha[:12])
    assert entry["review"]["contract_state"] == "not-recorded"
    assert "contract not recorded" in rendered["report.md"]
