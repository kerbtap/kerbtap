#!/usr/bin/env python3
"""Build a Codex review brief from `review-contract.md` beside this file, and log each review.

Nobody writes a review brief by hand: the brief is the contract template with its slots filled, and the
proposal's Source section copied into it unchanged (spec REV-001 to REV-009, change INLINE-001).

    review_brief.py brief <ID> plan|code [--pass N --dispositions FILE] [--question FILE] [--base REF] [-o OUT]
    review_brief.py log   <ID> plan|code --passes N --blockers N --out-of-scope N
    review_brief.py required plan|code --tier N      (exit 0 = required under KERBTAP_REVIEW_LEVEL)
    review_brief.py scope <ID> [--base REF] [--root DIR]   (plan files against the branch diff, SCP-001)

`brief` runs `openspec validate --strict` on the change first (pinned version, telemetry off) and refuses an
invalid change. `log` appends one JSON line to `<state folder>/telemetry/reviews.jsonl` (REV-009).

The default root is the current directory: run it from the project directory. The default log is the main
worktree's `<state folder>/telemetry/reviews.jsonl` (the current directory when git cannot say).
`scripts/review_brief.py` in the kerbtap repository runs this file with that repository as the root.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "review-contract.md"
OPENSPEC = "@fission-ai/openspec@1.16.0"
KINDS = ("plan", "code")
SLOT = re.compile(r"\{(id|files|source|base|dispositions|question)\}")


class BriefError(Exception):
    """A refusal with a message the caller can act on."""


def sections(text: str, level: str = "## ") -> dict[str, str]:
    """Map each `## name` heading to its body, up to the next heading of the same level."""
    out: dict[str, str] = {}
    name = None
    body: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith(level):
            if name is not None:
                out[name] = "".join(body)
            name, body = line[len(level):].strip(), []
        elif name is not None:
            body.append(line)
    if name is not None:
        out[name] = "".join(body)
    return out


def change_dir(root: Path, change_id: str) -> Path:
    path = root / "openspec" / "changes" / change_id
    if not (path / "proposal.md").is_file():
        raise BriefError(f"no proposal at {path / 'proposal.md'}")
    return path


def plan_files(root: Path, path: Path) -> list[str]:
    files = [path / "proposal.md"]
    files += sorted((path / "specs").glob("*/spec.md"))
    files += [p for p in (path / "design.md", path / "tasks.md") if p.is_file()]
    return [str(f.relative_to(root)) for f in files]


def source_section(path: Path) -> str:
    source = sections(read(path / "proposal.md")).get("Source", "").strip("\n")
    if not source.strip():
        raise BriefError(f"{path / 'proposal.md'} has no '## Source' section; quote the source verbatim (PLAN-002)")
    return source


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise BriefError(f"{path} is not valid UTF-8 ({exc.reason} at byte {exc.start})") from exc


def read_exact(path: Path) -> str:
    """Like `read`, but with no newline change: a CRLF file comes back with its CRLF."""
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError as exc:
        raise BriefError(f"{path} is not valid UTF-8 ({exc.reason} at byte {exc.start})") from exc


def validate(root: Path, change_id: str, timeout: int) -> None:
    if shutil.which("npx") is None:
        raise BriefError("npx not found; install Node 22.12+ to run openspec validate")
    try:
        proc = subprocess.run(
            ["npx", "-y", OPENSPEC, "validate", change_id, "--strict"],
            cwd=root, capture_output=True, text=True, check=False, timeout=timeout,
            env={**os.environ, "OPENSPEC_TELEMETRY": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        raise BriefError(f"openspec validate did not finish in {timeout}s; npx needs network access "
                         "on its first run") from exc
    if proc.returncode != 0:
        raise BriefError(f"openspec validate failed for {change_id}:\n{proc.stdout}{proc.stderr}")


def build(args: argparse.Namespace) -> str:
    root = Path(args.root).resolve()
    path = change_dir(root, args.id)
    template = sections(read(Path(args.template)))
    # Pass 1: the review's own steps, then the discovery rules. Pass 2 and later: the verification rules only.
    parts = [args.kind, f"{args.kind}-pass1", "pass1"] if args.pass_ == 1 else [args.kind, "pass2"]
    missing = [s for s in parts if s not in template]
    if args.question and "question" not in template:
        missing.append("question")
    if missing:
        raise BriefError(f"{args.template} has no section(s): {', '.join(missing)}")

    dispositions = ""
    if args.pass_ >= 2:
        if not args.dispositions:
            raise BriefError(f"pass {args.pass_} needs --dispositions: the open findings and what was done to each")
        dispositions = read(Path(args.dispositions)).strip("\n")
    question = read_exact(Path(args.question)) if args.question else ""
    if not args.no_validate:
        validate(root, args.id, args.validate_timeout)

    body = "\n\n".join(template[p].strip("\n") for p in parts)
    values = {
        "id": args.id,
        "files": "\n".join(f"- `{f}`" for f in plan_files(root, path)),
        "source": source_section(path),
        "base": args.base,
        "dispositions": dispositions,
        "question": question,
    }
    # One substitution pass: text copied in (source, dispositions, question) is never scanned for slots.
    brief = SLOT.sub(lambda m: values[m.group(1)], body).strip("\n") + "\n"
    if args.question:
        # After the final strip, so that the question text is copied in unchanged.
        section = SLOT.sub(lambda m: values[m.group(1)], template["question"].strip("\n"))
        brief += "\n## question\n\n" + section
    return brief


def default_log() -> Path:
    """The telemetry file in the main worktree, so a line from a linked worktree outlives it; the current directory when git fails."""
    here = Path.cwd()
    try:
        common = Path(subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"], cwd=here, stderr=subprocess.DEVNULL, text=True, timeout=5,
        ).strip())
        base = (common if common.is_absolute() else here / common).resolve().parent
    except (subprocess.SubprocessError, OSError):
        base = here
    old = base / ".governor"  # SF-001: an install made before the rename keeps `.governor`
    state = ".governor" if old.exists() or old.is_symlink() else ".kerbtap"
    return base / state / "telemetry" / "reviews.jsonl"


def log(args: argparse.Namespace) -> str:
    entry = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "change": args.id,
        "review": args.kind,
        "passes": args.passes,
        "blockers": args.blockers,
        "out_of_scope": args.out_of_scope,
    }
    target = Path(args.log) if args.log else default_log()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return f"logged {args.kind} review of {args.id} to {target}"


FILE_BULLET = re.compile(r"^- `([^`]+)`")


def git_out(root: Path, *argv: str) -> subprocess.CompletedProcess:
    """Run git and return its raw bytes; a path in the output need not be UTF-8."""
    try:
        return subprocess.run(["git", "-c", "core.quotePath=false", *argv], cwd=root, capture_output=True,
                              check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BriefError(f"cannot run git in {root}: {exc}") from exc


def scope(args: argparse.Namespace) -> str:
    """Compare the `## Files` section of the change's tasks.md with the whole branch diff (SCP-001)."""
    root = Path(args.root).resolve()
    tasks = root / "openspec" / "changes" / args.id / "tasks.md"
    if not tasks.is_file():
        raise BriefError(f"no tasks.md at {tasks}")
    body = sections(read(tasks)).get("Files")
    if body is None:
        raise BriefError(f"{tasks} has no '## Files' section; list each path the change touches there")
    planned = [m.group(1) for m in map(FILE_BULLET.match, body.splitlines()) if m]

    base = args.base
    if base is None:
        base = "origin/main" if git_out(root, "rev-parse", "--verify", "--quiet", "origin/main").returncode == 0 \
            else "main"
    proc = git_out(root, "diff", "--name-only", "-z", f"{base}...HEAD")
    if proc.returncode != 0:
        reason = proc.stderr.decode("utf-8", "replace").strip()
        raise BriefError(f"git diff --name-only {base}...HEAD failed in {root}: {reason}")
    changed = [c for c in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if c]

    def covers(entry: str, path: str) -> bool:
        return path.startswith(entry) if entry.endswith("/") else path == entry

    lines = [f"changed, not planned: {c}" for c in sorted(changed) if not any(covers(e, c) for e in planned)]
    lines += [f"planned, not changed: {e}" for e in sorted(set(planned)) if not any(covers(e, c) for c in changed)]
    text = "\n".join(lines) if lines else "scope: plan and diff agree"
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


