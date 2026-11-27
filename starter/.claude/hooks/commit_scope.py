#!/usr/bin/env python3
"""Shared prospective-commit scope engine for the pre-commit policy gates.

One definition of "what would land in the next commit", used by every gate that
has an opinion about it.

**Scope is here; policy is not.** This module answers "is this a git commit",
"did the operator prefix the named bypass", and "which paths would land". It has
no opinion about which of those paths matter or what the operator must do about
them. Each gate keeps its own policy obligation, because they are different
claims: ``precommit_review_guard`` asserts a deliverable was adversarially
reviewed, ``precommit_doc_consistency_guard`` asserts a living doc was swept for
decay. Collapsing those into one gate would report a property neither had
evaluated.

Deliberately NOT widened to "every staged file". kerbtap's gates are narrow on
purpose, because a gate that fires on every commit trains the reflex of typing the
bypass without reading it.

Vendored to ``starter/.claude/hooks/``, so
``kerbtap init`` installs it alongside the gates that import it. A gate reaching
an install without this file would die on import at first commit.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Coarse fallback, used only when a command will not tokenize (unbalanced
# quotes). Detection proper is token-based — see is_git_commit.
GIT_COMMIT_RE = re.compile(r"\bgit\s+commit\b")
_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_QUOTED_PUNCT_RE = re.compile(r"""(?<![^\s();<>|&])(["'])[();<>|&]+\1(?![^\s();<>|&])""")
# Characters shlex groups into operator tokens when punctuation_chars=True.
_PUNCTUATION = set("();<>|&")
# Git's own options, which sit between `git` and the subcommand. These take a
# separate value token (`git -C /path commit`); the `--opt=value` spellings are
# a single token and need no special case.
_GIT_GLOBAL_OPTS_WITH_VALUE = frozenset(
    {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)


def run(cmd: list[str], cwd: str) -> str:
    """Run a command, returning stripped stdout or "" on any failure.

    Catches subprocess-layer faults (CalledProcessError / TimeoutExpired) AND
    OS-layer faults (FileNotFoundError / PermissionError / BrokenPipeError).
    The gates' contract is "exit silently on any error" — stderr is suppressed
    anyway, so fail-safe beats diagnostics here.
    """
    try:
        return subprocess.check_output(
            cmd, cwd=cwd, stderr=subprocess.DEVNULL, text=True, timeout=5
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def tokenize(command: str) -> list[str]:
    """Shell-tokenize, or return [] if the command will not parse.

    punctuation_chars so `;`, `&&`, `|` become their own operator tokens — plain
    shlex.split would leave them glued to adjacent words. An empty list means
    malformed quoting, which is conservative for the bypass predicate (refuses to
    bypass) and falls back to the coarse pattern for detection.
    """
    # A whole word that is quoted punctuation (`"&&"`) is an argument, not an operator: hide
    # it behind a placeholder word, then put its quoted text back, which is no separator.
    quoted: list[str] = []

    def hide(match: re.Match) -> str:
        quoted.append(match.group(0))
        return f"QUOTEDPUNCT{len(quoted) - 1}X"

    try:
        lex = shlex.shlex(
            _QUOTED_PUNCT_RE.sub(hide, command), posix=True, punctuation_chars=True
        )
        lex.whitespace_split = True
        return [
            quoted[int(t[11:-1])] if re.fullmatch(r"QUOTEDPUNCT\d+X", t) and int(t[11:-1]) < len(quoted) else t
            for t in lex
        ]
    except ValueError:
        return []


def _is_separator(token: str) -> bool:
    return bool(token) and all(c in _PUNCTUATION for c in token)


def git_subcommand(tokens: list[str], start: int) -> str | None:
    """Return the subcommand of the git invocation whose ``git`` word is at ``start``.

    Git accepts its own options *before* the subcommand — ``git -C <path> commit``,
    ``git -c k=v commit``, ``git --no-pager commit``. A substring match for
    "git commit" misses every one of those, so the gate has to skip the option
    block and read the first bare word as the subcommand.
    """
    return _subcommand_args(tokens, start)[0]


def is_git_commit(command: str) -> bool:
    """True when the command invokes ``git commit``, however it is spelled."""
    tokens = tokenize(command)
    if not tokens:
        # Unparseable (unbalanced quotes) — fall back to the coarse pattern
        # rather than letting the command through unexamined.
        return bool(GIT_COMMIT_RE.search(command))
    return any(
        token == "git" and git_subcommand(tokens, i) == "commit"
        for i, token in enumerate(tokens)
    )


# Git subcommands that create commits without going through ``git commit``, so no
# commit gate evaluates what they land. Each maps to the arguments that mean "this
# invocation will not create one". ``rebase`` counts only with ``--continue``.
_COMMIT_CREATING_VERBS: dict[str, frozenset[str]] = {
    "rebase": frozenset({"--abort", "--quit", "--skip"}),
    "cherry-pick": frozenset({"--abort", "--quit", "--skip", "--no-commit", "-n"}),
    "revert": frozenset({"--abort", "--quit", "--skip", "--no-commit", "-n"}),
    "merge": frozenset({"--abort", "--quit", "--no-commit", "--squash"}),
    "am": frozenset({"--abort", "--quit", "--skip", "--show-current-patch"}),
}


def _subcommand_args(tokens: list[str], start: int) -> tuple[str | None, list[str]]:
    """``(subcommand, its arguments up to the next shell operator)`` for the ``git`` at ``start``."""
    i = start + 1
    while i < len(tokens):
        token = tokens[i]
        if _is_separator(token):
            return None, []
        if token in _GIT_GLOBAL_OPTS_WITH_VALUE:
            i += 2  # option plus its separate value token
            continue
        if token.startswith("-"):
            i += 1
            continue
        args: list[str] = []
        for arg in tokens[i + 1:]:
            if _is_separator(arg):
                break
            args.append(arg)
        return token, args
    return None, []


def commit_creating_verb(command: str) -> str | None:
    """The first git invocation that lands commits without ``git commit``, or ``None``.

    Returns the verb as recorded (``"rebase --continue"``, ``"cherry-pick"``, ...). An
    invocation that will not create a commit (``--abort``, ``--no-commit``, a bare
    ``rebase``, ...) is not reported. A command that will not tokenize reports nothing:
    this feeds telemetry, never a decision.
    """
    tokens = tokenize(command)
    for i, token in enumerate(tokens):
        if token != "git":
            continue
        sub, args = _subcommand_args(tokens, i)
        excluded = _COMMIT_CREATING_VERBS.get(sub or "")
        if excluded is None or any(arg in excluded for arg in args):
            continue
        if sub == "rebase":
            if "--continue" in args:
                return "rebase --continue"
            continue
        return sub
    return None


def has_bypass_prefix(command: str, bypass_env: str) -> bool:
    """True only when ``<bypass_env>=1`` is an env-assignment prefix of a
    ``git ... commit`` invocation.

    The token must sit in the assignment position immediately preceding the
    ``git`` word — the same text appearing inside an argument (quoted in a commit
    message, say) is data, not attestation, and must not open the gate. A commit
    message that documents this very mechanism is a realistic case here, not a
    contrived one (audit H1).

    Each gate passes its own ``bypass_env``. The two names are deliberately not
    unified: they authorize different claims, and one token that clears both
    would let a doc-consistency sweep open the deliverable-review gate.
    """
    return bypass_prefix(command, bypass_env) is not None


STATE_OLD = ".governor"
STATE_NEW = ".kerbtap"


def state_folder(root: Path) -> str:
    """The state folder name below ``root`` (SF-001): ``.governor`` when it exists, else ``.kerbtap``.

    An install made before the rename keeps its state in ``.governor``; nothing is moved.
    """
    old = root / STATE_OLD
    return STATE_OLD if old.exists() or old.is_symlink() else STATE_NEW


_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def record_event(directory: str, event: dict) -> bool:
    """Append one gate decision to the local gate log. True when the line was written.

    The log is ``<main worktree>/<state folder>/gate-log/events.jsonl``, the main worktree
    taken from ``git rev-parse --git-common-dir``. The directory carries a ``.gitignore``
    of ``*``. The log is a local file that any local user can edit, so it records, and
    proves nothing. Never raises: a deny does not depend on the write, and a bypass is
    refused when this returns ``False``.
    """

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=directory, stderr=subprocess.DEVNULL, text=True, timeout=2
        ).strip()

    try:
        common = Path(git("rev-parse", "--git-common-dir"))
        root = (common if common.is_absolute() else Path(directory) / common).resolve().parent
        try:
            branch, head = git("rev-parse", "--abbrev-ref", "HEAD"), git("rev-parse", "HEAD")
        except (subprocess.SubprocessError, OSError, ValueError):
            branch = head = None
        line = {
            "ts": datetime.now(timezone.utc).strftime(_UTC_FORMAT),
            "gate": event.get("gate"),
            "decision": event.get("decision"),
            "reason_code": event.get("reason_code"),
            "bypass_form": event.get("bypass_form"),
            "bypass_reason": event.get("bypass_reason"),
            "expires": event.get("expires"),
            "branch": branch,
            "head": head,
        }
        log = root / state_folder(root) / "gate-log" / "events.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        ignore = log.parent / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
        return True
    except (subprocess.SubprocessError, OSError, ValueError, TypeError):
        return False


# Gates that are always shadow: the mode file cannot make them block.
ALWAYS_SHADOW = frozenset({"precommit_doc_consistency_guard"})


def gate_mode(directory: str, gate: str) -> str:
    """``"shadow"`` when ``<main worktree>/<state folder>/gate-mode.json`` maps ``gate`` to it.

    Anything else (no file, a file that does not parse, a gate not named) is ``"enforce"``.
    A gate in ``ALWAYS_SHADOW`` is ``"shadow"`` whatever the file holds. Never raises.
    """
    if gate in ALWAYS_SHADOW:
        return "shadow"
    try:
        common = Path(
            subprocess.check_output(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=directory, stderr=subprocess.DEVNULL, text=True, timeout=2,
            ).strip()
        )
        root = (common if common.is_absolute() else Path(directory) / common).resolve().parent
        modes = json.loads((root / state_folder(root) / "gate-mode.json").read_text(encoding="utf-8"))
        return "shadow" if modes.get(gate) == "shadow" else "enforce"
    except (subprocess.SubprocessError, OSError, ValueError, AttributeError):
        return "enforce"


def check_bypass(
    directory: str, gate: str, form: str, reason: str | None, until: str | None = None
) -> str | None:
    """Settle one bypass attempt: ``None`` when it clears the gate, else a refusal code.

    A bypass needs a reason, an optional ``until`` (``YYYY-MM-DDTHH:MM:SSZ``) that has not
    passed, and a log line that was written. Each refusal is logged as a deny with its
    code: ``bypass-no-reason``, ``bypass-expired`` (past or unreadable) or
    ``bypass-unrecorded`` (the write failed).
    """
    code = None
    reason = (reason or "").strip()
    if not reason:
        code = "bypass-no-reason"
    elif until is not None:
        try:
            lapsed = datetime.strptime(until, _UTC_FORMAT).replace(tzinfo=timezone.utc)
            if lapsed <= datetime.now(timezone.utc):
                code = "bypass-expired"
        except ValueError:
            code = "bypass-expired"
    event = {
        "gate": gate,
        "bypass_form": form,
        "bypass_reason": reason or None,
        "expires": until,
    }
    if code is None:
        if record_event(directory, {**event, "decision": "bypass", "reason_code": "bypass"}):
            return None
        code = "bypass-unrecorded"
    record_event(directory, {**event, "decision": "deny", "reason_code": code})
    return code


def bypass_usage(bypass_env: str, command: str = "git commit ...") -> str:
    """The full override form, for a deny message."""
    return (
        f"`{bypass_env}=1 {bypass_env}_REASON=\"<why>\" [{bypass_env}_UNTIL=YYYY-MM-DDTHH:MM:SSZ] "
        f"{command}` (a bypass with no reason, a past expiry or an unwritable gate log is refused)"
    )


def bypass_prefix(command: str, bypass_env: str) -> dict[str, str] | None:
    """The env-assignment prefix of the ``git ... commit`` that carries ``<bypass_env>=1``.

    ``None`` when the prefix is absent. Otherwise every assignment of the prefix group,
    name to value, so the caller can read ``<bypass_env>_REASON`` and ``_UNTIL``.
    """
    tokens = tokenize(command)
    bypass = f"{bypass_env}=1"
    assignments: list[str] = []
    for i, token in enumerate(tokens):
        if token == "git":
            # The prefix only covers this git invocation if it is a commit.
            if bypass in assignments and git_subcommand(tokens, i) == "commit":
                return dict(a.split("=", 1) for a in assignments)
            assignments = []
        elif _ENV_ASSIGNMENT_RE.fullmatch(token):
            assignments.append(token)
        else:
            assignments = []
    return None


def commit_verdict(
    command: str, bypass_env: str, gate: str, directory: str, reason_code: str,
    decision: str = "deny",
) -> str | None:
    """Settle a commit gate that is about to deny.

    ``None``: a valid, recorded bypass clears the gate. Otherwise the gate denies, and the
    value is the refusal code of a bypass attempt, or ``""`` when none was attempted. The
    deny is already logged.
    """
    if gate_mode(directory, gate) == "shadow":
        record_event(
            directory, {"gate": gate, "decision": "shadow-deny", "reason_code": reason_code}
        )
        return ""
    values = bypass_prefix(command, bypass_env)
    if values is None:
        record_event(
            directory,
            {"gate": gate, "decision": decision, "reason_code": reason_code},
        )
        return ""
    return check_bypass(
        directory, gate, "env-prefix",
        values.get(f"{bypass_env}_REASON"), values.get(f"{bypass_env}_UNTIL"),
    )


def cd_dir(tokens: list[str], cwd: str, end: int) -> str:
    """``cwd`` after each ``cd <dir>`` below index ``end`` that is joined by ``&&`` or ``;``."""
    path = Path(cwd)
    for i in range(min(end, len(tokens))):
        if (
            tokens[i] == "cd"
            and (i == 0 or _is_separator(tokens[i - 1]))
            and i + 2 < len(tokens)
            and not tokens[i + 1].startswith("-")
            and not _is_separator(tokens[i + 1])
            and tokens[i + 2] in ("&&", ";")
        ):
            path = path / os.path.expanduser(tokens[i + 1])
    return str(path)


def commit_repo_dir(command: str, cwd: str) -> str:
    """The directory whose repository the ``git commit`` in ``command`` commits to.

    Start from the shell's working directory (the payload's ``cwd`` — in a linked
    worktree that is the worktree, not the project root), apply a ``cd <dir>`` that
    precedes the commit and is joined by ``&&`` or ``;``, then each ``git -C <dir>`` of
    the commit invocation in order — relative paths resolve against the previous one,
    as git itself does. Anything else (subshells, ``pushd``, variables) is not
    followed. A command that will not tokenize resolves to ``cwd``.
    """
    tokens = tokenize(command)
    for i, token in enumerate(tokens):
        if token == "git" and git_subcommand(tokens, i) == "commit":
            return _git_invocation_dir(tokens, i, cwd)
    return cwd


def _git_invocation_dir(tokens: list[str], i: int, cwd: str) -> str:
    """The directory the ``git`` word at ``i`` runs in: ``cd`` walk, then each ``-C``."""
    path = Path(cd_dir(tokens, cwd, i))
    j = i + 1
    while j < len(tokens) and not _is_separator(tokens[j]):
        option = tokens[j]
        if option == "-C" and j + 1 < len(tokens):
            path = path / os.path.expanduser(tokens[j + 1])
            j += 2
        elif option in _GIT_GLOBAL_OPTS_WITH_VALUE:
            j += 2
        elif option.startswith("-"):
            j += 1
        else:
            break
    return str(path)


def succeeds(cmd: list[str], cwd: str) -> bool:
    """True when the command exits 0. Any fault is ``False``."""
    try:
        return subprocess.run(
            cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
        ).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


# ``git commit`` short options whose value may be the next token (``-m msg``) or the
# rest of the cluster (``-mmsg``, ``-qam msg``).
_COMMIT_SHORT_WITH_VALUE = frozenset("mFCct")
_COMMIT_LONG_WITH_VALUE = frozenset(
    {
        "--message", "--file", "--author", "--date", "--template", "--reuse-message",
        "--reedit-message", "--fixup", "--squash", "--trailer", "--cleanup",
    }
)
# Options that make the commit take working-tree content the index does not show.
_COMMIT_WIDENING = frozenset({"a", "i", "o", "--all", "--include", "--only", "--pathspec-from-file"})


def _commit_options(command: str) -> tuple[bool, bool]:
    """``(widened, amend)`` for the first ``git commit`` in ``command``.

    *widened* is true when the commit may land unstaged content: ``-a``, ``-i``,
    ``-o``, ``--pathspec-from-file`` or any pathspec. An option this parser does not
    know leaves its value behind as a positional token, which reads as a pathspec —
    the error widens the scope, never narrows it.
    """
    tokens = tokenize(command)
    for i, token in enumerate(tokens):
        if token != "git":
            continue
        sub, args = _subcommand_args(tokens, i)
        if sub != "commit":
            continue
        widened = amend = False
        j = 0
        while j < len(args):
            arg = args[j]
            j += 1
            if arg == "--":
                widened = widened or j < len(args)
                break
            if arg.startswith("--"):
                name = arg.split("=", 1)[0]
                amend = amend or name == "--amend"
                widened = widened or name in _COMMIT_WIDENING
                if name in _COMMIT_LONG_WITH_VALUE and "=" not in arg:
                    j += 1
                continue
            if arg.startswith("-") and len(arg) > 1:
                for k, char in enumerate(arg[1:], start=2):
                    widened = widened or char in _COMMIT_WIDENING
                    if char in _COMMIT_SHORT_WITH_VALUE:
                        if k == len(arg):
                            j += 1  # value is the next token
                        break
                continue
            widened = True  # a pathspec
        return widened, amend
    return False, False


class ScopeError(Exception):
    """A git path query of the commit scope failed, so the scope is unknown."""


def _names(cmd: list[str], cwd: str) -> list[str]:
    """Paths printed by a git path query, NUL-separated and unquoted.

    Unlike :func:`run`, a fault is not an empty answer: an unreadable scope must not
    read as an empty commit. Output is not stripped, because a path may start or end
    with a space.
    """
    full = [cmd[0], "-c", "core.quotePath=false", cmd[1], "-z", *cmd[2:]]
    shown = " ".join(cmd)
    try:
        out = subprocess.run(
            full, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ScopeError(f"`{shown}` could not run ({type(exc).__name__})") from exc
    if out.returncode != 0:
        raise ScopeError(f"`{shown}` exited {out.returncode}")
    text = out.stdout.decode("utf-8", errors="surrogateescape")
    return [path for path in text.split("\0") if path]


def _incoming_is_landed(project_dir: str, merge_head: str) -> bool:
    """True when ``MERGE_HEAD`` is already contained in the remote default branch."""
    default = run(["git", "symbolic-ref", "-q", "refs/remotes/origin/HEAD"], project_dir)
    return bool(default) and succeeds(
        ["git", "merge-base", "--is-ancestor", merge_head, default], project_dir
    )


def _merge_conflicts(project_dir: str) -> list[str]:
    """Paths git listed under ``# Conflicts:`` in ``MERGE_MSG``."""
    rel = run(["git", "rev-parse", "--git-path", "MERGE_MSG"], project_dir)
    if not rel:
        return []
    try:
        text = (Path(project_dir) / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    paths: list[str] = []
    in_block = False
    for line in text.splitlines():
        if line.strip() == "# Conflicts:":
            in_block = True
        elif in_block and line.startswith("#\t"):
            paths.append(line[2:])
        elif in_block:
            in_block = False
    return paths


def _head_changes(project_dir: str) -> list[str]:
    """Paths ``HEAD`` changed against its first parent, or all of them for a root commit."""
    if succeeds(["git", "rev-parse", "-q", "--verify", "HEAD^"], project_dir):
        return _names(["git", "diff", "--name-only", "HEAD^", "HEAD"], project_dir)
    return _names(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "HEAD"], project_dir
    )


_GIT_ADD_RE = re.compile(r"\bgit\s+(add|stage)\b")
_WHOLE_TREE = ":/"


def _add_pathspecs(args: list[str]) -> tuple[list[str], bool, bool]:
    """``(pathspecs, tracked_only, partial)`` for the arguments of one ``git add``.

    *partial*: ``-p``, ``-i`` or ``-e`` (or the long forms) may stage only part of a file.

    ``-A``, ``--all``, ``--no-ignore-removal``, ``-u`` and ``--update`` with no
    pathspec, and ``--pathspec-from-file`` always, mean the whole tree.
    """
    specs: list[str] = []
    every = update = from_file = rest = partial = False
    j = 0
    while j < len(args):
        arg = args[j]
        j += 1
        if rest or not arg.startswith("-") or arg == "-":
            specs.append(arg)
        elif arg == "--":
            rest = True
        elif arg.startswith("--"):
            name = arg.split("=", 1)[0]
            if name == "--pathspec-from-file":
                from_file = True
                if "=" not in arg:
                    j += 1  # its value is not a pathspec
            elif name in ("--all", "--no-ignore-removal"):
                every = True
            elif name == "--update":
                update = True
            elif name in ("--patch", "--interactive", "--edit"):
                partial = True
        else:
            every = every or "A" in arg
            update = update or "u" in arg
            partial = partial or any(c in arg[1:] for c in "pie")
    if from_file or ((every or update) and not specs):
        specs = [_WHOLE_TREE]
    return specs, update and not every and not from_file, partial


def _add_names(specs: list[str], tracked_only: bool, cwd: str) -> list[str]:
    paths = _names(["git", "diff", "--name-only", "--", *specs], cwd)
    if not tracked_only:
        paths += _names(
            ["git", "ls-files", "--others", "--exclude-standard", "--full-name", "--", *specs], cwd
        )
    return paths


def added_paths(command: str, cwd: str) -> list[str]:
    """Repo-relative paths that each ``git add`` or ``git stage`` before the first
    ``git commit`` of ``command`` would stage.

    The hook runs before any part of the command, so the index does not show them yet.
    Every separator counts, so the scope can only be wider than the run. Each add runs in
    its own directory (the ``cd`` and ``-C`` walk of the commit). A command that will not
    tokenize and mentions an add reads as an add of the whole tree. Not reached: an edit
    earlier in the command, other index-writing verbs, subshells, variables, aliases.
    Raises :class:`ScopeError` when a path query fails.
    """
    return _scan_adds(command, cwd)[0]


def _scan_adds(command: str, cwd: str) -> tuple[list[str], set[str]]:
    """``(added paths, the subset staged by a partial add)``; see :func:`added_paths`."""
    tokens = tokenize(command)
    if not tokens:
        if not _GIT_ADD_RE.search(command):
            return [], set()
        names = _add_names([_WHOLE_TREE], False, cwd)
        return names, set(names)  # unreadable: any add may be partial
    commit = next(
        (i for i, t in enumerate(tokens) if t == "git" and git_subcommand(tokens, i) == "commit"),
        None,
    )
    paths: list[str] = []
    partial_paths: set[str] = set()
    for i, token in enumerate(tokens[: commit or 0]):
        if token != "git":
            continue
        sub, args = _subcommand_args(tokens, i)
        if sub not in ("add", "stage"):
            continue
        specs, tracked_only, partial = _add_pathspecs(args)
        if specs:
            names = _add_names(specs, tracked_only, _git_invocation_dir(tokens, i, cwd))
            paths += names
            if partial:
                partial_paths.update(names)
    return list(dict.fromkeys(paths)), partial_paths


def staged_paths(project_dir: str, command: str, cwd: str | None = None) -> list[str]:
    """Repo-relative paths that would land in the commit ``command`` makes.

    - The staged paths. An empty index gives an empty scope, so a message-only
      amend is not judged by unrelated unstaged edits (dogfood O72).
    - Concluding a merge whose incoming side the remote default branch already
      contains: a path staged unchanged from ``MERGE_HEAD`` already landed, and is
      dropped. The paths that differ from both parents stay, and so does every path
      git listed as conflicted (dogfood O73). Any other merge keeps the full diff
      against ``HEAD``: its incoming content has no landing on record.
    - ``-a``, ``-i``, ``-o``, ``--pathspec-from-file`` or a pathspec add the unstaged
      paths, because the commit takes working-tree content.
    - ``--amend`` adds the paths ``HEAD`` changed, because the new commit carries
      them, and ``HEAD`` may come from a verb no gate judged.
    - A ``git add`` before the commit in the same command adds the paths it would
      stage (:func:`added_paths`), run from ``cwd`` (default ``project_dir``).

    Not judged: cherry-pick, revert and rebase conclusions.

    This is the single canonical scope. A gate that resolved its own would be
    free to disagree with its neighbour about what "the commit" means, which is
    how two gates end up enforcing two different definitions of the same word.
    """
    widened, amend = _commit_options(command)
    paths = _names(["git", "diff", "--cached", "--name-only"], project_dir)
    merge_head = run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], project_dir)
    if merge_head and _incoming_is_landed(project_dir, merge_head):
        new = set(_names(["git", "diff", "--cached", "--name-only", "MERGE_HEAD"], project_dir))
        paths = [p for p in paths if p in new] + _merge_conflicts(project_dir)
    if widened:
        paths += _names(["git", "diff", "--name-only"], project_dir)
    if amend:
        paths += _head_changes(project_dir)
    paths += added_paths(command, cwd or project_dir)
    return list(dict.fromkeys(paths))


def _commit_form(command: str) -> str:
    """How the first ``git commit`` in ``command`` picks the content it lands.

    ``"plain"``: no widening option, so the index lands. ``"all"``: ``-a`` or ``--all``
    with no pathspec, so the working tree lands. ``"pathspec"``: ``-i``, ``-o``,
    ``--pathspec-from-file`` or any pathspec. The walk is the one in
    :func:`_commit_options`; an unknown option's value reads as a pathspec.
    """
    tokens = tokenize(command)
    for i, token in enumerate(tokens):
        if token != "git":
            continue
        sub, args = _subcommand_args(tokens, i)
        if sub != "commit":
            continue
        every = pathspec = False
        j = 0
        while j < len(args):
            arg = args[j]
            j += 1
            if arg == "--":
                pathspec = pathspec or j < len(args)
                break
            if arg.startswith("--"):
                name = arg.split("=", 1)[0]
                every = every or name == "--all"
                pathspec = pathspec or name in {"--include", "--only", "--pathspec-from-file"}
                if name in _COMMIT_LONG_WITH_VALUE and "=" not in arg:
                    j += 1
                continue
            if arg.startswith("-") and len(arg) > 1:
                for k, char in enumerate(arg[1:], start=2):
                    every = every or char == "a"
                    pathspec = pathspec or char in "io"
                    if char in _COMMIT_SHORT_WITH_VALUE:
                        if k == len(arg):
                            j += 1
                        break
                continue
            pathspec = True
        if pathspec:
            return "pathspec"
        return "all" if every else "plain"
    return "plain"


def landing_blob(project_dir: str, command: str, path: str, cwd: str | None = None) -> str | None:
    """The blob hash of ``path`` as the commit ``command`` lands it, or ``None``.

    A plain commit lands the index blob. ``-a`` or ``--all`` with no pathspec lands the
    working-tree blob. Any pathspec form lands the index blob only when it equals the
    working-tree blob, because the commit takes the working-tree content there. ``None``
    on any git fault, a missing index entry (a conflicted or untracked path), a missing
    file or an unequal pair. A plain commit whose command adds ``path`` first lands the
    working-tree blob, because the add has not run yet when the index is read; a
    partial add (``-p``, ``-i``, ``-e``) has no known blob and gives ``None``.
    """
    index = run(["git", "rev-parse", "--verify", "-q", f":0:{path}"], project_dir)
    top = run(["git", "rev-parse", "--show-toplevel"], project_dir)
    if not top:
        return None
    form = _commit_form(command)
    if form == "plain":
        try:
            added, partial = _scan_adds(command, cwd or project_dir)
        except ScopeError:
            return None
        if path in partial:
            return None  # `git add -p` may stage part of the file; no blob is known
        if path in added:
            return run(["git", "hash-object", "--", path], top) or None
    if not index:
        return None
    if form == "plain":
        return index
    work = run(["git", "hash-object", "--", path], top)
    if not work:
        return None
    if form == "all":
        return work
    return index if index == work else None


def read_payload(stream=None) -> dict | None:
    """Parse a PreToolUse payload, or None when it is unusable.

    None means "exit silently" — malformed JSON and non-object JSON (null, list,
    scalar) are accepted rather than denied, since a gate that hard-fails on a
    payload shape it does not recognise would block every commit on a harness
    change.
    """
    try:
        data = json.load(stream if stream is not None else sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def deny(reason: str, directory: str | None = None, gate: str | None = None) -> None:
    """Emit a PreToolUse deny decision, or a warning when ``gate`` is shadowed.

    ``deny`` rather than ``ask``: an ``ask`` decision is silently auto-approved
    when ``skipAutoPermissionPrompt: true`` is set in the user's settings (true
    on this machine), so an ``ask``-mode gate is a gate that does not gate.
    """
    if directory and gate and gate_mode(directory, gate) == "shadow":
        print(json.dumps({"systemMessage": f"KERBTAP SHADOW ({gate} would deny): {reason}"}))
        return
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(output))
