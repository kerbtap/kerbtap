"""``kerbtap learn``: capture the mechanical incident triggers and report the M3 rates.

An incident is a gate-log ``deny`` or
``bypass`` event, or a revert commit. A tracked ledger pairs each incident with a
disposition. The gate log is a local, editable file, so the counts are a record, not a proof.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from kerbtap.collect.governance import gate_log_path

logger = logging.getLogger(__name__)

DISPOSITIONS = ("guard", "no-guard")
_DECISIONS = ("deny", "ask", "bypass", "clear", "shadow-deny")  # the values `record_event` writes; a clear or shadow deny is no incident
FLOOR = 4
NOT_CAPTURED = "red CI runs on the default branch, post-merge defect fixes"


class LedgerError(Exception):
    """The incident ledger is malformed."""


def capture(repo_path: Path) -> dict[str, object]:
    """Return ``{"keys": [...], "gate_log_present": bool, "unparsed": int}``."""
    keys: dict[str, None] = {}
    seen: dict[str, int] = {}
    unparsed = 0
    shadow: dict[str, int] = {}
    path = gate_log_path(repo_path)
    present = False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        present = True
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        logger.warning("Gate log %s could not be read: %s", path, exc)
        text = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            unparsed += 1
            continue
        if not isinstance(record, dict) or not isinstance(record.get("ts"), str):
            unparsed += 1
            continue
        decision = record.get("decision")
        if decision not in _DECISIONS or not isinstance(record.get("gate"), str):
            unparsed += 1
            continue
        base = f"gate:{record['ts']}:{record['gate']}"
        seen[base] = seen.get(base, 0) + 1
        if decision == "shadow-deny":
            shadow[record["gate"]] = shadow.get(record["gate"], 0) + 1
        if decision in ("deny", "bypass"):
            keys[f"{base}:{seen[base]}"] = None
    logger.info("Read %d gate-log incidents (%d unparsed lines)", len(keys), unparsed)
    gate_count = len(keys)
    out = ""
    reverts_read = True
    try:
        subprocess.check_output(
            ["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=repo_path,
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        )
    except subprocess.CalledProcessError:
        pass  # no commits: no reverts
    except (subprocess.SubprocessError, OSError) as exc:
        reverts_read = False
        logger.warning("Revert history could not be read: %s", exc)
    else:
        try:
            out = subprocess.check_output(
                ["git", "log", "--format=%H%x00%s", "HEAD"], cwd=repo_path,
                stderr=subprocess.DEVNULL, timeout=30,
            ).decode("utf-8", errors="replace")
        except (subprocess.SubprocessError, OSError) as exc:
            reverts_read = False
            logger.warning("Revert history could not be read: %s", exc)
    for row in out.splitlines():
        sha, _, subject = row.partition("\x00")
        if subject.startswith('Revert "'):
            keys[f"revert:{sha}"] = None
    logger.info("Read %d revert commits", len(keys) - gate_count)
    return {"keys": list(keys), "gate_log_present": present, "unparsed": unparsed,
            "shadow": shadow, "reverts_read": reverts_read}


def load_ledger(path: Path) -> dict[str, dict[str, str]] | None:
    """Return the entries by key, or ``None`` when the file is absent. Raise ``LedgerError``."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise LedgerError(f"ledger {path} cannot be read: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LedgerError(f"ledger {path} is not JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("incidents"), list):
        raise LedgerError(f"ledger {path} must be an object with an 'incidents' list")
    entries: dict[str, dict[str, str]] = {}
    for index, entry in enumerate(data["incidents"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str) or not entry["key"]:
            raise LedgerError(f"ledger entry {index} has no text 'key'")
        key = entry["key"]
        if entry.get("disposition") not in DISPOSITIONS:
            raise LedgerError(
                f"ledger entry {key!r}: 'disposition' must be one of {', '.join(DISPOSITIONS)}"
            )
        if not isinstance(entry.get("ref"), str) or not entry["ref"].strip():
            raise LedgerError(f"ledger entry {key!r}: 'ref' must be a non-empty text")
        if key in entries:
            raise LedgerError(f"ledger entry {index}: duplicate key {key!r}")
        entries[key] = entry
    logger.info("Read %d ledger entries", len(entries))
    return entries


def _rate(n: int, total: int) -> str:
    return f"{n} ({round(100 * n / total)}%)" if total else f"{n} (n/a)"


def report(captured: dict[str, object], ledger: dict[str, dict[str, str]] | None) -> str:
    keys: list[str] = captured["keys"]  # type: ignore[assignment]
    entries = ledger or {}
    total = len(keys)
    undispositioned = [k for k in keys if k not in entries]
    guard = sum(1 for k in keys if k in entries and entries[k]["disposition"] == "guard")
    lines = []
    if not captured["gate_log_present"]:
        lines.append("gate log: absent")
    if captured["unparsed"]:
        lines.append(f"gate log: {captured['unparsed']} unparsable line(s)")
    for gate, n in sorted(captured.get("shadow", {}).items()):  # type: ignore[union-attr]
        lines.append(f"shadow denies: {gate}: {n}")
    if not captured["reverts_read"]:
        lines.append("revert history: not read (git failed); the revert count is incomplete")
    if ledger is None:
        lines.append("ledger: absent")
    lines += [
        f"captured: {total}",
        f"dispositioned: {_rate(total - len(undispositioned), total)}",
        f"guard: {_rate(guard, total)}",
        f"conversion floor ({FLOOR}+ incidents) applies: {'yes' if total >= FLOOR else 'no'}",
        f"ledger keys not captured: {len([k for k in entries if k not in keys])}",
        f"not captured by this command: {NOT_CAPTURED}",
        f"incidents with no disposition: {len(undispositioned)}",
    ]
    lines += [f"  {k}" for k in undispositioned]
    return "\n".join(lines) + "\n"