LEVEL_VAR = "KERBTAP_REVIEW_LEVEL"
OLD_LEVEL_VAR = "GOVERNOR_REVIEW_LEVEL"  # read only when LEVEL_VAR is unset or empty
# Level -> the highest tier number each review covers (Tier 1 is the most critical; architectural work counts as 1).
LEVELS = {"1": {"plan": 2, "code": 2}, "2": {"plan": 1, "code": 2}, "3": {"plan": 1, "code": 1}}


def required(kind: str, tier: int) -> bool:
    """Is a Codex `kind` review required for work of `tier` under KERBTAP_REVIEW_LEVEL (default 2)? (REV-010)"""
    level = os.environ.get(LEVEL_VAR, "").strip() or os.environ.get(OLD_LEVEL_VAR, "").strip() or "2"
    if level not in LEVELS:
        raise BriefError(f"{LEVEL_VAR}={level!r} is not valid; use 1 (all Tier 2+), 2 (code review Tier 2+, "
                         "plan review Tier 1 only) or 3 (Tier 1 only)")
    return tier <= LEVELS[level][kind]


def positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {value}")
    return value


def count(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return value


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("brief", help="print the Codex brief for a change")
    b.add_argument("id")
    b.add_argument("kind", choices=KINDS)
    b.add_argument("--pass", dest="pass_", type=positive, default=1)
    b.add_argument("--dispositions", help="file listing the open findings and their dispositions (pass 2 and later)")
    b.add_argument("--question", help="file with a question for the reviewer to answer (any pass)")
    b.add_argument("--base", default="origin/main", help="diff base for a code review")
    b.add_argument("--root", default=str(Path.cwd()), help="the project directory (default: the current directory)")
    b.add_argument("--template", default=str(TEMPLATE))
    b.add_argument("--no-validate", action="store_true", help="skip openspec validate (tests only)")
    b.add_argument("--validate-timeout", type=int, default=300, help="seconds before openspec validate is abandoned")
    b.add_argument("-o", "--out", help="write the brief here instead of stdout")

    sc = sub.add_parser("scope", help="list files changed but not planned, and planned but not changed")
    sc.add_argument("id")
    sc.add_argument("--base", default=None, help="diff base (default: origin/main, else main)")
    sc.add_argument("--root", default=str(Path.cwd()), help="the project directory (default: the current directory)")

    r = sub.add_parser("required", help=f"exit 0 if the review is required under {LEVEL_VAR}, 1 if not")
    r.add_argument("kind", choices=KINDS)
    r.add_argument("--tier", type=int, required=True, choices=(1, 2, 3),
                   help="the work's tier; architectural work counts as 1")

    lg = sub.add_parser("log", help="append the REV-009 telemetry line")
    lg.add_argument("id")
    lg.add_argument("kind", choices=KINDS)
    lg.add_argument("--passes", type=positive, required=True)
    lg.add_argument("--blockers", type=count, required=True)
    lg.add_argument("--out-of-scope", type=count, required=True)
    lg.add_argument("--log", default=None, help="telemetry file (default: the main worktree's <state folder>/telemetry/reviews.jsonl)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.cmd == "log":
            print(log(args), file=sys.stderr)
            return 0
        if args.cmd == "required":
            needed = required(args.kind, args.tier)
            print(f"{args.kind} review {'required' if needed else 'not required'} for Tier {args.tier}")
            return 0 if needed else 1
        if args.cmd == "scope":
            print(scope(args))
            return 0
        brief = build(args)
        if args.out:
            Path(args.out).write_text(brief, encoding="utf-8")
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            sys.stdout.write(brief)
    except (BriefError, OSError) as exc:
        print(f"review_brief: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
