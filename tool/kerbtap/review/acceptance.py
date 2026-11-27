"""``kerbtap review accept`` — accepted review residuals with an end date.

An accepted finding lives in the tracked ``<state folder>/acceptances.json`` so a reviewer stops
raising it every round. Each entry expires: expiry forces a re-look, and ``renewals`` counts
the re-acceptances so a permanent acceptance cannot hide behind thirty-day rewrites. A hand
edit of the file outside the CLI is not detected here; git history is the record for that.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from kerbtap.review.record import ReviewError, normalize_subject, real_path_under
from kerbtap.state import state_rel

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 30
MAX_DAYS = 60
_FIELDS = ("id", "subject", "statement", "accepted_at", "expired_at", "renewals")


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _path(repo: Path) -> Path:
    return real_path_under(repo, state_rel(repo, "acceptances.json").parts)


def _parse_date(value: object, what: str) -> date:
    if not isinstance(value, str):
        raise ReviewError(f"{what} must be a YYYY-MM-DD date, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ReviewError(f"{what} must be a YYYY-MM-DD date, got {value!r}") from exc


def _validate(entry: object) -> dict:
    if not isinstance(entry, dict) or set(entry) != set(_FIELDS):
        raise ReviewError(f"an acceptance needs exactly these fields: {', '.join(_FIELDS)}")
    if not isinstance(entry["id"], str) or not entry["id"]:
        raise ReviewError("an acceptance needs a non-empty `id`")
    normalize_subject([entry["subject"]])
    if not isinstance(entry["statement"], str) or not entry["statement"].strip():
        raise ReviewError(f"acceptance {entry['id']!r} needs a non-empty `statement`")
    _parse_date(entry["accepted_at"], "accepted_at")
    _parse_date(entry["expired_at"], "expired_at")
    renewals = entry["renewals"]
    if isinstance(renewals, bool) or not isinstance(renewals, int) or renewals < 0:
        raise ReviewError(f"acceptance {entry['id']!r} needs a non-negative integer `renewals`")
    return entry


def load(repo: Path) -> list[dict]:
    """Read and validate the acceptances; a missing file is an empty list."""
    path = _path(repo)
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewError(f"acceptances file unreadable: {exc}") from exc
    if not isinstance(entries, list):
        raise ReviewError("the acceptances file must hold a JSON list")
    seen: set[str] = set()
    for entry in entries:
        _validate(entry)
        if entry["id"] in seen:
            raise ReviewError(f"acceptance id {entry['id']!r} appears twice")
        seen.add(entry["id"])
    return entries


def _check_expiry(entry: dict, today: date) -> None:
    expired = _parse_date(entry["expired_at"], "expired_at")
    if expired < today:
        raise ReviewError(f"expired_at {expired} is before {today}")
    if expired > today + timedelta(days=MAX_DAYS):
        raise ReviewError(f"expired_at {expired} is more than {MAX_DAYS} days after {today}")


def write_entries(repo: Path, entries: list[dict], *, today: date, renewed: str | None = None) -> None:
    """Validate and write the whole list; refuse a renewal that rewrites its history."""
    seen: set[str] = set()
    for entry in entries:
        _validate(entry)
        if entry["id"] in seen:
            raise ReviewError(f"acceptance id {entry['id']!r} appears twice")
        seen.add(entry["id"])
    prior = {e["id"]: e for e in load(repo)}
    for entry in entries:
        old = prior.get(entry["id"])
        if old is None or old == entry:
            if old is None:
                _check_expiry(entry, today)
            continue
        if entry["id"] != renewed:
            raise ReviewError(f"acceptance {entry['id']!r} changed outside a renewal")
        if entry["accepted_at"] != old["accepted_at"]:
            raise ReviewError(f"a renewal must keep accepted_at of {entry['id']!r}")
        if entry["renewals"] != old["renewals"] + 1:
            raise ReviewError(f"a renewal must raise renewals of {entry['id']!r} by exactly one")
        _check_expiry(entry, today)
    path = _path(repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ReviewError(f"cannot write {path}: {exc}") from exc


def accept(
    repo: Path,
    *,
    id: str | None = None,
    subject: str | None = None,
    statement: str | None = None,
    expired_at: str | None = None,
    renew: str | None = None,
    today: date | None = None,
) -> dict:
    """Add one acceptance, or renew one; return the entry written."""
    today = today or utc_today()
    entries = load(repo)
    new_expiry = expired_at or (today + timedelta(days=DEFAULT_DAYS)).isoformat()
    if renew is not None:
        match = next((e for e in entries if e["id"] == renew), None)
        if match is None:
            raise ReviewError(f"no acceptance with id {renew!r} to renew")
        entry = {**match, "renewals": match["renewals"] + 1, "expired_at": new_expiry}
        entries = [entry if e["id"] == renew else e for e in entries]
    else:
        entry = {
            "id": id,
            "subject": subject,
            "statement": statement,
            "accepted_at": today.isoformat(),
            "expired_at": new_expiry,
            "renewals": 0,
        }
        if any(e["id"] == id for e in entries):
            raise ReviewError(f"acceptance id {id!r} already exists; use --renew to extend it")
        entries.append(entry)
    write_entries(repo, entries, today=today, renewed=renew)
    logger.info("Acceptance %s written, renewals %d", entry["id"], entry["renewals"])
    return entry


def split_for_paths(entries: list[dict], paths: list[str], today: date) -> tuple[list[dict], int]:
    """The live entries whose subject is in ``paths``, and the count of expired ones."""
    live, dropped = [], 0
    for entry in entries:
        if entry["subject"] not in paths:
            continue
        if _parse_date(entry["expired_at"], "expired_at") >= today:
            live.append(entry)
        else:
            dropped += 1
    return live, dropped
