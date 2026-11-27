"""``kerbtap init`` — install the scaffold into a repository in one command.

Before this existed, installing kerbtap meant three ``cp`` invocations followed by
hand-editing roughly twenty constants across five hook files, knowing which gates to
take from ``starter/`` and which stronger variants to take from kerbtap's own
``.claude/``, porting the tests that ``starter/`` does not ship, and then discovering
that half the gates pointed at directories the target repo does not have. That is a
template you copy from, not a product you install.

This module does the whole thing:

* copies the scaffold, preferring the hardened gate variants where they exist;
* derives a bypass-env prefix from the repo name and renames every ``PROJECT_*``
  bypass across the hooks, the settings file, and ``CLAUDE.md``;
* detects which surfaces the repo actually has (docs, Python code) and points each
  gate at those, dropping gates whose surface is absent rather than leaving them
  wired at defaults — a gate pointed at nothing reads as coverage;
* creates the sources ledger the fact-claim gate reads, if docs are in scope;
* installs a git ``pre-commit`` hook that runs the commit gates for commits made outside
  Claude Code, chaining to any hook already there (:mod:`kerbtap.git_hook`);
* runs the installed hook tests so the install is proven, not asserted.

Over an existing ``.claude/``, the install merges: files the scaffold does not ship are kept,
identical ones are left alone, and ``settings.json`` keeps everything but kerbtap's own hook
entries, with the previous file saved beside it (:mod:`kerbtap.settings_merge`). A file
that differs at a scaffold path stops the install, named, before anything is written;
``--force`` overwrites those files.

Nothing here writes outside the target repository.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from kerbtap.claude_md import block_body, governor_block, merge_brief, pending_rel, write_pending
from kerbtap.git_hook import GitHookError
from kerbtap.git_hook import install as git_hook_install
from kerbtap.hook_python import hook_python_problem
from kerbtap.settings_merge import SettingsMergeError, load_settings, merge_hooks, write_settings
from kerbtap.state import state_name

logger = logging.getLogger("kerbtap.init")


class InitError(Exception):
    """The scaffold could not be installed."""


# The agent an installed repository's gates name. `starter/.claude/agents/reviewer/`
# ships the contract; kerbtap's own `.claude/` keeps naming its `architect`.
INSTALLED_REVIEWER = "reviewer"

# Hooks kerbtap no longer ships. `merge_hooks` strips the wiring of an owned hook, so a
# re-run of `init` unwires one an earlier install wrote; the file on disk is left alone.
RETIRED_HOOKS = ("review_dispatch_guard.py",)

# Replaced in the installed CLAUDE.md by the generated hook table.
HOOK_TABLE_MARKER = "<!-- GOVERNOR:HOOK-TABLE -->"

BYPASS_RENAMES = (
    "PROJECT_REVIEWED",
    "PROJECT_DOC_REVIEWED",
    "PROJECT_CLAIM_VERIFIED",
)

SOURCES_LEDGER_REL = "docs/sources-ledger.md"

LEDGER_TEMPLATE = """# Sources ledger

Every empirical figure in a `docs/**` deliverable must have a row here before it can land.
`.claude/hooks/fact_claim_guard.py` hard-blocks the edit otherwise.

A row records that someone **fetched the primary source and read the line**. It does not
record that the figure is still true today, so the verification date is load-bearing.

Rules:

- One line per figure. Table rows only — prose in this file backs nothing.
- The quote is **verbatim**, copied from the source itself and never from a tool's summary
  of it. Quote to clause boundaries; no mid-quote ellipses, because an ellipsis is how a
  qualifier gets deleted and a true sentence becomes a false claim.
- Primary sources outrank analysts, which outrank press. A second-hand attribution is not
  a source.
- A figure that cannot be verified gets a row marked `UNVERIFIED` and stays out of the
  deliverable as a claim.

