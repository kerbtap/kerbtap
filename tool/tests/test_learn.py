"""LRN-001 to LRN-004: `governor learn` captures incidents and reports the M3 rates."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kerbtap.cli import main
from tests.repo_factory import commit, commit_raw_message, git, init_repo

GATE_LOG = Path(".kerbtap") / "gate-log" / "events.jsonl"


def _gate(repo: Path, lines: list) -> None:
    log = repo / GATE_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n",
        encoding="utf-8",
    )


def _ev(ts: str, decision: str, gate: str = "g") -> dict:
    return {"ts": ts, "gate": gate, "decision": decision}


def _ledger(repo: Path, entries: list) -> Path:
    path = repo / "incidents" / "ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"incidents": entries}), encoding="utf-8")
    return path


def _entry(key: str, disposition: str = "guard", ref: str = "abc") -> dict:
    return {"key": key, "disposition": disposition, "ref": ref}


def _run(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> tuple[int, str]:
    monkeypatch.chdir(repo)
    code = main(["learn"])
    return code, capsys.readouterr().out


def _five(repo: Path) -> list[str]:
    commit(repo, "first")
    sha = commit(repo, 'Revert "first"')
    _gate(repo, [_ev(f"2026-01-0{i}T00:00:00Z", "deny") for i in (1, 2, 3)] + [_ev("2026-01-04T00:00:00Z", "bypass")])
    return [f"gate:2026-01-0{i}T00:00:00Z:g:1" for i in (1, 2, 3, 4)] + [f"revert:{sha}"]


def test_learn_captures_gate_and_revert_triggers(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "r")
    commit(repo, "first")
    sha = commit(repo, 'Revert "first"')
    _gate(repo, [_ev("t1", "deny"), _ev("t2", "deny"), _ev("t3", "bypass"), _ev("t4", "ask")])
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "feature", str(linked))

    code, out = _run(linked, monkeypatch, capsys)

    assert code == 0
    assert "captured: 4" in out
    for key in ("gate:t1:g:1", "gate:t2:g:1", "gate:t3:g:1", f"revert:{sha}"):
        assert key in out
    assert "gate:t4:g" not in out


@pytest.mark.parametrize(
    "entries, named",
    [
        ([_entry("k1", "fixed")], "k1"),
        ([{"disposition": "guard", "ref": "x"}], "entry 0"),
        ([_entry("k1", ref="")], "k1"),
        ([_entry("k1"), _entry("k1")], "k1"),
    ],
)
def test_learn_refuses_malformed_ledger(tmp_path, monkeypatch, capsys, caplog, entries, named) -> None:
    repo = init_repo(tmp_path / "r")
    commit(repo, "first")
    _ledger(repo, entries)

    code, _ = _run(repo, monkeypatch, capsys)

    assert code == 2
    assert named in caplog.text


def test_learn_reports_m3_rates(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "r")
    keys = _five(repo)
    _ledger(repo, [_entry(keys[0]), _entry(keys[1]), _entry(keys[2], "no-guard", "why")])

    code, out = _run(repo, monkeypatch, capsys)

    assert code == 0
    assert "captured: 5" in out
    assert "dispositioned: 3 (60%)" in out
    assert "guard: 2 (40%)" in out
    assert "applies: yes" in out
    assert f"  {keys[3]}" in out and f"  {keys[4]}" in out
    assert "red CI" in out and "post-merge defect fixes" in out


def test_learn_floor_below_four(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "r")
    commit(repo, "first")
    _gate(repo, [_ev(f"t{i}", "deny") for i in (1, 2, 3)])
    _ledger(repo, [_entry(f"gate:t{i}:g:1") for i in (1, 2, 3)])

    _, out = _run(repo, monkeypatch, capsys)

    assert "captured: 3" in out
    assert "guard: 3 (100%)" in out
    assert "applies: no" in out


def test_learn_reports_absent_inputs(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "r")
    commit(repo, "first")

    code, out = _run(repo, monkeypatch, capsys)

    assert code == 0
    assert "gate log: absent" in out
    assert "ledger: absent" in out
    assert "captured: 0" in out
    assert "dispositioned: 0 (n/a)" in out
    assert "guard: 0 (n/a)" in out


def test_learn_counts_unparsed_gate_lines(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "r")
    commit(repo, "first")
    _gate(repo, ["{not json", "[1]", {"gate": "g", "decision": "deny"}, _ev("t1", "deny")])

    _, out = _run(repo, monkeypatch, capsys)

    assert "captured: 1" in out
    assert "3 unparsable line(s)" in out


def test_learn_numbers_events_across_decisions(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "r")
    commit(repo, "first")
    _gate(repo, [_ev("t1", "ask"), _ev("t1", "deny")])

    _, out = _run(repo, monkeypatch, capsys)

    assert "captured: 1" in out
    assert "gate:t1:g:2" in out


def test_learn_counts_unknown_decision_as_unparsed(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "r")
    commit(repo, "first")
    _gate(repo, [_ev("t1", "block"), _ev("t2", "deny")])

    _, out = _run(repo, monkeypatch, capsys)

    assert "captured: 1" in out
    assert "1 unparsable line(s)" in out


def test_learn_reads_non_utf8_revert_subject(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "r")
    commit(repo, "first")
    sha = commit_raw_message(repo, b'Revert "caf\xe9"\n')

    code, out = _run(repo, monkeypatch, capsys)

    assert code == 0
    assert f"revert:{sha}" in out


def test_learn_counts_uncaptured_ledger_keys(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "r")
    commit(repo, "first")
    _gate(repo, [_ev("t1", "deny")])
    _ledger(repo, [_entry("gate:t1:g:1"), _entry("gate:gone:g:1")])

    _, out = _run(repo, monkeypatch, capsys)

    assert "captured: 1" in out
    assert "dispositioned: 1 (100%)" in out
    assert "ledger keys not captured: 1" in out


def test_learn_counts_same_second_events_separately(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "r")
    commit(repo, "first")
    _gate(repo, [_ev("t1", "deny"), _ev("t1", "deny")])

    _, out = _run(repo, monkeypatch, capsys)

    assert "captured: 2" in out
    assert "gate:t1:g:1" in out and "gate:t1:g:2" in out


def test_learn_reports_failed_git_log(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "r")
    commit(repo, "first")
    real = subprocess.check_output

    def fail_log(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "log"]:
            raise subprocess.CalledProcessError(128, cmd)
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", fail_log)

    code, out = _run(repo, monkeypatch, capsys)

    assert code == 0
    assert "revert history: not read" in out


def test_learn_outside_git_repo(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    plain = tmp_path / "plain"
    plain.mkdir()

    code, _ = _run(plain, monkeypatch, capsys)

    assert code == 2


def test_learn_clear_is_not_an_incident(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "r")
    commit(repo, "first")
    _gate(repo, [_ev("t1", "deny"), _ev("t2", "clear")])

    _, out = _run(repo, monkeypatch, capsys)

    assert "captured: 1" in out
    assert "unparsable" not in out


def test_learn_reports_shadow_denies(tmp_path, monkeypatch, capsys) -> None:
    repo = init_repo(tmp_path / "r")
    commit(repo, "first")
    _gate(repo, [_ev("t1", "shadow-deny", "precommit_review_guard")])

    _, out = _run(repo, monkeypatch, capsys)

    assert "shadow denies: precommit_review_guard: 1" in out
    assert "captured: 0" in out
    assert "unparsable" not in out
