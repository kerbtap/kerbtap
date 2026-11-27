"""The Agentic Change Record for one change (vision §6.2): ``kerbtap change-record``.

Reuses the pack's per-change record (``assemble.build_scoped_changes``) and adds the local
pre-commit trail, joined by branch. The local stores are per machine and gitignored, so every
part says which of three states it is in: what is recorded, ``none recorded`` (a git or GitHub
source was read and holds nothing) or ``no local record`` (a local store has no line for the
change). A count never stands in for the second or third state.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from kerbtap.assemble import build_scoped_changes
from kerbtap.collect import git as git_collect
from kerbtap.collect import github
from kerbtap.collect.governance import gate_log_path, gate_log_rel
from kerbtap.honesty import assert_clean, redact_quoted
from kerbtap.model import Change
from kerbtap.state import state_rel

logger = logging.getLogger(__name__)

SCHEMA = "governor.change-record/1"
RECORDED = "recorded"
NONE_RECORDED = "none recorded"
NO_LOCAL = "no local record"
NOT_COLLECTED = "not collected"

_PR_FIELDS = "number,title,state,headRefName,baseRefName,commits,mergeCommit"


class ChangeRecordError(RuntimeError):
    """The change cannot be resolved to commits."""


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=repo, stderr=subprocess.PIPE, text=True, timeout=30
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise ChangeRecordError(f"git {args[0]} failed: {(exc.stderr or '').strip()}") from exc
    except (subprocess.SubprocessError, OSError) as exc:
        raise ChangeRecordError(f"git {args[0]} could not run: {exc}") from exc


def _git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False).returncode == 0


def _gh_pr_view(repo: Path, number: int) -> dict:
    try:
        out = subprocess.run(
            ["gh", "pr", "view", str(number), "--json", _PR_FIELDS],
            cwd=repo, capture_output=True, text=True, check=True, timeout=30,
        ).stdout
        data = json.loads(out)
    except FileNotFoundError as exc:
        raise ChangeRecordError("gh CLI not found; a PR number needs it (pass a branch name instead)") from exc
    except subprocess.CalledProcessError as exc:
        raise ChangeRecordError(f"gh pr view {number} failed: {(exc.stderr or '').strip()[:200]}") from exc
    except (subprocess.SubprocessError, ValueError) as exc:
        raise ChangeRecordError(f"gh pr view {number} gave no usable answer: {exc}") from exc
    if not isinstance(data, dict):
        raise ChangeRecordError(f"gh pr view {number} gave no usable answer")
    return data


def _default_branch(repo: Path) -> str:
    try:
        return _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    except ChangeRecordError:
        pass
    for name in ("main", "master"):
        if _git_ok(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            return name
    raise ChangeRecordError("no default branch found (origin/HEAD, main or master)")


def _resolve_branch(repo: Path, branch: str) -> tuple[list[str], dict]:
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        if _git_ok(repo, "rev-parse", "--verify", "--quiet", ref):
            break
    else:
        raise ChangeRecordError(f"{branch} is neither a PR number nor a branch in this repository")
    base = _default_branch(repo)
    shas = _git(repo, "rev-list", f"{_git(repo, 'merge-base', base, ref)}..{ref}").split()
    if not shas:
        raise ChangeRecordError(f"{branch} holds no commit beyond {base}")
    return shas, {"kind": "branch", "branch": branch, "base": base, "title": None, "state": None}


def _resolve_pr(repo: Path, number: int) -> tuple[list[str], dict]:
    data = _gh_pr_view(repo, number)
    shas = [c["oid"] for c in data.get("commits") or [] if isinstance(c, dict) and c.get("oid")]
    merge = data.get("mergeCommit")
    if isinstance(merge, dict) and merge.get("oid") and merge["oid"] not in shas:
        shas.append(merge["oid"])
    if not shas:
        raise ChangeRecordError(f"PR {number} lists no commits")
    head = next((c["oid"] for c in reversed(data.get("commits") or []) if isinstance(c, dict) and c.get("oid")), None)
    return shas, {
        "kind": "pr", "number": number, "branch": data.get("headRefName"), "base": data.get("baseRefName"),
        "title": data.get("title"), "state": data.get("state"), "head_sha": head,
    }


def _clean(value):
    return redact_quoted(value)[0] if isinstance(value, str) else value


def _read_jsonl(path: Path) -> list[dict]:
    """Every JSON-object line of a local store; a missing or unreadable file is no lines."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        logger.info("Store %s does not exist", path.name)
        return []
    except OSError as exc:
        logger.warning("Store %s could not be read: %s", path.name, exc)
        return []
    lines: list[dict] = []
    for raw in text.splitlines():
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if isinstance(record, dict):
            lines.append(record)
    return lines