| figure | claim | source (org, year) | URL or repo path | verbatim quote | status |
|---|---|---|---|---|---|
| | | | | | |
"""


@dataclass
class Surfaces:
    """What the target repo actually contains, so gates point at real paths."""

    docs: bool
    code: bool
    review_code: bool = False  # opt-in: put src/, tests/ and .py on the review gate

    @property
    def review_prefixes(self) -> tuple[str, ...]:
        prefixes: list[str] = []
        if self.docs:
            prefixes.append("docs/")
        if self.code and self.review_code:
            prefixes.extend(("src/", "tests/"))
        return tuple(prefixes)

    @property
    def review_suffixes(self) -> tuple[str, ...]:
        suffixes: list[str] = []
        if self.docs:
            suffixes.extend((".md", ".json"))
        if self.code and self.review_code:
            suffixes.append(".py")
        return tuple(suffixes)


@dataclass
class InitResult:
    repo: Path
    prefix: str
    surfaces: Surfaces
    installed: list[str] = field(default_factory=list)
    overwritten: list[str] = field(default_factory=list)  # differed, replaced under --force
    unchanged: list[str] = field(default_factory=list)  # already identical, not rewritten
    dropped: list[str] = field(default_factory=list)
    tests_run: int = 0
    tests_failed: list[str] = field(default_factory=list)
    settings_merged: bool = False  # an existing settings.json was merged, not replaced
    settings_backup: Path | None = None
    claude_md_block: str = ""  # the kerbtap-owned block this install renders
    claude_md_pending: Path | None = None  # an existing CLAUDE.md is waiting for a merge
    claude_md_current: bool = False  # an existing CLAUDE.md already carries this block
    git_hook: str = ""  # the git pre-commit hook path, or why none was installed


def derive_prefix(repo: Path) -> str:
    """Bypass-env prefix from the repo directory name.

    ``carbonLedger`` -> ``CARBONLEDGER``. Non-alphanumerics become underscores, and a
    leading digit is prefixed so the result is a legal shell variable name.
    """
    raw = re.sub(r"[^A-Za-z0-9]+", "_", repo.name).strip("_").upper()
    if not raw:
        raise InitError(f"cannot derive an env-var prefix from directory name {repo.name!r}")
    if raw[0].isdigit():
        raw = f"P_{raw}"
    return raw


def detect_surfaces(repo: Path, review_code: bool = False) -> Surfaces:
    docs = (repo / "docs").is_dir()
    code = (repo / "src").is_dir() or (repo / "tests").is_dir() or any(repo.glob("*.py"))
    if not docs and not code:
        # A repo with neither is almost always brand new. Gate docs by default: the
        # directory is created below, and a docs surface is the one every project
        # acquires first.
        docs = True
    return Surfaces(docs=docs, code=code, review_code=review_code)


def installed_review_code(repo: Path) -> bool:
    """True when the review gate installed in *repo* covers ``.py`` (an earlier ``--review-code``)."""
    hook = repo / ".claude" / "hooks" / "precommit_review_guard.py"
    if not hook.exists():
        return False
    try:
        text = hook.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Could not read the installed review gate %s: %s", hook, exc)
        return False
    match = re.search(r"^TRACKED_SUFFIXES = \(([^)]*)\)", text, flags=re.MULTILINE)
    return bool(match) and '".py"' in match.group(1)


def _starter_root(source_repo: Path) -> Path:
    starter = source_repo / "starter"
    if not (starter / ".claude").is_dir():
        raise InitError(f"scaffold not found at {starter} — is this the kerbtap repository?")
    return starter


def _strip_todos(text: str) -> str:
    """Remove the scaffold's ``TODO: adapt for <PROJECT>`` markers.

    They are instructions to a human doing the install by hand. Once the install is
    mechanical they are stale comments, and a stale instruction is worse than none —
    it tells the next reader that work is outstanding when it is not.
    """
    # Whole HTML comment blocks (possibly multi-line) that carry a marker.
    text = re.sub(
        r"<!--(?:(?!-->).)*?TODO: adapt for(?:(?!-->).)*?-->[ \t]*\n?",
        "",
        text,
        flags=re.DOTALL,
    )
    # Single-line hash comments, including continuation lines indented under them.
    out: list[str] = []
    skipping = False
    for line in text.split("\n"):
        stripped = line.strip()
        if re.match(r"^#\s*TODO: adapt for", stripped):
            skipping = True
            continue
        if skipping:
            # Continuation of the marker comment: an indented '#' line that is not a
            # new top-level comment sentence.
            if re.match(r"^#\s+\S", stripped) and line.startswith("#   "):
                continue
            skipping = False
        # Trailing markers on an otherwise useful line.
        line = re.sub(r"\s*#\s*TODO: adapt for.*$", "", line)
        out.append(line)
    text = "\n".join(out)
    # Docstring-level TODO sections: drop the marker line and its indented body. The
    # placeholder may already have been substituted for the repo name by this point,
    # so match on the marker phrase alone rather than on "<PROJECT>".
    text = re.sub(r"\nTODO: adapt for [^\n]*\n(?:[ \t]+.*\n|\n)*", "\n", text)
    return text


# Fixture paths inside kerbtap's own gate tests. The tests are
# table-driven against kerbtap's surfaces, so a port has to move the fixtures onto
# the surfaces the installing repo actually has, or the test asserts against paths
# the installed constants can never match.
#
# The maps are PER TEST FILE on purpose: the same path is a positive fixture in one
# suite and a negative control in another. `docs/workplan.md` must stay tracked for
# the doc-consistency gate and must fall outside the surface for the review gate. A
# single global map silently inverts one of them, which is how a negative control
# stops controlling for anything.
TEST_FIXTURE_REWRITES: dict[str, dict[str, str]] = {
    # test_precommit_review_guard.py is handled by _review_guard_fixtures(), because
    # its positives have to land on whichever surface this repo actually has.
    "test_precommit_doc_consistency_guard.py": {
        # Positives: exact tracked paths.
        "docs/workplan.md": "docs/sources-ledger.md",
        "docs/vision.md": "docs/decision-log.md",
        '"AGENTS.md"': '"CLAUDE.md"',
        # Negatives: real files that are not tracked by exact path.
        "docs/user-manual.md": "docs/notes.md",
        "tool/kerbtap/cli.py": "src/app.py",
        "starter/CLAUDE.md": "vendor/CLAUDE.md",
    },
}


# Case names, not paths: the suite's own labels described this project's deliverables, so
# an installed copy read as though it were testing someone else's business plan.
REVIEW_GUARD_LABEL_REWRITES = {
    "md deliverable": "guarded .md file",
    "json deliverable": "guarded .json file",
    "md outside business-plan": "md outside the guarded surface",
    "json outside business-plan": "json outside the guarded surface",
}


def _review_guard_fixtures(surfaces: Surfaces) -> dict[str, str]:
    """Fixtures for the review-guard suite, on whichever surface this repo has.

    The suite needs two positives that the installed constants match and two
    negatives they cannot. A docs-only repo has no ``.py`` on the reviewed surface
    and a code-only repo has no ``.md``, so a fixed pair works for neither.
    """
    if surfaces.docs:
        positives = ("docs/market-sizing.md", "docs/model/assumptions.json")
    else:
        positives = ("src/app.py", "tests/test_app.py")
    return {
        "docs/deliverables/market-sizing.md": positives[0],
        "docs/deliverables/model/assumptions.json": positives[1],
        # Negatives live outside every tracked prefix in both layouts.
        "docs/workplan.md": "README.md",
        "docs/decision-log.json": "notes/scratch.json",
    }


def _rewrite_test_fixtures(filename: str, text: str, surfaces: Surfaces) -> str:
    if filename == "test_precommit_review_guard.py":
        mapping = {**_review_guard_fixtures(surfaces), **REVIEW_GUARD_LABEL_REWRITES}
    else:
        mapping = TEST_FIXTURE_REWRITES.get(filename, {})
    for old, new in mapping.items():
        text = text.replace(old, new)
    return text


def _rename_bypasses(text: str, prefix: str) -> str:
    for name in BYPASS_RENAMES:
        text = text.replace(name, f"{prefix}_{name.split('_', 1)[1]}")
    return text


def _set_tuple_constant(text: str, name: str, values: tuple[str, ...]) -> str:
    """Rewrite ``NAME = (...)`` to the given values, preserving a trailing comma style."""
    rendered = ", ".join(f'"{v}"' for v in values)
    if len(values) == 1:
        rendered += ","
    return re.sub(
        rf"^{name} = \([^)]*\)",
        f"{name} = ({rendered})",
        text,
        count=1,
        flags=re.MULTILINE,
    )


def _set_reviewer_agent(text: str) -> str:
    """Point an installed gate at the generic ``reviewer`` contract.

    kerbtap's own copies name its ``architect`` agent. Installing that name into another
    repository sends the operator to a contract that repository does not have — the gate
    would refuse a commit and then name a reviewer nobody can run. A constant, rewritten
    here like the surface tuples, rather than prose surgery on the refusal text.
    """
    return re.sub(
        r'^REVIEWER_AGENT = "[^"]*"',
        f'REVIEWER_AGENT = "{INSTALLED_REVIEWER}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )


def _apply_surfaces(filename: str, text: str, surfaces: Surfaces, repo_name: str) -> str:
    if filename == "precommit_review_guard.py":
        text = _set_tuple_constant(text, "TRACKED_PATH_PREFIXES", surfaces.review_prefixes)
        text = _set_tuple_constant(text, "TRACKED_SUFFIXES", surfaces.review_suffixes)
        text = _set_reviewer_agent(text)
    elif filename == "precommit_doc_consistency_guard.py":
        tracked = '        "CLAUDE.md",\n        "docs/decision-log.md",\n        "docs/sources-ledger.md",\n'
        text = re.sub(
            r"TRACKED_PATHS = frozenset\(\n    \{\n.*?\n    \}\n\)",
            "TRACKED_PATHS = frozenset(\n    {\n" + tracked + "    }\n)",
            text,
            count=1,
            flags=re.DOTALL,
        )
    return text.replace("<PROJECT>", repo_name)


def _gate_is_wanted(filename: str, surfaces: Surfaces) -> bool:
    """Drop gates whose surface the repo does not have.

    Leaving a gate wired at defaults it can never match is worse than not installing
    it: the settings file then advertises coverage that does not exist.
    """
    if filename.startswith("fact_claim_guard") or filename.startswith("test_fact_claim_guard"):
        return surfaces.docs
    if filename in ("precommit_review_guard.py", "test_precommit_review_guard.py"):
        return bool(surfaces.review_prefixes)
    return True



# One source for both the settings wiring and the CLAUDE.md hook table. A hand-written
# table drifts from the gates actually installed — observed in a pilot install that
# documented a gate `init` had dropped, understated a matcher, and called a deny-mode gate
# a soft block. `_hook_table` renders only what `_settings_json` wires.
GATE_DOCS: dict[str, str] = {
    "precommit_review_guard.py": (
        "**Hard-blocks** (deny mode) a `git commit` touching the guarded surface "
        "(`TRACKED_PATH_PREFIXES` / `TRACKED_SUFFIXES`) without a review. Bypass: "
        "`{prefix}_REVIEWED=1 {prefix}_REVIEWED_REASON=<why>` as an env-assignment prefix "
        "(optional `{prefix}_REVIEWED_UNTIL=YYYY-MM-DDTHH:MM:SSZ`; a bypass with no reason, a past "
        "expiry or an unwritable gate log is refused). Each deny and bypass is one line in "
        "`{state}/gate-log/events.jsonl`. Enforces from the first commit. "
        "Commits made by `rebase --continue`, `cherry-pick`, `revert`, `merge` or `am` bypass it; "
        "each such command is recorded to `{state}/telemetry/ungated-commit-verbs.jsonl`, never blocked."
    ),
    "precommit_doc_consistency_guard.py": (
        "**Warns** (always shadow, never blocks) on a `git commit` touching a canonical living doc listed "
        "in `TRACKED_PATHS` without a stale-ref / count-drift pass. Each warning is one `shadow-deny` "
        "line in `{state}/gate-log/events.jsonl`; `kerbtap doc-pass record <doc>...` turns it into a `clear`."
    ),
    "fact_claim_guard.py": (
        "**Hard-blocks** (deny mode) an edit adding an empirical statistic to a fact-facing doc "
        "with no verified row in the sources ledger. Bypass: `{prefix}_CLAIM_VERIFIED=1` with `{prefix}_CLAIM_VERIFIED_REASON=<why>` "
        "(same refusals and gate log)."
    ),
    "mangled_text_guard.py": (
        "Warns on doubled words/tokens and project-name typos after a write. Non-blocking."
    ),
}


def _hook_table(prefix: str, gates: set[str], matchers: dict[str, str], state: str) -> str:
    """Render the hook table for exactly the gates installed, with their real matchers."""
    rows = [
        "| Hook | Event | Behaviour |",
        "|------|-------|-----------|",
    ]
    for name in sorted(gates | {"mangled_text_guard.py"}):
        doc = GATE_DOCS.get(name)
        if doc is None:
            continue
        rows.append(f"| `{name}` | {matchers[name]} | {doc.format(prefix=prefix, state=state)} |")
    return "\n".join(rows)


def _hook_matchers(gates: set[str]) -> dict[str, str]:
    """The event/matcher label per gate, mirroring exactly what `_settings_json` wires."""
    matchers = {
        "precommit_review_guard.py": "PreToolUse / `Bash`",
        "precommit_doc_consistency_guard.py": "PreToolUse / `Bash`",
        "fact_claim_guard.py": "PreToolUse / `Edit|Write|MultiEdit`",
        "mangled_text_guard.py": "PostToolUse / `Edit|Write`",
    }
    return {k: v for k, v in matchers.items() if k in gates or k == "mangled_text_guard.py"}


def _settings_json(prefix: str, gates: set[str]) -> str:
    bash_hooks = [
        g
        for g in ("precommit_review_guard.py", "precommit_doc_consistency_guard.py")
        if g in gates
    ]
    blocks: list[str] = []
    if bash_hooks:
        entries = ",\n".join(_hook_entry(g, 10) for g in bash_hooks)
        blocks.append('      {\n        "matcher": "Bash",\n        "hooks": [\n' + entries + "\n        ]\n      }")
    if "fact_claim_guard.py" in gates:
        blocks.append(
            '      {\n        "matcher": "Edit|Write|MultiEdit",\n        "hooks": [\n'
            + _hook_entry("fact_claim_guard.py", 10)
            + "\n        ]\n      }"
        )
    pre = ",\n".join(blocks)
    post = (
        '      {\n        "matcher": "Edit|Write",\n        "hooks": [\n'
        + _hook_entry("mangled_text_guard.py", 5)
        + "\n        ]\n      }"
    )
    comment = (
        f"Per-repo hook wiring installed by `kerbtap init`. Generic safety hooks live in "
        f"~/.claude/hooks and apply to every project automatically — do not re-wire them here. "
        f"Bypass env vars: {prefix}_REVIEWED, {prefix}_DOC_REVIEWED, "
        f"{prefix}_CLAIM_VERIFIED. permissions.deny is intentionally empty."
    )
    return (
        "{\n"
        f'  "_comment": "{comment}",\n'
        '  "permissions": {\n    "deny": []\n  },\n'
        '  "hooks": {\n'
        '    "PreToolUse": [\n' + pre + "\n    ],\n"
        '    "PostToolUse": [\n' + post + "\n    ]\n"
        "  }\n"
        "}\n"
    )


def hook_command(filename: str) -> str:
    """The exact command kerbtap wires for a project hook — the ownership key for merges."""
    return f'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/{filename}"'


def _hook_entry(filename: str, timeout: int) -> str:
    return (
        "          {\n"
        '            "type": "command",\n'
        f"            \"command\": {json.dumps(hook_command(filename))},\n"
        f'            "timeout": {timeout}\n'
        "          }"
    )


def project_hook_files(source_repo: Path) -> set[str]:
    """Every non-test hook file the project tier can install, dropped-by-surface ones included."""
    starter = _starter_root(source_repo.expanduser().resolve())
    return {p.name for p in (starter / ".claude/hooks").glob("*.py") if not p.name.startswith("test_")}


def artifact_root(package_dir: Path) -> Path | None:
    """The scaffold bundled inside an installed wheel, or ``None`` in a source checkout.

    ``tool/setup.py`` copies ``starter/``, ``global/``, ``research/`` and the marketplace into
    ``kerbtap/_artifact/`` at build time, so a wheel carries everything ``init`` and
    ``install-machine`` read. An editable install has no ``_artifact/`` and falls back
    to the checkout.
    """
    bundled = package_dir / "_artifact"
    return bundled if (bundled / "starter" / ".claude").is_dir() else None


def default_source_repo() -> Path:
    """Where the scaffold comes from when ``--source`` is not given.

    The wheel's own bundled copy when there is one; otherwise the kerbtap checkout
    this package was installed from — ``tool/kerbtap/init.py`` -> repository root two
    levels up, which is what an editable install has.
    """
    return artifact_root(Path(__file__).resolve().parent) or Path(__file__).resolve().parents[2]


@dataclass
class RenderedHooks:
    """The hook files ``init`` would write for one repository, before anything is written."""

    files: dict[str, str] = field(default_factory=dict)  # filename -> rendered text
    dropped: list[str] = field(default_factory=list)
    gates: set[str] = field(default_factory=set)  # non-test files


def render_hooks(source_repo: Path, repo_name: str, prefix: str, surfaces: Surfaces) -> RenderedHooks:
    """Render every hook file for a repository, exactly as ``init`` installs them.

    The one rendering path: ``init`` writes its result, and the Evidence Pack compares an
    installed layer against it. Pure apart from reading the scaffold.
    """
    starter = _starter_root(source_repo.expanduser().resolve())
    rendered = RenderedHooks()
    for src in sorted((starter / ".claude/hooks").glob("*.py")):
        if not _gate_is_wanted(src.name, surfaces):
            rendered.dropped.append(src.name)
            continue
        text = src.read_text(encoding="utf-8")
        text = _rename_bypasses(text, prefix)
        text = _apply_surfaces(src.name, text, surfaces, repo_name)
        text = _strip_todos(text)
        if src.name.startswith("test_"):
            text = _rewrite_test_fixtures(src.name, text, surfaces)
        rendered.files[src.name] = text
        if not src.name.startswith("test_"):
            rendered.gates.add(src.name)
    return rendered


def hook_entry_key(event: str, matcher: str, hook: dict) -> tuple[str, str, str]:
    """A comparable identity for one wired hook: event, matcher and the full hook object."""
    return event, matcher, json.dumps(hook, sort_keys=True)


def expected_wiring(prefix: str, gates: set[str]) -> dict[str, Counter]:
    """``{gate: Counter(hook_entry_key)}`` exactly as the installed settings wire each gate."""
    settings = json.loads(_settings_json(prefix, gates))
    wiring: dict[str, Counter] = {}
    for event, entries in settings["hooks"].items():
        for entry in entries:
            for hook in entry["hooks"]:
                name = hook["command"].rsplit("/", 1)[-1].rstrip('"')
                wiring.setdefault(name, Counter())[hook_entry_key(event, entry["matcher"], hook)] += 1
    return wiring


def _in_git_work_tree(repo: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _run_hook_tests(repo: Path) -> tuple[int, list[str]]:
    tests = sorted((repo / ".claude/hooks").glob("test_*.py"))
    failed: list[str] = []
    for test in tests:
        proc = subprocess.run(
            [sys.executable, str(test)], cwd=repo, capture_output=True, text=True, timeout=120
        )
        if proc.returncode != 0:
            failed.append(test.name)
    return len(tests), failed


def render_claude_md_block(repo: Path, source_repo: Path, prefix: str | None = None) -> str:
    """The kerbtap block for *repo*, rendered from the gates an install there would wire.

    `merge-claude-md` runs after `init`, so it renders the block the same way rather than
    reading it back out of a file that a merge may have moved.
    """
    repo = repo.expanduser().resolve()
    resolved_prefix = prefix or derive_prefix(repo)
    surfaces = detect_surfaces(repo, review_code=installed_review_code(repo))
    gates = render_hooks(source_repo, repo.name, resolved_prefix, surfaces).gates
    return governor_block(resolved_prefix, _hook_table(resolved_prefix, gates, _hook_matchers(gates), state_name(repo)))


def _check_install_path(repo: Path, rel: str) -> None:
    """Refuse, before any write, a destination the install cannot or must not write (INS-001).

    A symlink anywhere from `<repo>/openspec` down could point outside the repository; a
    directory where a file belongs, or a file where a directory belongs, cannot be written.
    `--force` clears none of these, so each stops the install, named.
    """
    parts = Path(rel).parts
    current = repo
    for i, part in enumerate(parts):
        current = current / part
        last = i == len(parts) - 1
        if current.is_symlink():
            raise InitError(f"{current} is a symbolic link; nothing was written. Remove it and re-run.")
        if last and current.is_dir():
            raise InitError(f"{current} is a directory where kerbtap installs a file; nothing was written.")
        if not last and current.exists() and not current.is_dir():
            raise InitError(f"{current} is a file where kerbtap needs a directory; nothing was written.")


def _ignore_lines(repo: Path) -> list[str]:
    """The `.gitignore` block IG-001 adds for the resolved state folder."""
    state = state_name(repo)
    return ["# kerbtap: transient state, not committed", f"{state}/review-packages/", f"{state}/telemetry/"]


def _write_gitignore(repo: Path) -> list[str]:
    """Append the missing lines of the ignore block to `<repo>/.gitignore`; return the lines added."""
    path = repo / ".gitignore"
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        present = {line.strip() for line in text.splitlines()}
        missing = [line for line in _ignore_lines(repo) if line not in present]
        if not missing:
            return []
        prefix = "" if not text or text.endswith("\n") else "\n"
        gap = "\n" if text.strip() else ""
        path.write_text(text + prefix + gap + "\n".join(missing) + "\n", encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InitError(f"cannot write {path}: {exc}") from exc
    return missing


def init(
    repo: Path,
    source_repo: Path,
    prefix: str | None = None,
    force: bool = False,
    run_tests: bool = True,
    with_code_review_graph: bool = False,
    git_hook: bool = True,
    review_code: bool = False,
) -> InitResult:
    """Install the scaffold into *repo*. Returns what was installed and what was dropped."""
    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        raise InitError(f"target is not a directory: {repo}")

    target_claude = repo / ".claude"
    problem = hook_python_problem()
    if problem:
        raise InitError(problem)

    starter = _starter_root(source_repo.expanduser().resolve())
    resolved_prefix = prefix or derive_prefix(repo)
    surfaces = detect_surfaces(repo, review_code=review_code or installed_review_code(repo))
    result = InitResult(repo=repo, prefix=resolved_prefix, surfaces=surfaces)

    # Hooks, preferring the hardened variant where one exists.
    rendered = render_hooks(source_repo, repo.name, resolved_prefix, surfaces)
    gates = rendered.gates

    # Settings first, before anything is written: an existing file is merged, and a file
    # that cannot be merged safely stops the install with the repository untouched.
    settings_path = target_claude / "settings.json"
    settings_text = _settings_json(resolved_prefix, gates)
    merged_settings = None
    _check_install_path(repo, ".claude/settings.json")
    if settings_path.exists():
        owned = project_hook_files(source_repo)
        try:
            merged_settings = merge_hooks(
                load_settings(settings_path),
                json.loads(settings_text),
                owned_commands={hook_command(name) for name in owned | set(RETIRED_HOOKS)},
                owned_files=owned,
            )
        except SettingsMergeError as exc:
            raise InitError(f"cannot merge {settings_path}: {exc}") from exc

    # Every scaffold file, rendered in memory first: paths relative to `.claude/`.
    planned: dict[str, bytes] = {
        f"hooks/{name}": text.encode("utf-8") for name, text in rendered.files.items()
    }
    result.dropped.extend(rendered.dropped)

    # Everything else in the scaffold, copied as-is apart from placeholder renaming.
    for sub in ("rules", "agents", "skills", "git-hooks"):
        src_dir = starter / ".claude" / sub
        if not src_dir.is_dir():
            continue
        for src in sorted(p for p in src_dir.rglob("*") if p.is_file()):
            if "__pycache__" in src.parts or src.suffix == ".pyc":
                continue
            rel = src.relative_to(starter / ".claude").as_posix()
            try:
                text = src.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                planned[rel] = src.read_bytes()
                continue
            text = _strip_todos(_rename_bypasses(text, resolved_prefix))
            planned[rel] = text.replace("<PROJECT>", repo.name).encode("utf-8")

    # `starter/openspec/` lands at `<repo>/openspec/`, byte for byte (INS-001): the planner's
    # change layout and the review spec that `review-contract.md` cites. Keys are paths from
    # the repository root, which is also the name an error gives.
    planned_root: dict[str, bytes] = {}
    openspec_src = starter / "openspec"
    if openspec_src.is_dir():
        for src in sorted(p for p in openspec_src.rglob("*") if p.is_file()):
            if "__pycache__" in src.parts or src.suffix == ".pyc":
                continue
            planned_root[src.relative_to(starter).as_posix()] = src.read_bytes()

    # An existing `.claude/` is merged into, not refused: a file kerbtap does not ship is
    # never touched, and an identical one is left as it is. A file that differs at a path
    # kerbtap ships may be the user's own work, so it stops the install, named, with
    # nothing written — `--force` overwrites exactly the files that refusal names.
    differing = []
    for rel, data in planned.items():
        dest = target_claude / rel
        _check_install_path(repo, f".claude/{rel}")
        if dest.is_file():
            if dest.read_bytes() == data:
                result.unchanged.append(rel)
            else:
                differing.append(rel)
    differing_root = []
    for rel, data in planned_root.items():
        dest = repo / rel
        _check_install_path(repo, rel)
        if dest.is_file():
            if dest.read_bytes() == data:
                result.unchanged.append(rel)
            else:
                differing_root.append(rel)
    # `.mcp.json` configures a third-party MCP server that runs an external executable
    # (`uvx code-review-graph serve`). Installing that by default puts a dependency into
    # someone's repository that they never asked for, and `starter/README.md` — where it
    # is documented — is not copied into the target, so it is no notice at all. Opt-in.
    copied_files = ["CLAUDE.md"]
    if with_code_review_graph:
        copied_files.append(".mcp.json")

    # A repository's CLAUDE.md states what that repository is. `--force` means "overwrite
    # the scaffold", never "replace the document that explains this project", so an
    # existing one is left alone whatever the flags say: the gate block is parked as a
    # merge brief for `kerbtap merge-claude-md` instead.
    block = governor_block(resolved_prefix, _hook_table(resolved_prefix, gates, _hook_matchers(gates), state_name(repo)))

    # Check only what `init` will write; a dangling link reads as absent, so it is refused.
    for name in copied_files:
        if (starter / name).is_file() and not (repo / name).exists():
            _check_install_path(repo, name)
    claude_md = repo / "CLAUDE.md"
    if claude_md.exists() and block_body(claude_md.read_text(encoding="utf-8")) != block_body(block):
        _check_install_path(repo, pending_rel(repo))
    if surfaces.docs and not (repo / SOURCES_LEDGER_REL).exists():
        _check_install_path(repo, SOURCES_LEDGER_REL)
    _check_install_path(repo, ".gitignore")

    if (differing or differing_root) and not force:
        listing = "\n  ".join([*differing, *differing_root])
        raise InitError(
            f"these files in {target_claude} (or under {repo / 'openspec'}) differ from kerbtap's "
            f"copies; yours may be your own work, so nothing was written. Diff each one, then "
            f"re-run with --force to overwrite them:\n  {listing}"
        )

    for rel, data in planned.items():
        if rel in result.unchanged:
            continue
        dest = target_claude / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except OSError as exc:
            raise InitError(f"cannot write {dest}: {exc}") from exc
        (result.overwritten if rel in differing else result.installed).append(rel)

    for rel, data in planned_root.items():
        if rel in result.unchanged:
            continue
        dest = repo / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except OSError as exc:
            raise InitError(f"cannot write {dest}: {exc}") from exc
        (result.overwritten if rel in differing_root else result.installed).append(rel)

    if merged_settings is None:
        settings_path.write_text(settings_text, encoding="utf-8")
        result.installed.append("settings.json")
    else:
        result.settings_merged = True
        result.settings_backup = write_settings(settings_path, merged_settings)
        # No backup means the merge produced the bytes already there.
        (result.installed if result.settings_backup else result.unchanged).append("settings.json")

    result.claude_md_block = block

    for name in copied_files:
        src = starter / name
        dest = repo / name
        if not src.is_file():
            continue
        if dest.exists():
            if name == "CLAUDE.md":
                original = dest.read_text(encoding="utf-8")
                if block_body(original) == block_body(block):
                    result.claude_md_current = True
                else:
                    try:
                        result.claude_md_pending = write_pending(
                            repo, merge_brief(repo.name, original, block)
                        )
                    except OSError as exc:
                        raise InitError(f"cannot write {repo / pending_rel(repo)}: {exc}") from exc
            continue
        text = _rename_bypasses(src.read_text(encoding="utf-8"), resolved_prefix)
        if name == "CLAUDE.md":
            text = text.replace(HOOK_TABLE_MARKER, block, 1)
        try:
            dest.write_text(text.replace("<PROJECT>", repo.name), encoding="utf-8")
        except OSError as exc:
            raise InitError(f"cannot write {dest}: {exc}") from exc
        result.installed.append(name)

    if surfaces.docs:
        ledger = repo / SOURCES_LEDGER_REL
        if not ledger.exists():
            try:
                ledger.parent.mkdir(parents=True, exist_ok=True)
                ledger.write_text(LEDGER_TEMPLATE, encoding="utf-8")
            except OSError as exc:
                raise InitError(f"cannot write {ledger}: {exc}") from exc
            result.installed.append(SOURCES_LEDGER_REL)

    added = _write_gitignore(repo)
    if added:
        logger.info("Added %d line(s) to %s", len(added), repo / ".gitignore")
        result.installed.append(".gitignore")

    if not git_hook:
        result.git_hook = "skipped"
    elif not _in_git_work_tree(repo):
        result.git_hook = "skipped (not a git work tree)"
    else:
        try:
            result.git_hook = str(git_hook_install(repo))
        except GitHookError as exc:
            raise InitError(f"git hook: {exc}") from exc

    if run_tests:
        result.tests_run, result.tests_failed = _run_hook_tests(repo)

    return result
