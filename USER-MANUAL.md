# kerbtap: User Manual

For a developer who has just installed kerbtap. Install steps: [`INSTALL.md`](INSTALL.md).

## 1. What you get after the install

Skills (`.claude/skills/`):
- `/sprint-start`: runs a sprint with a plan-first routine.
- `/ticket`: takes one ticket from plan to merged pull request.
- `/wrap-up`: checks the session and writes a handoff.
- `/verify-claims`: checks a claim against its source.
- `frontend-design`: builds and reviews web front ends, with accessibility rules.
- `asd-ste100`: rewrites agent briefs in Simplified Technical English.

Agents: `planner`, `builder`, `reviewer`, `scribe`, `advisor` (plugin), and `researcher` (second plugin, only where `pwm` is installed).

Machine-wide gates (plugin):
- `destructive_git_guard`: refuses `reset --hard`, `push --force` and similar.
- `secret_exposure_guard`: refuses a command that would print a credential.
- `main_branch_push_guard`: refuses a push that lands on `main`.
- `inline_script_guard`: refuses a long inline `python -c` or `node -e`.
- `ci_config_reminder`: reminds you that local tests do not check CI.
- `session_start_context`: shows the branch, the tree and the last decision.
- `doctrine_context`: adds the working rules to each session.

Per-repository gates (from `kerbtap init`):
- `precommit_review_guard`: refuses a commit to reviewed paths with no review.
- `precommit_doc_consistency_guard`: warns on a canonical doc with no consistency pass.
- `fact_claim_guard`: refuses a statistic with no verified source row.
- `mangled_text_guard`: warns about doubled words and typos.

## 2. How to use it

A typical day: start Claude Code in your repository, make one change with `/ticket <KEY>-NNN` (it plans, builds, reviews, then pushes, opens the PR and merges), and end with `/wrap-up`. `/sprint-start` runs the same discipline across a sprint. `/verify-claims` checks a claim before you write it down.

Each change goes through these stages:

| Stage | What happens | Held by |
|---|---|---|
| **Spec** | Each change becomes an OpenSpec change with one SHALL per requirement. | `planner` agent, `openspec/` from `kerbtap init` |
| **Mechanism check** | Before code, the plan verifies each mechanism the fix relies on. | `/ticket` |
| **Failure dimensions** | The plan lists what the change can break before it is built. | `/ticket` |
| **Plan review** | Codex checks the plan; each requirement gets PASS or FAIL. | `reviewer` agent, review contract |
| **Build** | The builder builds from the approved plan and commits locally. | `builder` agent |
| **Code review** | Codex grades the diff on seven criteria. A serious new finding blocks. | `reviewer` agent, review contract |
| **Commit** | A commit to reviewed paths is refused until the verdict is recorded. | `precommit_review_guard` hook |
| **Claims** | No status claim without a source checked in that turn. | `/verify-claims` |
| **Done** | Landed, tested at the named commit, docs reconciled, tracker matches. | `/ticket` |
| **Sessions** | Starts from measured state; ends with a compliance check and handoff. | `session_start_context` hook, `/wrap-up` |

## 3. When a gate stops you

A deny message names the rule and the way through. A bypass takes a reason:

```bash
MYREPO_REVIEWED=1 MYREPO_REVIEWED_REASON="<why>" git commit …
```

A bypass with no reason is refused. Each deny and bypass is one line in `.kerbtap/gate-log/events.jsonl`. Shadow mode shows a block without enforcing it: set the gate to `shadow` in `.kerbtap/gate-mode.json`.

## 4. Check that it works

Run `kerbtap doctor`. Each project gate reports `fires`, `silent`, `stuck`, `crashed`, `not wired` or `not installed`. Doctor does not show whether Claude Code runs the hooks in a live session.

## 5. Limits, and `/ship` and `/steward`

- `/ship` and `/steward`: the kerbtap hooks do not examine a git command that Claude Code runs outside a tool call. The other limits are in the [README Limits section](README.md#limits).

## 6. Extras

`kerbtap change-record` prints the Agentic Change Record for one change: commits, then five headings. It shows how the change was authorized, bound to policy, verified and approved, with every bypass on the way. Paste it into the PR body before you merge. A PR number needs `gh`; a branch name does not. Example: `kerbtap change-record 42`.

`kerbtap generate` writes the Evidence Pack, six files built from the repository's history, for whoever asks how AI-written code gets checked. Paths and author emails are stripped unless you pass `--internal`.

```bash
kerbtap generate /path/to/your/repo -o ./evidence-pack
```

This Evidence Pack is evidence reviewed and packaged from named systems. It does not claim to be tamper-evident, audit-grade or certified.

## 7. Troubleshooting

- **`HONESTY GUARD TRIPPED — pack not written`** (exit 3): a generated file has a barred word. The error names it.
- **Pull-request evidence is missing**: `gh` is not installed or not authenticated.
- **`git commit` was blocked**: read the deny message. It names the rule and the way through.
- **`unverified empirical claim`**: add the primary source's row to the sources ledger, then retry.

## 8. See also

- [`README.md`](README.md): what kerbtap is, how to install it, and its limits.
- [`INSTALL.md`](INSTALL.md): install and verify the machine and project tiers.
