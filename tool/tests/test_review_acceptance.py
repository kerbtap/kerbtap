"""``governor review accept``: accepted residuals with an end date."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from kerbtap.cli import main
from kerbtap.review.acceptance import accept, load, write_entries
from kerbtap.state import state_rel
from kerbtap.review.package import build_package
from kerbtap.review.record import ReviewError
from tests.repo_factory import commit_files, git, init_repo

TODAY = date(2026, 11, 16)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = init_repo(tmp_path / "r")
    commit_files(r, "base", {"docs/a.md": "one\n"})
    (r / "docs/a.md").write_text("two\n")
    git(r, "add", "-A")
    return r


def _file(repo: Path) -> Path:
    return repo / state_rel(repo, "acceptances.json")


def _put(repo: Path, entries: list[dict]) -> None:
    _file(repo).parent.mkdir(parents=True, exist_ok=True)
    _file(repo).write_text(json.dumps(entries))


def _entry(**over: object) -> dict:
    base = {
        "id": "a1", "subject": "docs/a.md", "statement": "known", "accepted_at": "2026-11-01",
        "expired_at": "2026-12-01", "renewals": 2,
    }
    return {**base, **over}


def _ok(repo: Path, **over: object) -> dict:
    args = {"id": "a1", "subject": "docs/a.md", "statement": "known", "today": TODAY, **over}
    return accept(repo, **args)


def test_accept_defaults_expired_at_to_30_days(repo: Path) -> None:
    entry = _ok(repo)
    assert entry["expired_at"] == "2026-12-16"
    assert entry["renewals"] == 0
    assert entry["accepted_at"] == "2026-11-16"
    assert load(repo) == [entry]


@pytest.mark.parametrize("statement", [None, "", "  "])
def test_accept_refuses_missing_statement(repo: Path, statement: str | None) -> None:
    with pytest.raises(ReviewError):
        _ok(repo, statement=statement)
    assert not _file(repo).exists()


def test_load_refuses_missing_expired_at(repo: Path) -> None:
    entry = _entry()
    del entry["expired_at"]
    _put(repo, [entry])
    with pytest.raises(ReviewError):
        load(repo)


def test_accept_refuses_past_expired_at(repo: Path) -> None:
    with pytest.raises(ReviewError):
        _ok(repo, expired_at="2026-11-15")
    assert not _file(repo).exists()


def test_accept_refuses_expired_at_over_60_days(repo: Path) -> None:
    with pytest.raises(ReviewError):
        _ok(repo, expired_at="2027-01-16")
    assert not _file(repo).exists()
    assert _ok(repo, expired_at="2027-01-15")["expired_at"] == "2027-01-15"


def test_accept_refuses_duplicate_id(repo: Path) -> None:
    _ok(repo)
    before = _file(repo).read_bytes()
    with pytest.raises(ReviewError):
        _ok(repo)
    assert _file(repo).read_bytes() == before


def test_renew_bumps_renewals_and_keeps_accepted_at(repo: Path) -> None:
    _put(repo, [_entry()])
    entry = accept(repo, renew="a1", today=TODAY)
    assert entry["renewals"] == 3
    assert entry["accepted_at"] == "2026-11-01"
    assert entry["expired_at"] == "2026-12-16"
    assert load(repo) == [entry]


def test_renew_refuses_reset_accepted_at(repo: Path) -> None:
    _put(repo, [_entry()])
    before = _file(repo).read_bytes()
    with pytest.raises(ReviewError):
        write_entries(repo, [_entry(renewals=3, accepted_at="2026-11-16", expired_at="2026-12-16")], today=TODAY, renewed="a1")
    assert _file(repo).read_bytes() == before


def test_renew_refuses_reset_renewals(repo: Path) -> None:
    _put(repo, [_entry()])
    before = _file(repo).read_bytes()
    with pytest.raises(ReviewError):
        write_entries(repo, [_entry(renewals=0, expired_at="2026-12-16")], today=TODAY, renewed="a1")
    assert _file(repo).read_bytes() == before


def test_renew_refuses_unknown_id(repo: Path) -> None:
    with pytest.raises(ReviewError):
        accept(repo, renew="nope", today=TODAY)


def test_accept_refuses_symlinked_governor_dir(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".governor").symlink_to(outside)
    with pytest.raises(ReviewError):
        _ok(repo)
    assert list(outside.iterdir()) == []


def test_accept_cli_exits_2_on_write_error(repo: Path) -> None:
    (repo / ".governor").write_text("a file, not a directory")
    argv = ["review", "accept", "--repo", str(repo), "--id", "a1", "--subject", "docs/a.md", "--statement", "s"]
    assert main(argv) == 2


def test_package_without_acceptances_has_no_acceptance_keys(repo: Path) -> None:
    manifest = build_package(repo, staged=True, today=TODAY).manifest
    assert "acceptances" not in manifest and "acceptances_dropped" not in manifest


def test_package_includes_live_acceptance(repo: Path) -> None:
    _put(repo, [_entry(expired_at="2026-11-16")])
    manifest = build_package(repo, staged=True, today=TODAY).manifest
    assert manifest["acceptances"] == [_entry(expired_at="2026-11-16")]
    assert manifest["acceptances_dropped"] == 0


def test_package_drops_and_counts_expired_acceptance(repo: Path) -> None:
    _put(repo, [_entry(expired_at="2026-11-15"), _entry(id="other", subject="elsewhere.md", expired_at="2026-11-15")])
    manifest = build_package(repo, staged=True, today=TODAY).manifest
    assert manifest["acceptances"] == []
    assert manifest["acceptances_dropped"] == 1


def test_package_diff_bytes_unchanged_by_acceptances(repo: Path, tmp_path: Path) -> None:
    plain = build_package(repo, staged=True, today=TODAY, out_dir=tmp_path / "p1")
    _put(repo, [_entry()])
    with_acc = build_package(repo, staged=True, today=TODAY, out_dir=tmp_path / "p2")
    assert plain.diff_path.read_bytes() == with_acc.diff_path.read_bytes()
    assert plain.manifest["diff_sha256"] == with_acc.manifest["diff_sha256"]


def test_package_after_accept_writes_new_stem(repo: Path) -> None:
    first = build_package(repo, staged=True, today=TODAY)
    _ok(repo)
    second = build_package(repo, staged=True, today=TODAY)
    assert second.manifest_path != first.manifest_path
    assert second.manifest["acceptances"][0]["id"] == "a1"
