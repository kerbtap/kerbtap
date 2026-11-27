#!/usr/bin/env python3
"""Integration test for fact_claim_guard.py.

Drives the actual hook as a subprocess, exactly as the harness invokes it: an
Edit/Write JSON payload on stdin, ``CLAUDE_PROJECT_DIR`` in the environment, and
the deny decision (or silence) read back from stdout.

Both directions are exercised for every check the guard makes — the figure that
must be blocked AND the figure that must not be, the ledger row that backs a claim
AND the row that only looks like it does. A guard that blocks everything is as
useless as one that blocks nothing, and only the paired cases tell them apart.

Run against the SHIPPED defaults (``docs/**.md`` in scope, ledger at
``docs/sources-ledger.md``). After adapting the TODO constants for a project,
adapt the fixtures below with them.

Run: ``python3 .claude/hooks/test_fact_claim_guard.py``
(exit 0 = all pass). No pytest dependency — plain asserts so it runs anywhere the
hook does.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "fact_claim_guard.py"

DOC = "docs/market.md"
LEDGER = "docs/sources-ledger.md"

CLAIM = "Roughly 60% of surveyed businesses reported an incident last year.\n"
RATIO_CLAIM = "About 1 in 5 companies never recovered.\n"
INTERNAL = "Gross margin holds at 82% at the base-case price point.\n"

LEDGER_VERIFIED = """# Sources ledger

| figure | claim | source | url | quote | status |
|---|---|---|---|---|---|
| 60% | surveyed businesses reporting an incident | Example Org, 2026 | https://example.org/r | "60% of respondents reported an incident" | Verified 2026-07-01 |
"""

LEDGER_UNVERIFIED = LEDGER_VERIFIED.replace("Verified 2026-07-01", "UNVERIFIED — chasing primary source")

LEDGER_PROSE_ONLY = """# Sources ledger

