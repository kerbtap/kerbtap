# claude-starter

A reusable per-repo Claude Code scaffold. Copy it into each new project and finish the `TODO` markers.

## Promote vs copy — what lives where

Claude Code merges two layers. The **user level** (`~/.claude/`) applies to *every* project
automatically — you never copy it. This scaffold is only the **per-repo** layer (`<repo>/.claude/`
+ `<repo>/CLAUDE.md`) that a project carries in its own git history.

| Layer | Location | Action |
|---|---|---|
| Settings, plugins, global agents/hooks/skills, global doctrine | `~/.claude/` (incl. `~/.claude/CLAUDE.md`) | Already global — nothing to do |
| Project hook wiring, project agents/skills/rules, project rules | `<repo>/.claude/` + `<repo>/CLAUDE.md` | Copy from this scaffold, then fill TODOs |

## Already global (no action needed)

- **Plugins** (your code-review tool, issue tracker, etc.).
- **Generic safety hooks** (destructive-git, secret-exposure, inline-script, main-branch-push, ci-config-reminder) and the **session-start context injector** (`session_start_context.py`, which opens every session with the branch, working tree, recent commits, and newest decision-log entry) promoted to `~/.claude/hooks` — they fire in every repo, so this scaffold's `settings.json` does NOT re-wire them.
- **Universal ways-of-working** in `~/.claude/CLAUDE.md`. Keep the per-repo `CLAUDE.md` about *this* project only.

## New-project recipe

```
cd <newrepo> && kerbtap init
```

That is the supported path, and the only one. `kerbtap init` copies this scaffold and then **adapts** it: it derives the bypass env prefix from the repository directory name and renames every `PROJECT_*` occurrence across the hooks, `settings.json` and `CLAUDE.md`; it detects which surfaces the repo actually has and points each gate at those; it **drops** the gates whose surface is absent rather than leaving them wired at defaults, and prints what it dropped; and it runs the installed gate tests, failing the install if any fail.

**Copying this directory by hand is not supported.** The `TODO` markers below are *choices*, not unfinished code. Skip them and you install a gate pointed at paths your repository does not have: `precommit_review_guard.py` denies, but only for the paths in its constants. A gate pointed at nothing reads as coverage; a gate that never fires reads as coverage too.

`.mcp.json` is **project-scoped** (MCP servers in it are NOT inherited from `~/.claude/` — unlike plugins). It ships the `code-review-graph` server so the graph MCP loads in the new repo. The graph is per-repo data: build it once in the new repo (needs `uvx` / `code-review-graph` installed). Plugin-delivered MCPs (your code-review tool, issue tracker) are already global — not in this file.

### Adapting by hand (unsupported fallback)

If you install without the tool — a wheel with no checkout, an air-gapped copy — every item below is **blocking**, not advisory. Copy `.claude/`, `CLAUDE.md` and `.mcp.json` into the new repo, then work through the `TODO` markers:

- `grep -rn "TODO: adapt for <PROJECT>" <newrepo>/.claude` (and `<!-- TODO: project specifics -->` in `CLAUDE.md`).
- Rename each bypass env var — `PROJECT_REVIEWED`, `PROJECT_DOC_REVIEWED`, `PROJECT_CLAIM_VERIFIED` — in its hook, in the `settings.json` comment, and in `CLAUDE.md`.
- Fill the surface constants each gate needs: `TRACKED_PATHS` (doc-consistency), `LEDGER_REL` / `IN_SCOPE_RES` / `SUBJECT` (fact-claim). A gate pointed at nothing is worse than no gate — it reads as coverage.
- Fill `TYPO_PATTERNS` / `GLUED_ALLOWLIST` in `mangled_text_guard.py` if you want project-name typo detection.
- Delete the gates this project does not want, and their tests, rather than leaving them wired at defaults.
- Run the shipped hook tests once after adapting: `for t in .claude/hooks/test_*.py; do python3 "$t"; done`. They exercise each gate in both directions (fires when violated, silent when not), so they catch a constant you adapted into a gate that can no longer fire.
- Genericized agents/skills (`architect`, `cloud-devsec`, `verify-claims`, `wrap-up`, `sprint-start`, `ticket`, `frontend-design`) are worked examples — adapt the coupled passages or delete what you don't use. `asd-ste100` is vendored unchanged from upstream (MIT, pin in its `.pinned-commit`) and needs no adapting.

## What's inside

```
.claude/rules/    docker.md, python_runtime.md         (copied as-is, generic)
.claude/agents/   architect/, cloud-devsec.md          (genericized + TODOs)
.claude/skills/   verify-claims, wrap-up, sprint-start, ticket, frontend-design (templates + TODOs),
                  asd-ste100 (vendored upstream, unchanged)
.claude/hooks/    precommit_review_guard.py, precommit_doc_consistency_guard.py,
                  fact_claim_guard.py, mangled_text_guard.py
                  (+ test_*.py for the gates that ship with tests)
.claude/settings.json   per-repo hook wiring template
CLAUDE.md         project-rules skeleton
.mcp.json         project-scoped MCP servers (code-review-graph)
```