def _store(lines: list[dict], rel: Path, **extra) -> dict:
    return {"store": rel.as_posix(), "state": RECORDED if lines else NO_LOCAL, "lines": lines, **extra}


def _local_trail(repo: Path, branch: str | None, keys: list[str]) -> dict:
    root = gate_log_path(repo).parents[2]
    gate_rel = gate_log_rel(root)
    reviews_rel = state_rel(root, "telemetry", "reviews.jsonl")
    verbs_rel = state_rel(root, "telemetry", "ungated-commit-verbs.jsonl")
    gate_lines = [
        {
            "ts": _clean(r.get("ts")), "gate": _clean(r.get("gate")), "decision": _clean(r.get("decision")),
            "reason_code": _clean(r.get("reason_code")), "reason": _clean(r.get("bypass_reason")),
            "expires": _clean(r.get("expires")), "head": _clean(r.get("head")),
        }
        for r in _read_jsonl(root / gate_rel) if branch and r.get("branch") == branch
    ]
    review_lines = [
        {k: _clean(r.get(k)) for k in ("ts", "change", "review", "passes", "blockers", "out_of_scope")}
        for r in _read_jsonl(root / reviews_rel) if r.get("change") in keys
    ]
    verb_lines = [
        {k: _clean(r.get(k)) for k in ("ts", "gate", "verb", "head")}
        for r in _read_jsonl(root / verbs_rel) if branch and r.get("branch") == branch
    ]
    logger.info(
        "Local trail for branch %s: %d gate-log, %d review, %d ungated-verb lines",
        branch, len(gate_lines), len(review_lines), len(verb_lines),
    )
    return {
        "branch": _clean(branch),
        "gate_log": _store(gate_lines, gate_rel),
        "reviews": _store(review_lines, reviews_rel, keys=keys),
        "ungated_verbs": _store(verb_lines, verbs_rel),
    }


def _grouped(trail_part: dict, decisions: tuple[str, ...]) -> dict:
    """The gate-log lines of ``decisions``: none recorded when the store holds the branch but none of them."""
    if trail_part["state"] == NO_LOCAL:
        return {"state": NO_LOCAL, "lines": []}
    lines = [ln for ln in trail_part["lines"] if ln["decision"] in decisions]
    return {"state": RECORDED if lines else NONE_RECORDED, "lines": lines}


def _properties(changes: list[Change], trail: dict, checks: dict | None, keys: list[str]) -> dict:
    gates = Counter((g.gate, g.outcome) for c in changes for g in c.policy_gates)
    states = Counter(c.review.verification for c in changes)
    gate_log = trail["gate_log"]
    reviews = trail["reviews"]
    return {
        "authorized": {"state": RECORDED if keys else NONE_RECORDED, "ticket_refs": keys},
        "policy_bound": {
            "commit_gates": [{"gate": g, "outcome": o, "commits": n} for (g, o), n in sorted(gates.items())],
            "gate_log": _grouped(gate_log, ("deny", "ask", "clear")),
        },
        "verified": {"state": RECORDED, "checks": checks} if checks else {"state": NOT_COLLECTED, "checks": None},
        "approved": {
            "review_states": [{"verification": v, "commits": n} for v, n in sorted(states.items())],
            "review_passes": {"state": reviews["state"], "lines": reviews["lines"]},
        },
        "exception_controlled": {
            "bypasses": _grouped(gate_log, ("bypass",)),
            "shadow_denies": _grouped(gate_log, ("shadow-deny",)),
            "ungated_verbs": {"state": trail["ungated_verbs"]["state"], "lines": trail["ungated_verbs"]["lines"]},
        },
    }