We discussed the 60% figure on 2026-07-01 but never traced it to a primary source.
"""


def _write(cwd, rel, text):
    p = Path(cwd) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _run_hook(cwd, file_path=DOC, added=CLAIM, field="new_string", env=None, payload=None):
    if payload is None:
        payload = json.dumps(
            {
                "tool_name": "Edit" if field == "new_string" else "Write",
                "tool_input": {"file_path": file_path, field: added},
                "cwd": cwd,
            }
        )
    proc_env = {**os.environ, "CLAUDE_PROJECT_DIR": cwd, **(env or {})}
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, text=True,
        capture_output=True, env=proc_env,
    )
    denied = '"permissionDecision": "deny"' in proc.stdout
    return denied, proc.stdout


# --- checks ---------------------------------------------------------------


def check_unbacked_claim_denies(tmp):
    denied, out = _run_hook(tmp)  # no ledger at all
    if not denied:
        return f"unbacked empirical claim passed (stdout={out!r})"
    if "60%" not in out:
        return f"deny reason must list the offending figure: {out!r}"
    return None


def check_verified_row_passes(tmp):
    _write(tmp, LEDGER, LEDGER_VERIFIED)
    denied, out = _run_hook(tmp)
    if denied:
        return f"claim backed by a verified ledger row was denied (stdout={out!r})"
    return None


def check_unverified_row_denies(tmp):
    _write(tmp, LEDGER, LEDGER_UNVERIFIED)
    denied, out = _run_hook(tmp)
    if not denied:
        return f"a row flagged UNVERIFIED backed the claim anyway (stdout={out!r})"
    return None


def check_prose_mention_denies(tmp):
    """Only table rows are verification records — prose that names the figure is not."""
    _write(tmp, LEDGER, LEDGER_PROSE_ONLY)
    denied, out = _run_hook(tmp)
    if not denied:
        return f"ledger prose was accepted as a verification record (stdout={out!r})"
    return None


def check_internal_number_passes(tmp):
    denied, out = _run_hook(tmp, added=INTERNAL)
    if denied:
        return f"an internal model number was treated as an empirical claim (stdout={out!r})"
    return None


def check_ratio_form_denies(tmp):
    denied, out = _run_hook(tmp, added=RATIO_CLAIM)
    if not denied:
        return f"the 'N in M' ratio form was not matched (stdout={out!r})"
    if "1 in 5" not in out:
        return f"deny reason must list the ratio: {out!r}"
    return None


def check_decimal_and_spacing_normalised(tmp):
    """`20,6 %` in the doc must match `20,6%` in the ledger row."""
    _write(
        tmp,
        LEDGER,
        "| figure | claim | source | url | quote | status |\n|---|---|---|---|---|---|\n"
        '| 20,6% | share of firms | Example Org, 2026 | https://example.org/r | "20,6% of firms" | Verified 2026-07-01 |\n',
    )
    denied, out = _run_hook(tmp, added="Some 20,6 % of firms reported the same.\n")
    if denied:
        return f"spacing/decimal normalisation failed to match the ledger row (stdout={out!r})"
    return None


def check_empty_evidence_cells_denies(tmp):
    _write(tmp, LEDGER, "| 60% | | | | | Verified 2026-01-01 |\n")
    denied, out = _run_hook(tmp)
    if not denied:
        return f"a row with empty evidence cells backed the claim (stdout={out!r})"
    return None


def check_date_only_status_denies(tmp):
    _write(tmp, LEDGER, LEDGER_VERIFIED.replace("Verified 2026-07-01", "2026-07-01"))
    denied, out = _run_hook(tmp)
    if not denied:
        return f"a date without the word verified backed the claim (stdout={out!r})"
    return None


def check_figure_outside_figure_cell_denies(tmp):
    _write(tmp, LEDGER, LEDGER_VERIFIED.replace("| 60% |", "| 75% |", 1))
    denied, out = _run_hook(tmp)
    if not denied:
        return f"a figure found only in the quote cell backed the claim (stdout={out!r})"
    return None


def check_longer_figure_does_not_back(tmp):
    _write(tmp, LEDGER, LEDGER_VERIFIED.replace("| 60% |", "| 160% |", 1))
    denied, out = _run_hook(tmp)
    if not denied:
        return f"a figure cell of 160% backed a claim of 60% (stdout={out!r})"
    return None


def _deny_for_row(tmp, row):
    _write(tmp, LEDGER, row + "\n")
    return _run_hook(tmp)


def check_empty_leading_cell_denies(tmp):
    row = "|" + LEDGER_VERIFIED.splitlines()[-1]
    denied, out = _deny_for_row(tmp, row)
    if not denied:
        return f"a row starting with || backed the claim (stdout={out!r})"
    return None


def check_empty_trailing_cell_denies(tmp):
    row = LEDGER_VERIFIED.splitlines()[-1] + "|"
    denied, out = _deny_for_row(tmp, row)
    if not denied:
        return f"a row ending with || backed the claim (stdout={out!r})"
    return None


def check_wrong_cell_count_denies(tmp):
    row = '| 60% | surveyed businesses | Example Org, 2026 | https://example.org/r | Verified 2026-07-01 |'
    denied, out = _deny_for_row(tmp, row)
    if not denied:
        return f"a five-cell row backed the claim (stdout={out!r})"
    return None


def check_multiedit_is_scanned(tmp):
    payload = json.dumps(
        {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": DOC,
                "edits": [
                    {"old_string": "a", "new_string": INTERNAL},
                    {"old_string": "b", "new_string": CLAIM},
                    "junk",
                    {"old_string": "c", "new_string": 5},
                ],
            },
            "cwd": tmp,
        }
    )
    denied, out = _run_hook(tmp, payload=payload)
    if not denied:
        return f"an unbacked claim in the second MultiEdit entry passed (stdout={out!r})"
    return None


def check_multiedit_malformed_edits_is_silent(tmp):
    payload = json.dumps(
        {"tool_name": "MultiEdit", "tool_input": {"file_path": DOC, "edits": "nope"}, "cwd": tmp}
    )
    denied, out = _run_hook(tmp, payload=payload)
    if denied or out.strip():
        return f"a non-list edits should be silent, got stdout={out!r}"
    return None


def check_export_path_out_of_scope(tmp):
    denied, out = _run_hook(tmp, file_path="docs/exports/deck.md")
    if denied:
        return f"generated export was policed (stdout={out!r})"
    return None


def check_ledger_itself_out_of_scope(tmp):
    denied, out = _run_hook(tmp, file_path=LEDGER)
    if denied:
        return f"the ledger itself was policed (stdout={out!r})"
    return None


def check_non_markdown_out_of_scope(tmp):
    denied, out = _run_hook(tmp, file_path="docs/notes.txt")
    if denied:
        return f"a non-markdown file was policed (stdout={out!r})"
    return None


def check_out_of_docs_tree_out_of_scope(tmp):
    denied, out = _run_hook(tmp, file_path="README.md")
    if denied:
        return f"a file outside the in-scope tree was policed (stdout={out!r})"
    return None


def check_write_content_is_scanned(tmp):
    """A whole-file Write must be scanned, not only an Edit's new_string."""
    denied, out = _run_hook(tmp, field="content")
    if not denied:
        return f"Write.content was not scanned (stdout={out!r})"
    return None


BYPASS = {"PROJECT_CLAIM_VERIFIED": "1", "PROJECT_CLAIM_VERIFIED_REASON": "x"}
GATE_LOG = Path(".kerbtap") / "gate-log" / "events.jsonl"


def _gate_log(repo):
    path = Path(repo) / GATE_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _repo(tmp):
    subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)


def check_env_bypass_passes(tmp):
    _repo(tmp)
    denied, out = _run_hook(tmp, env=BYPASS)
    if denied:
        return f"the documented env bypass did not clear the guard (stdout={out!r})"
    return None


def check_deny_and_bypass_logged(tmp):
    """GEL-001: an unbacked figure logs one deny; the bypass with a reason logs one bypass."""
    _repo(tmp)
    denied, _ = _run_hook(tmp)
    after_deny = _gate_log(tmp)
    bypassed, _ = _run_hook(tmp, env=BYPASS)
    lines = _gate_log(tmp)
    if not (
        denied and not bypassed and len(after_deny) == 1 and len(lines) == 2
        and after_deny[0]["gate"] == "fact_claim_guard"
        and after_deny[0]["decision"] == "deny"
        and lines[1]["decision"] == "bypass"
        and lines[1]["bypass_form"] == "session-env"
        and lines[1]["bypass_reason"] == "x"
    ):
        return f"denied={denied} bypassed={not bypassed} log={lines}"
    return None


