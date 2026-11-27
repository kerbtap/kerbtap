# Acme Pricing Service

Rust + Postgres service that prices quotes for the broker network.

## Architecture

Axum HTTP layer, a pricing core with no IO, and a Postgres store behind a repository trait.
The pricing core is pure so it can be property-tested; nothing else is.

## Commands

- `just test` runs unit and property tests.
- `just migrate` applies sqlx migrations.

## Conventions

Errors bubble as `anyhow::Error` at the edges and concrete types in the core.
No unwrap outside tests.

<!-- GOVERNOR:BEGIN -->
## Governance gates

Installed by `governor init` from the gates actually wired. Regenerated on install;
edit outside the markers, never inside them.

| Hook | Event | Behaviour |
|------|-------|-----------|
| `mangled_text_guard.py` | PostToolUse / `Edit|Write` | Warns on doubled words/tokens and project-name typos after a write. Non-blocking. |
| `precommit_doc_consistency_guard.py` | PreToolUse / `Bash` | **Hard-blocks** (deny mode) a `git commit` touching a canonical living doc listed in `TRACKED_PATHS` without a stale-ref / count-drift pass. Bypass: `REPO_DOC_REVIEWED=1` as an env-assignment prefix. |
| `precommit_review_guard.py` | PreToolUse / `Bash` | **Hard-blocks** (deny mode) a `git commit` touching the guarded surface (`TRACKED_PATH_PREFIXES` / `TRACKED_SUFFIXES`) without a review. Bypass: `REPO_REVIEWED=1` as an env-assignment prefix. Enforces from the first commit. |

Each gate is cleared by its own env-assignment prefix — `REPO_REVIEWED=1`,
`REPO_DOC_REVIEWED=1`, `REPO_CLAIM_VERIFIED=1` —
placed before the command, never quoted inside an argument.

Each gate ships a `test_*.py` beside it that drives the real hook and asserts it
fires on a violation. Run them after changing a gate's constants; a gate that can no
longer fire is the failure worth catching.
<!-- GOVERNOR:END -->