def build_record(repo: Path, target: str) -> dict:
    """The change record of ``target`` (a PR number or a branch name) as one JSON-ready dict."""
    started = time.monotonic()
    repo = repo.resolve()
    if target.isdigit():
        shas, change = _resolve_pr(repo, int(target))
    else:
        shas, change = _resolve_branch(repo, target)
    changes, unread = build_scoped_changes(repo, shas)
    keys = list(dict.fromkeys(
        [r for c in changes for r in c.jira_refs]
        + git_collect.ticket_keys(change.get("title") or "")
    ))
    checks = None
    if change["kind"] == "pr":
        checks = github._checks_block(repo, change.get("head_sha"))
    trail = _local_trail(repo, change["branch"], keys)
    record = {
        "schema": SCHEMA,
        "change": {k: _clean(v) for k, v in change.items() if k != "head_sha"},
        "commits": [asdict(c) for c in changes],
        "unread_commits": unread,
        "local_trail": trail,
        "properties": _properties(changes, trail, checks, keys),
    }
    assert_clean(json.dumps(record, ensure_ascii=False), context="change record")
    logger.info("Change record for %s built from %d commits in %.1fs", target, len(changes), time.monotonic() - started)
    return record


def _lines(part: dict, fmt) -> list[str]:
    if part["state"] != RECORDED:
        return [f"- {part['state']}"]
    return [f"- {fmt(ln)}" for ln in part["lines"]]


def _gate_line(ln: dict) -> str:
    extra = ""
    if ln["decision"] == "bypass":
        extra = f"; reason: {ln['reason'] or NONE_RECORDED}; expires: {ln['expires'] or NONE_RECORDED}"
    return f"{ln['ts']} `{ln['gate']}` {ln['decision']} ({ln['reason_code'] or 'no reason code'}){extra}"


def render_markdown(record: dict) -> str:
    """The record as Markdown; the same content as the JSON form."""
    change, props = record["change"], record["properties"]
    if change["kind"] == "pr":
        title = f"PR #{change['number']}: {change['title']} ({change['state']}, `{change['branch']}` into `{change['base']}`)"
    else:
        title = f"branch `{change['branch']}` (against `{change['base']}`)"
    out = [f"# Change record: {title}", "",
           "Recorded from git, GitHub and this machine's local stores; not a verification of any of them.", "",
           "## Commits", ""]
    for c in record["commits"]:
        ai = c["ai_assisted"]["value"]
        out.append(f"- `{c['change_id']}` {c['subject']} - landed via {c['landed_via']}; AI-assisted: {ai}; "
                   f"review: {c['review']['verification']}")
    if record["unread_commits"]:
        out.append(f"- not readable in this clone: {', '.join(s[:7] for s in record['unread_commits'])}")
    auth = props["authorized"]
    out += ["", "## Authorized", "",
            f"- ticket refs: {', '.join(auth['ticket_refs'])}" if auth["ticket_refs"] else f"- {NONE_RECORDED}"]
    out += ["", "## Policy-bound", ""]
    out += [f"- `{g['gate']}`: {g['outcome']} on {g['commits']} of {len(record['commits'])} commits of this change"
            for g in props["policy_bound"]["commit_gates"]]
    out += _lines(props["policy_bound"]["gate_log"], _gate_line)
    out += ["", "## Verified", ""]
    checks = props["verified"]["checks"]
    out.append(f"- checks on head `{str(checks.get('head_sha') or '')[:7]}`: {checks['outcome']} "
               f"({len(checks.get('runs') or [])} check runs recorded)" if checks else f"- {NOT_COLLECTED}")
    out += ["", "## Approved", ""]
    out += [f"- review state `{s['verification']}` on {s['commits']} of {len(record['commits'])} commits"
            for s in props["approved"]["review_states"]]
    out += _lines(props["approved"]["review_passes"], lambda ln: (
        f"{ln['ts']} {ln['change']} {ln['review']} review: {ln['passes']} passes, "
        f"{ln['blockers']} blockers, {ln['out_of_scope']} out of scope"))
    exc = props["exception_controlled"]
    out += ["", "## Exception-controlled", "", "Bypasses:"]
    out += _lines(exc["bypasses"], _gate_line)
    out += ["Shadow denies:"] + _lines(exc["shadow_denies"], _gate_line)
    out += ["Commit-creating verbs no gate judged:"] + _lines(
        exc["ungated_verbs"], lambda ln: f"{ln['ts']} `{ln['gate']}` {ln['verb']} at {str(ln['head'])[:7]}")
    text = "\n".join(out) + "\n"
    assert_clean(text, context="change record")
    return text