def check_bypass_without_reason_is_refused(tmp):
    """GEL-002: a bare bypass denies, and the line says why."""
    _repo(tmp)
    denied, out = _run_hook(tmp, env={"PROJECT_CLAIM_VERIFIED": "1"})
    codes = [l["reason_code"] for l in _gate_log(tmp)]
    if not (denied and "bypass-no-reason" in out and codes == ["bypass-no-reason"]):
        return f"denied={denied} log={codes}"
    return None


def check_expired_bypass_is_refused(tmp):
    """GEL-003: a past expiry denies; a future one clears."""
    _repo(tmp)
    denied, _ = _run_hook(tmp, env={**BYPASS, "PROJECT_CLAIM_VERIFIED_UNTIL": "2000-01-01T00:00:00Z"})
    codes = [l["reason_code"] for l in _gate_log(tmp)]
    future, _ = _run_hook(tmp, env={**BYPASS, "PROJECT_CLAIM_VERIFIED_UNTIL": "2999-01-01T00:00:00Z"})
    if not (denied and not future and codes == ["bypass-expired"]):
        return f"denied={denied} future denied={future} log={codes}"
    return None


def check_unwritable_record_refuses_bypass(tmp):
    """GEL-004: with no way to write the record the bypass does not clear the gate."""
    _repo(tmp)
    (Path(tmp) / ".governor").mkdir()
    (Path(tmp) / ".governor" / "gate-log").write_text("not a directory", encoding="utf-8")
    denied, out = _run_hook(tmp, env=BYPASS)
    if not (denied and "bypass-unrecorded" in out):
        return f"denied={denied} stdout={out!r}"
    return None


def check_malformed_tool_input_is_silent(tmp):
    payload = json.dumps({"tool_name": "Edit", "tool_input": None, "cwd": tmp})
    denied, out = _run_hook(tmp, payload=payload)
    if denied or out.strip():
        return f"malformed tool_input should be silent, got stdout={out!r}"
    return None


def check_malformed_stdin_is_silent(tmp):
    denied, out = _run_hook(tmp, payload="not json")
    if denied or out.strip():
        return f"malformed stdin should be silent, got stdout={out!r}"
    return None


CHECKS = [
    ("unbacked empirical claim is denied", check_unbacked_claim_denies),
    ("verified ledger row lets the claim land", check_verified_row_passes),
    ("row flagged UNVERIFIED still denies", check_unverified_row_denies),
    ("ledger prose is not a verification record", check_prose_mention_denies),
    ("internal model number passes", check_internal_number_passes),
    ("'N in M' ratio form is matched", check_ratio_form_denies),
    ("decimal/spacing normalisation matches", check_decimal_and_spacing_normalised),
    ("row with empty evidence cells denies", check_empty_evidence_cells_denies),
    ("date without the word verified denies", check_date_only_status_denies),
    ("figure outside the figure cell denies", check_figure_outside_figure_cell_denies),
    ("longer figure in the figure cell does not back", check_longer_figure_does_not_back),
    ("empty leading cell denies", check_empty_leading_cell_denies),
    ("empty trailing cell denies", check_empty_trailing_cell_denies),
    ("five-cell row denies", check_wrong_cell_count_denies),
    ("MultiEdit edits are scanned", check_multiedit_is_scanned),
    ("non-list MultiEdit edits is silent", check_multiedit_malformed_edits_is_silent),
    ("generated export is out of scope", check_export_path_out_of_scope),
    ("the ledger itself is out of scope", check_ledger_itself_out_of_scope),
    ("non-markdown file is out of scope", check_non_markdown_out_of_scope),
    ("file outside the docs tree is out of scope", check_out_of_docs_tree_out_of_scope),
    ("Write.content is scanned", check_write_content_is_scanned),
    ("env bypass clears the guard", check_env_bypass_passes),
    ("check_deny_and_bypass_logged", check_deny_and_bypass_logged),
    ("check_bypass_without_reason_is_refused", check_bypass_without_reason_is_refused),
    ("check_expired_bypass_is_refused", check_expired_bypass_is_refused),
    ("check_unwritable_record_refuses_bypass", check_unwritable_record_refuses_bypass),
    ("malformed tool_input is silent", check_malformed_tool_input_is_silent),
    ("malformed stdin is silent", check_malformed_stdin_is_silent),
]


def main():
    failures = []
    for desc, check in CHECKS:
        with tempfile.TemporaryDirectory() as tmp:
            problem = check(tmp)
        if problem:
            failures.append(f"FAIL: {desc}\n  {problem}")
        else:
            print(f"pass: {desc}")

    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"\n{len(failures)}/{len(CHECKS)} case(s) failed", file=sys.stderr)
        sys.exit(1)
    print(f"\nall {len(CHECKS)} cases passed")


if __name__ == "__main__":
    main()
