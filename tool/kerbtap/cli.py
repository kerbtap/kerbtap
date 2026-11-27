"""Command-line entry point for the kerbtap Evidence Pack generator.

Usage:
    python -m kerbtap generate <repo_path> [-o <output_dir>] [-v]
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from kerbtap import __version__
from kerbtap.assemble import build_bundle
from kerbtap.build import PackWriteError, generate_pack
from kerbtap.collect.git import GitError
from kerbtap.honesty import HonestyViolation
from kerbtap.change_record import ChangeRecordError, build_record, render_markdown
from kerbtap.claude_md import DEFAULT_MODEL, DEFAULT_TIMEOUT, MergeError, diff, merge_claude_md
from kerbtap.doctor import run_doctor
from kerbtap.doc_pass import DocPassError, record as doc_pass_record
from kerbtap.git_hook import GitHookError, install as git_hook_install, remove as git_hook_remove, status as git_hook_status
from kerbtap.init import InitError, default_source_repo, init, render_claude_md_block
from kerbtap.learn import LedgerError, capture, load_ledger, report
from kerbtap.machine import DEFAULT_MARKETPLACE, PLUGIN, MachineInstallError, default_marketplace, install_machine
from kerbtap.review.acceptance import accept
from kerbtap.review.package import build_package
from kerbtap.review.record import ReviewError, record, verify
from kerbtap.review.trailer import VALID_VERDICTS

logger = logging.getLogger("kerbtap")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kerbtap",
        description="Generate a six-component AI SDLC Evidence Pack (7A) from a git repository.",
    )
    parser.add_argument("--version", action="version", version=f"kerbtap {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    ini = sub.add_parser(
        "init",
        help="Install the kerbtap scaffold into a repository. One command, no TODO markers.",
    )
    ini.add_argument(
        "repo_path",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Repository to install into (default: current directory).",
    )
    ini.add_argument(
        "--prefix",
        help="Bypass env-var prefix (default: derived from the repo directory name).",
    )
    ini.add_argument(
        "--source",
        type=Path,
        default=None,
        help="kerbtap repository to install the scaffold from (default: the one this CLI runs from).",
    )
    ini.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the files in an existing .claude/ that differ from kerbtap's copies. "
             "Without it, init merges and stops, writing nothing, if any file differs.",
    )
    ini.add_argument("--no-tests", action="store_true", help="Skip running the installed hook tests.")
    ini.add_argument(
        "--with-code-review-graph",
        action="store_true",
        help=(
            "Also install .mcp.json, which configures the code-review-graph MCP server. "
            "Requires uvx and downloads a third-party package on first use. Omitted by default."
        ),
    )
    ini.add_argument(
        "--review-code",
        action="store_true",
        help="Put src/, tests/ and .py on the review gate. Omitted by default.",
    )
    ini.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")

    ghk = sub.add_parser(
        "git-hook",
        help="Install, remove or report the git pre-commit hook that runs the commit gates.",
    )
    ghk.add_argument("action", choices=("install", "remove", "status"))
    ghk.add_argument(
        "repo_path",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Repository (default: current directory).",
    )
    ghk.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")

    doc = sub.add_parser(
        "doctor",
        help="Run each project gate on a violating and a harmless probe and report whether it fires.",
    )
    doc.add_argument(
        "repo_path",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Repository (default: current directory).",
    )
    doc.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")

    mac = sub.add_parser(
        "install-machine",
        help="Move this machine to the kerbtap plugin: install it, remove the old hook wiring from "
             "~/.claude/settings.json and move the old copied files aside. Run it again to check: it then "
             "changes nothing.",
    )
    mac.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Home directory whose .claude/ is cut over (default: the current user's).",
    )
    mac.add_argument(
        "--marketplace",
        default=default_marketplace(),
        help=f"Marketplace to install the plugin from (default: the one bundled in the wheel, else {DEFAULT_MARKETPLACE}).",
    )
    mac.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and change nothing; no claude command runs.",
    )
    for removed in ("--force", "--source"):
        mac.add_argument(removed, nargs="?", const=True, default=None, help=argparse.SUPPRESS)
    mac.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")

    mrg = sub.add_parser(
        "merge-claude-md",
        help="Merge the gate block into a repository's own CLAUDE.md, through the model.",
        description="Ask the local Claude Code CLI to merge kerbtap's gate block into this "
                    "repository's CLAUDE.md, then verify the result before writing: every "
                    "heading and paragraph of the original must survive, and the block must be "
                    "byte-equal to what init renders. A proposal failing that is kept under "
                    "<state folder>/pending/ and the original is left alone.",
    )
    mrg.add_argument(
        "repo_path",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Repository whose CLAUDE.md to merge into (default: current directory).",
    )
    mrg.add_argument(
        "--source",
        type=Path,
        default=None,
        help="kerbtap repository to render the block from (default: the one this CLI runs from).",
    )
    mrg.add_argument("--model", default=DEFAULT_MODEL, help=f"Model for the merge (default: {DEFAULT_MODEL}).")
    mrg.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Seconds to wait for the merge (default: {DEFAULT_TIMEOUT}).",
    )
    mrg.add_argument(
        "--no-apply",
        action="store_true",
        help="Verify and write the proposal to <state folder>/pending/ without touching CLAUDE.md.",
    )
    mrg.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")

    gen = sub.add_parser("generate", help="Generate an Evidence Pack from a repository.")
    gen.add_argument("repo_path", type=Path, help="Path to the git repository to assess.")
    gen.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Output directory (default: <repo>/evidence-pack/).",
    )
    gen.add_argument(
        "--internal",
        action="store_true",
        help="Include PII (absolute repo path + author emails) for internal dogfood use. "
             "Default output is external-safe (redacted).",
    )
    gen.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")

    lrn = sub.add_parser(
        "learn",
        help="Capture gate-log and revert incidents and report the M3 disposition rates.",
    )
    lrn.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="Path to the incident ledger (default: <repo root>/incidents/ledger.json).",
    )
    lrn.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")

    rev = sub.add_parser(
        "review",
        help="Record or verify an evidence-bound review trailer for a change.",
    )
    rev_sub = rev.add_subparsers(dest="review_command", required=True)

    rec = rev_sub.add_parser(
        "record",
        help="Store a reviewer's verdict as a content-addressed artifact and print the "
             "commit trailer that binds a change to it.",
    )
    source = rec.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--review-output",
        type=Path,
        help="Path to the reviewer's structured verdict JSON. It must carry a `subject` list of "
             "the repo-relative paths reviewed; each is bound to the content staged now, so "
             "stage exactly what was reviewed before recording.",
    )
    source.add_argument(
        "--from-message",
        type=Path,
        help="Path to the reviewer's saved final message; the last fenced json block (or a "
             "message that is only a JSON object) is the verdict. Reviewers do not write files.",
    )
    rec.add_argument("--verdict", choices=VALID_VERDICTS, required=True, help="Review verdict.")
    rec.add_argument(
        "--reviewer",
        default="architect",
        help="Reviewer label recorded in the trailer (default: architect).",
    )
    rec.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Repository root under which <state folder>/reviews/ lives (default: cwd).",
    )
    rec.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")

    pkg = rev_sub.add_parser(
        "package",
        help="Pin the change a reviewer is handed: write the exact diff and a manifest "
             "(base, head, changed paths, hunk ranges) under <state folder>/review-packages/.",
    )
    pkg.add_argument(
        "--staged",
        action="store_true",
        help="Package the index against HEAD (what the next commit would land). Default: "
             "merge-base(--base, HEAD)..HEAD.",
    )
    pkg.add_argument("--base", default="main", help="Base ref for a committed package (default: main).")
    pkg.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Repository to package (default: cwd).",
    )
    pkg.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: <repo>/<state folder>/review-packages/).",
    )
    pkg.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")

    acc = rev_sub.add_parser(
        "accept",
        help="Record an accepted review residual with an end date (default 30 days, at most 60), "
             "or renew one with --renew <id>.",
    )
    acc.add_argument("--id", help="Acceptance id (new entry).")
    acc.add_argument("--subject", help="Repo-relative path the acceptance covers (new entry).")
    acc.add_argument("--statement", help="Why the residual is accepted (new entry).")
    acc.add_argument("--expired-at", help="End date, YYYY-MM-DD (default: 30 days from today, UTC).")
    acc.add_argument("--renew", metavar="ID", help="Renew this acceptance: renewals + 1, new end date.")
    acc.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Repository root holding <state folder>/acceptances.json (default: cwd).",
    )
    acc.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")

    ver = rev_sub.add_parser(
        "verify",
        help="Check a commit's review trailer against its stored artifact "
             "(exit 0 = the record is integrity-checked and covers the commit).",
    )
    ver.add_argument("commit", help="Commit-ish to verify (e.g. HEAD, a SHA, a ref).")
    ver.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Repository root to read the commit and artifact from (default: cwd).",
    )
    ver.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")

    dpass = sub.add_parser(
        "doc-pass",
        help="Record a finished `/verify-claims doc-pass` against the content of each doc.",
    )
    dpass_sub = dpass.add_subparsers(dest="doc_pass_command", required=True)
    dprec = dpass_sub.add_parser(
        "record",
        help="Store the blob hash of each doc, so the doc gate clears a commit that lands "
             "exactly that content.",
    )
    dprec.add_argument("paths", nargs="+", type=Path, help="Docs the pass covered.")
    dprec.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Repository the paths are relative to (default: cwd).",
    )
    dprec.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")

    chg = sub.add_parser(
        "change-record",
        help="Print the Agentic Change Record for one PR or branch: the five properties, with the local trail.",
    )
    chg.add_argument("target", help="A PR number (needs gh) or a branch name.")
    chg.add_argument("--json", action="store_true", help="Print the record as JSON.")
    chg.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Repository to read (default: cwd).",
    )
    chg.add_argument("-v", "--verbose", action="store_true", help="Verbose (DEBUG) logging.")
    return parser


def _cmd_install_machine(args: argparse.Namespace) -> int:
    for removed in ("force", "source"):
        if getattr(args, removed) is not None:
            logger.error(
                "--%s is gone: install-machine no longer copies files. It installs the kerbtap plugin, removes the "
                "old hook wiring and moves the old copied files aside. Run it without --%s.", removed, removed,
            )
            return 2
    home = (args.home or Path.home()).expanduser()
    try:
        result = install_machine(home, marketplace=args.marketplace, dry_run=args.dry_run)
    except MachineInstallError as exc:
        logger.error("Install failed: %s", exc)
        return 2
    lead = "Plan (nothing was changed)" if result.dry_run else "Cut over"
    print(f"{lead}: the kerbtap machine tier in {home / '.claude'} runs from the kerbtap plugin")
    for command in result.commands:
        print(f"  {'would run' if result.dry_run else 'ran'}  {command}")
    for done in result.already_installed:
        print(f"  already there  {done}")
    verb = "would remove" if result.dry_run else "removed"
    print(f"  settings.json   {verb + ' the old hook wiring' if result.wiring_removed else 'no old hook wiring'}"
          + (f"; previous file kept at {result.settings_backup}" if result.settings_backup else ""))
    if result.moved:
        where = "a governor-legacy-<UTC time> folder" if result.dry_run else str(result.legacy_dir)
        print(f"  old files       {'would move' if result.dry_run else 'moved'} {len(result.moved)} to {where}")
    else:
        print("  old files       none to move")
    if not result.dry_run:
        print("\nCheck inside Claude Code with /hooks and /plugin. In a repository, `kerbtap doctor` checks that each gate fires.")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    source = (args.source or default_source_repo()).expanduser().resolve()
    try:
        result = init(
            repo=args.repo_path,
            source_repo=source,
            prefix=args.prefix,
            force=args.force,
            run_tests=not args.no_tests,
            with_code_review_graph=args.with_code_review_graph,
            review_code=args.review_code,
        )
    except InitError as exc:
        logger.error("Install failed: %s", exc)
        if "scaffold not found" in str(exc):
            logger.error("Point --source at a kerbtap checkout that contains starter/.")
        return 2

    surfaces = [name for name, on in (("docs", result.surfaces.docs), ("code", result.surfaces.code)) if on]
    if result.surfaces.code:
        surfaces.append("code review on" if result.surfaces.review_code else "code review off (opt in with --review-code)")
    print(f"Installed the kerbtap scaffold into {result.repo}")
    print(f"  bypass prefix   {result.prefix}_REVIEWED, {result.prefix}_DOC_REVIEWED, ...")
    print(f"  surfaces        {', '.join(surfaces)}")
    print(f"  files           {len(result.installed)} installed, {len(result.overwritten)} overwritten, "
          f"{len(result.unchanged)} already current")
    if result.settings_backup:
        print(f"  settings.json   merged; previous file kept at {result.settings_backup}")
    if result.dropped:
        # Say what was left out. A silent omission reads as coverage.
        print(f"  gates dropped   {', '.join(sorted(result.dropped))} (no matching surface in this repo)")
    if not args.no_tests:
        if result.tests_failed:
            print(f"  hook tests      {len(result.tests_failed)} of {result.tests_run} FAILED: "
                  f"{', '.join(result.tests_failed)}")
            logger.error("The scaffold is installed but not proven — fix the failing gate tests.")
            return 1
        print(f"  hook tests      {result.tests_run} suites passed")
    if args.with_code_review_graph:
        print("  mcp             .mcp.json installed — code-review-graph, needs `uvx`")
    else:
        print("  mcp             .mcp.json not installed (opt in with --with-code-review-graph)")
    print(f"  git hook        {result.git_hook}")
    if result.claude_md_current:
        print("  CLAUDE.md       yours, already carrying this gate block — untouched")
    elif result.claude_md_pending:
        print(f"  CLAUDE.md       yours, untouched. Gate block parked at {result.claude_md_pending}")
        print("                  Merge it in with: kerbtap merge-claude-md")
    # When enforcement starts is not obvious, and getting it wrong strands a new repository:
    # the review gate works from the very first commit because it reads the staged diff.
    print(
        "\nEnforcement begins on this repository's first commit: the pre-commit review guard "
        "checks staged\nfiles, including in a brand-new repository."
    )
    _init_machine_step()
    return 0


def _plugin_installed() -> bool:
    """Is ``kerbtap@kerbtap`` in ``claude plugin list``? A failed query reads as not installed."""
    try:
        proc = subprocess.run(["claude", "plugin", "list", "--json"], capture_output=True, text=True,
                              timeout=60, check=False)
        data = json.loads(proc.stdout) if proc.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return any(isinstance(p, dict) and p.get("id") == PLUGIN for p in data if isinstance(data, list))


def _init_machine_step() -> None:
    """After a good ``init``: install the machine tier when ``claude`` can, else say how to do it later."""
    if shutil.which("claude") is None:
        print("\nMachine tier: the `claude` command is not on PATH. Run `kerbtap install-machine` later.")
        return
    if _plugin_installed():
        return
    print("\nMachine tier: installing the kerbtap plugin")
    _cmd_install_machine(argparse.Namespace(home=None, marketplace=None, dry_run=False, force=None, source=None))


def _cmd_git_hook(args: argparse.Namespace) -> int:
    repo = args.repo_path.expanduser().resolve()
    try:
        if args.action == "install":
            print(f"installed {git_hook_install(repo)}")
        elif args.action == "remove":
            print(f"removed {git_hook_remove(repo)}")
        else:
            installed, hook = git_hook_status(repo)
            print(f"{'installed' if installed else 'absent'} {hook}")
            return 0 if installed else 1
    except GitHookError as exc:
        logger.error("Git hook %s failed: %s", args.action, exc)
        return 2
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    lines, code = run_doctor(args.repo_path.expanduser())
    print("\n".join(lines))
    return code


def _cmd_merge_claude_md(args: argparse.Namespace) -> int:
    repo: Path = args.repo_path.expanduser().resolve()
    source = (args.source or default_source_repo()).expanduser().resolve()
    target = repo / "CLAUDE.md"

    try:
        block = render_claude_md_block(repo, source)
    except InitError as exc:
        logger.error("Cannot render the gate block: %s", exc)
        return 2

    before = target.read_text(encoding="utf-8") if target.is_file() else ""
    try:
        result = merge_claude_md(
            repo,
            block,
            model=args.model,
            timeout=args.timeout,
            apply=not args.no_apply,
        )
    except MergeError as exc:
        logger.error("Merge failed: %s", exc)
        logger.error("CLAUDE.md is unchanged.")
        return 2

    if result.unchanged:
        print(f"{target} already carries this gate block — nothing to merge.")
        return 0

    if result.check and not result.check.ok:
        print(f"Merge refused — {target} is unchanged. The proposal is at {result.proposal_path}")
        for failure in result.check.failures:
            print(f"  - {failure}")
        return 1

    if result.applied:
        print(f"Merged the gate block into {target}")
        print(f"  backup          {result.backup}")
        print(diff(before, target.read_text(encoding="utf-8")), end="")
        return 0

    print(f"Verified, not applied (--no-apply). The proposal is at {result.proposal_path}")
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    repo_path: Path = args.repo_path.expanduser().resolve()
    if not repo_path.exists():
        logger.error("Repository path does not exist: %s", repo_path)
        return 2

    out_dir: Path = (args.output or (repo_path / "evidence-pack")).expanduser().resolve()

    try:
        logger.info("Assessing %s (mode=%s)", repo_path, "internal" if args.internal else "external-safe")
        bundle = build_bundle(repo_path, internal=args.internal)
        written = generate_pack(bundle, out_dir)
    except GitError as exc:
        logger.error("Git collection failed: %s", exc)
        return 2
    except HonestyViolation as exc:
        logger.error("HONESTY GUARD TRIPPED — pack not written: %s", exc)
        return 3
    except PackWriteError as exc:
        logger.error("Unsafe output path — pack not written: %s", exc)
        return 4

    logger.info(
        "Evidence Pack written to %s (%d commits, %d PRs, %d reviewed changes)",
        out_dir,
        bundle.scope.assessed_commits,
        bundle.scope.assessed_prs,
        bundle.review_attested_count,
    )
    for path in written:
        print(path)
    return 0


def _cmd_learn(args: argparse.Namespace) -> int:
    try:
        root = Path(subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], stderr=subprocess.DEVNULL, text=True, timeout=5,
        ).strip())
    except (subprocess.SubprocessError, OSError):
        logger.error("Cannot learn: the current directory is not in a git repository")
        return 2
    ledger_path = args.ledger.expanduser() if args.ledger else root / "incidents" / "ledger.json"
    try:
        ledger = load_ledger(ledger_path)
    except LedgerError as exc:
        logger.error("Incident ledger error: %s", exc)
        return 2
    print(report(capture(root), ledger), end="")
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    if args.review_command == "record":
        try:
            from_message = args.from_message is not None
            artifact_path, trailers = record(
                review_output=(args.from_message if from_message else args.review_output).expanduser(),
                verdict=args.verdict,
                reviewer=args.reviewer,
                repo=args.repo.expanduser().resolve(),
                from_message=from_message,
            )
        except ReviewError as exc:
            logger.error("Cannot record review: %s", exc)
            return 2
        logger.info("Review artifact: %s", artifact_path)
        # stdout is the trailer block only, so it can be piped into a commit message.
        print(trailers)
        return 0

    if args.review_command == "package":
        try:
            package = build_package(
                args.repo.expanduser().resolve(),
                base=args.base,
                staged=args.staged,
                out_dir=args.out.expanduser() if args.out else None,
            )
        except ReviewError as exc:
            logger.error("Cannot build review package: %s", exc)
            return 2
        # stdout: the two paths a reviewer is handed, diff first.
        print(package.diff_path)
        print(package.manifest_path)
        return 0

    if args.review_command == "accept":
        try:
            entry = accept(
                args.repo.expanduser().resolve(),
                id=args.id,
                subject=args.subject,
                statement=args.statement,
                expired_at=args.expired_at,
                renew=args.renew,
            )
        except ReviewError as exc:
            logger.error("Cannot accept: %s", exc)
            return 2
        print(json.dumps(entry, indent=2, sort_keys=True, ensure_ascii=False))
        return 0

    # verify
    try:
        ok, message, _signal = verify(args.repo.expanduser().resolve(), args.commit)
    except ReviewError as exc:
        logger.error("Cannot verify review: %s", exc)
        return 2
    print(message)
    return 0 if ok else 1


def _cmd_doc_pass(args: argparse.Namespace) -> int:
    try:
        lines = doc_pass_record(args.repo.expanduser().resolve(), [p.expanduser() for p in args.paths])
    except DocPassError as exc:
        logger.error("Cannot record doc-pass: %s", exc)
        return 2
    for line in lines:
        print(f"{line['path']} {line['blob']}")
    return 0


def _cmd_change_record(args: argparse.Namespace) -> int:
    try:
        record = build_record(args.repo.expanduser().resolve(), args.target)
        text = json.dumps(record, indent=2, ensure_ascii=False) + "\n" if args.json else render_markdown(record)
    except (ChangeRecordError, GitError, HonestyViolation) as exc:
        logger.error("Cannot build the change record: %s", exc)
        return 2
    print(text, end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "verbose", False))
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "git-hook":
        return _cmd_git_hook(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "install-machine":
        return _cmd_install_machine(args)
    if args.command == "merge-claude-md":
        return _cmd_merge_claude_md(args)
    if args.command == "generate":
        return _cmd_generate(args)
    if args.command == "learn":
        return _cmd_learn(args)
    if args.command == "review":
        return _cmd_review(args)
    if args.command == "doc-pass":
        return _cmd_doc_pass(args)
    if args.command == "change-record":
        return _cmd_change_record(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
