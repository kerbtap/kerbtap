"""Synthetic git-repo builder for the input-space test suite.

Every helper builds a real repository shape the generator can meet in the wild —
including hostile ones (non-UTF-8 bytes, separator control characters, unborn HEAD,
detached HEAD, shallow clones) — so coverage is exhausted by construction rather than
discovered one real repo at a time.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def git(
    repo: Path, *args: str, check: bool = True, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run a git command inside ``repo`` (test-fixture plumbing)."""
    env = {**os.environ, **env_extra} if env_extra else None
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True, env=env
    )


def init_repo(path: Path, *, name: str = "Test Dev", email: str = "dev@example.com") -> Path:
    """``git init`` an empty repository with a configured identity (unborn HEAD)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)], check=True, capture_output=True, text=True
    )
    git(path, "config", "user.name", name)
    git(path, "config", "user.email", email)
    git(path, "config", "commit.gpgsign", "false")
    return path


def _next_index(repo: Path) -> int:
    out = git(repo, "rev-list", "--count", "HEAD", check=False).stdout.strip()
    return int(out) if out.isdigit() else 0


def _date_env(date: str | None) -> dict[str, str] | None:
    if date is None:
        return None
    return {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}


def commit(
    repo: Path,
    subject: str,
    *,
    body: str = "",
    author: str | None = None,
    date: str | None = None,
) -> str:
    """Create a non-empty commit and return its SHA."""
    n = _next_index(repo)
    (repo / f"f{n}.txt").write_text(f"content {n}\n", encoding="utf-8")
    git(repo, "add", "-A")
    args = ["commit", "-m", subject]
    if body:
        args += ["-m", body]
    if author:
        args += ["--author", author]
    git(repo, *args, env_extra=_date_env(date))
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def commit_raw_message(repo: Path, raw_message: bytes) -> str:
    """Create a commit whose message is arbitrary raw bytes (e.g. invalid UTF-8)."""
    n = _next_index(repo)
    (repo / f"f{n}.txt").write_text(f"content {n}\n", encoding="utf-8")
    git(repo, "add", "-A")
    msg_file = repo / ".git" / "COMMIT_RAW_MSG"
    msg_file.write_bytes(raw_message)
    git(repo, "commit", "-F", str(msg_file))
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def merge_pr(
    repo: Path,
    pr_number: int,
    branch: str,
    subject: str,
    *,
    body: str = "",
    date: str | None = None,
) -> str:
    """Simulate a GitHub merge-commit PR landing: branch commit + --no-ff merge."""
    git(repo, "checkout", "-b", branch)
    commit(repo, subject, body=body, date=date)
    git(repo, "checkout", "main")
    git(
        repo, "merge", "--no-ff", branch,
        "-m", f"Merge pull request #{pr_number} from org/{branch}",
        env_extra=_date_env(date),
    )
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def detach_head(repo: Path) -> None:
    """Move the repo to a detached-HEAD checkout of the current tip."""
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "--detach", sha)


def shallow_clone(src: Path, dest: Path, *, depth: int = 1) -> Path:
    """Make a depth-limited (shallow) clone of ``src`` at ``dest``."""
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", str(depth), f"file://{src}", str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    git(dest, "config", "user.name", "Clone Dev")
    git(dest, "config", "user.email", "clone@example.com")
    return dest


def write_dogfood_log(repo: Path, observations: list[tuple[str, str]]) -> None:
    """Write a dogfood log with the given ``(id, headline)`` observation blocks."""
    lines = ["# Dogfood Log", ""]
    for obs_id, headline in observations:
        lines.append(f"**{obs_id} — {headline}** Detail follows.")
        lines.append("")
    path = repo / "docs" / "replication" / "dogfood-log.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# --- review-gate fixtures -----------------------------------------------------------

GATE_HOOK_REL = ".claude/hooks/precommit_review_guard.py"
GATE_SETTINGS_REL = ".claude/settings.json"


def gate_hook_source(
    prefixes: str = '("docs/plan/",)',
    suffixes: str = '(".md", ".json")',
    bypass: str = '"GOVERNOR_REVIEWED"',
) -> str:
    """A review-gate hook whose surface constants are the given Python literals."""
    return (
        "import sys\n"
        f"BYPASS_ENV = {bypass}\n"
        f"TRACKED_PATH_PREFIXES = {prefixes}\n"
        f"TRACKED_SUFFIXES = {suffixes}\n"
        "def main():\n    sys.exit(0)\n"
    )


def gate_settings_source(
    matcher: object = "Bash",
    command: str = 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/precommit_review_guard.py"',
) -> str:
    """A settings file wiring one PreToolUse hook entry."""
    return json.dumps(
        {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [{"type": "command", "command": command}]}]}}
    )


def commit_files(repo: Path, subject: str, files: dict[str, str], body: str = "") -> str:
    """Write ``files`` (repo-relative path -> text), stage everything, commit; return the SHA."""
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    args = ["commit", "-m", subject]
    if body:
        args += ["-m", body]
    git(repo, *args)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def install_review_gate(repo: Path, hook: str | None = None, settings: str | None = None) -> str:
    """Commit a wired review gate (default surface: ``docs/plan/`` .md/.json)."""
    return commit_files(
        repo,
        "install gate",
        {GATE_HOOK_REL: hook or gate_hook_source(), GATE_SETTINGS_REL: settings or gate_settings_source()},
    )
