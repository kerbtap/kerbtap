#!/usr/bin/env python3
"""PreToolUse hook — block unfounded empirical claims in fact-facing docs.

Origin: a pitch deck shipped the claim "60% of breached SMBs close within six
months" attributed to a vendor that never published it — in reality a ~2012 figure
the original body disavowed a decade later. It HAD a citation, so a "does this
have a citation?" check would not have caught it. The failure was that the
citation was never verified against its primary source.

A hook cannot fetch the web and confirm a number is true. What it CAN do is make
shipping an empirical claim require a recorded, auditable verification: every
statistic in a fact-facing doc must have a row in the sources ledger carrying a
primary-source URL, a verbatim supporting quote, and a verification date. This
hook enforces that the record exists (and is not flagged unverified/disputed)
before the claim can land. The fetch-and-quote is done by a human or agent and
written into the ledger; this hook is the deterministic gate that the record was
actually made.

What is checked: only figures in the ADDED text (``Edit.new_string`` /
``Write.content`` / each ``MultiEdit.edits[].new_string``) that read as EMPIRICAL claims about the world — a percentage or
an "N in M" ratio sitting on a line that also names an empirical subject. Internal
model numbers (prices, margins, client counts, ARR) carry no such subject word and
are ignored, so the guard stays low-noise.

Verdict: DENY (hard block) if any such figure is absent from the ledger or present
only on a row marked UNVERIFIED / DISPUTED / DEBUNKED / RETIRED / PENDING. The
block lists the offending figures.

Bypass (deliberate, audited): set ``PROJECT_CLAIM_VERIFIED=1`` and
``PROJECT_CLAIM_VERIFIED_REASON=<why>`` in the session env (optional
``PROJECT_CLAIM_VERIFIED_UNTIL=YYYY-MM-DDTHH:MM:SSZ``). Each deny and each bypass is one
line in the gate log; a bypass with no reason, a past expiry or an unwritable
log is refused.
Intended for edits where the ledger genuinely already covers the claim in prose the
matcher cannot see — not a routine escape.

Limitation, stated honestly: this verifies a verification was RECORDED, not that
the live source still says it. It matches numeric figures only, not word-form
stats ("four in ten"). It is friction-grade, not an adversarial boundary — its job
is to stop the specific failure above from recurring silently.

TODO: adapt for <PROJECT>
  1. ``LEDGER_REL`` — where this project keeps its sources ledger, and create it
     with the row format named in :func:`_reason`.
  2. ``IN_SCOPE_RES`` / ``OUT_OF_SCOPE_RES`` — which docs are fact-facing. Build
     outputs and the ledger itself must stay out of scope.
  3. ``SUBJECT`` — the empirical-subject vocabulary of this project's domain. Too
     narrow and the guard never fires; too wide and every internal number trips it.
  4. ``BYPASS_ENV`` — a project-specific name, kept in sync with the project's
     CLAUDE.md hook-contracts table.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from commit_scope import check_bypass, record_event
except ImportError:  # pragma: no cover - the engine is missing
    # Without the log writer no bypass can be recorded, so none clears the gate.
    def record_event(directory, event):
        return False

    def check_bypass(directory, gate, form, reason, until=None):
        return "bypass-unrecorded"

GATE = "fact_claim_guard"
LEDGER_REL = "docs/sources-ledger.md"  # TODO: adapt for <PROJECT>
BYPASS_ENV = "PROJECT_CLAIM_VERIFIED"  # TODO: adapt for <PROJECT>

# TODO: adapt for <PROJECT> — fact-facing docs this guard polices.
IN_SCOPE_RES = (re.compile(r"(^|/)docs/.+\.md$"),)
# TODO: adapt for <PROJECT> — generated output that must never be policed.
OUT_OF_SCOPE_RES = (re.compile(r"(^|/)docs/exports/"),)

FIG_PCT = re.compile(r"\d+(?:[.,]\d+)?\s*%")
FIG_RATIO = re.compile(r"\b\d+\s+in\s+\d+\b", re.IGNORECASE)
# TODO: adapt for <PROJECT> — the words that make a number a claim about the world
# rather than a number about this business.
SUBJECT = re.compile(
    r"\b(smbs?|business(?:es)?|compan(?:y|ies)|firms?|teams?|developers?|"
    r"engineers?|enterprises?|organi[sz]ations?|respondents?|surveyed|adopt\w*|"
    r"breach(?:ed|es)?|incidents?|findings?|vulnerab\w*|close[ds]?|closure)\b",
    re.IGNORECASE,
)
STATUS_VERIFIED = re.compile(r"\bverified\b", re.IGNORECASE)
REDFLAG = re.compile(
    r"unverif|debunk|disput|removed|retired|pending|do not use", re.IGNORECASE
)


def _in_scope(file_path: str) -> bool:
    """True iff *file_path* is a fact-facing doc this guard polices."""
    p = file_path.replace("\\", "/")
    if p.endswith(LEDGER_REL):
        return False  # the ledger IS the verification record — never police it
    if any(pattern.search(p) for pattern in OUT_OF_SCOPE_RES):
        return False
    return any(pattern.search(p) for pattern in IN_SCOPE_RES)


def _added_text(data: dict) -> str:
    ti = data.get("tool_input")
    if not isinstance(ti, dict):
        return ""
    parts = []
    for key in ("new_string", "content"):
        if isinstance(ti.get(key), str):
            parts.append(ti[key])
    edits = ti.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict) and isinstance(edit.get("new_string"), str):
                parts.append(edit["new_string"])
    return "\n".join(parts)


def _figures(text: str) -> list[str]:
    """Empirical figures: a %/ratio on a line that also names an empirical subject."""
    out: list[str] = []
    for line in text.splitlines():
        if not SUBJECT.search(line):
            continue
        for m in FIG_PCT.findall(line):
            out.append(re.sub(r"\s+", "", m))  # "20,6 %" -> "20,6%"
        for m in FIG_RATIO.findall(line):
            out.append(re.sub(r"\s+", " ", m).strip())  # "1 in 5"
    seen: set[str] = set()
    uniq = []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def _ledger_rows(project_dir: Path) -> list[str]:
    """Only markdown TABLE rows count as verification records — never prose.

    Prose in the ledger can legitimately mention a figure and a date while
    discussing it; that must not accidentally 'back' the claim.
    """
    led = project_dir / LEDGER_REL
    try:
        lines = led.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    return [ln for ln in lines if ln.lstrip().startswith("|")]


def _is_backed(fig: str, rows: list[str]) -> bool:
    is_pct = fig.endswith("%")
    fig_key = fig.replace(" ", "") if is_pct else fig.lower()
    for row in rows:
        body = row.strip()
        if body.startswith("|"):
            body = body[1:]
        if body.endswith("|"):
            body = body[:-1]
        cells = [c.strip() for c in body.split("|")]
        if len(cells) != 6:
            continue
        cell_key = cells[0].replace(" ", "") if is_pct else cells[0].lower()
        if not re.search(r"(?<![\d.,])" + re.escape(fig_key) + r"(?!\d)", cell_key):
            continue
        if not (cells[2] and cells[3] and cells[4]):
            continue
        if STATUS_VERIFIED.search(cells[5]) and not REDFLAG.search(row):
            return True
    return False


def _reason(file_path: str, unbacked: list[str]) -> str:
    return (
        "FACT-CLAIM GUARD — unverified empirical claim(s) blocked.\n\n"
        f"These figures appear in your edit to {file_path} but have no VERIFIED "
        f"row in {LEDGER_REL}:\n"
        + "".join(f"  • {f}\n" for f in unbacked)
        + "\nEvery statistic in a fact-facing doc must be backed by a primary "
        "source someone actually checked. Before this can land:\n"
        "  1. Fetch the primary source and copy the VERBATIM line that states "
        "the figure.\n"
        f"  2. Add a single-line row to {LEDGER_REL}: | figure | claim | source "
        '(org, year) | URL-or-repo-path | "verbatim quote" | Verified '
        "<YYYY-MM-DD> |\n"
        "  3. Re-run the edit.\n\n"
        "Do NOT invent, guess, or trust a second-hand attribution — that is "
        "exactly the failure this guard exists to prevent.\n"
        "Deliberate override (only when the ledger already covers it in prose "
        f"this matcher cannot see): set {BYPASS_ENV}=1 and {BYPASS_ENV}_REASON=\"<why>\" "
        f"(optional {BYPASS_ENV}_UNTIL=YYYY-MM-DDTHH:MM:SSZ) in the session env. A bypass "
        f"with no reason, a past expiry or an unwritable gate log is refused."
    )


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)
    if not isinstance(data, dict):
        sys.exit(0)

    tool_input = data.get("tool_input")
    file_path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
    if not isinstance(file_path, str) or not file_path or not _in_scope(file_path):
        sys.exit(0)

    figures = _figures(_added_text(data))
    if not figures:
        sys.exit(0)

    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or ".")
    unbacked = [f for f in figures if not _is_backed(f, _ledger_rows(project_dir))]
    if not unbacked:
        sys.exit(0)

    reason = _reason(file_path, unbacked)
    if os.environ.get(BYPASS_ENV) == "1":
        refusal = check_bypass(
            str(project_dir), GATE, "session-env",
            os.environ.get(f"{BYPASS_ENV}_REASON"), os.environ.get(f"{BYPASS_ENV}_UNTIL"),
        )
        if refusal is None:
            sys.exit(0)
        reason += f"\n\nBYPASS REFUSED: {refusal}."
    else:
        record_event(str(project_dir), {"gate": GATE, "decision": "deny", "reason_code": "unbacked-figure"})
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
