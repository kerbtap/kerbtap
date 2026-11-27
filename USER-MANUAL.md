# kerbtap — User Manual

For developers who let AI agents write code, and now have to explain to somebody how that code got reviewed.

## What you get

- Hooks that block a commit or push that breaks your rules, machine-wide and per project.
- Agent roles and doctrine loaded into every session, from the `kerbtap` Claude Code plugin.
- An Evidence Pack built from your repository's own history. It states what it does not claim.
- Install: the machine tier is the `kerbtap` plugin, once per machine. The project tier comes from `kerbtap init`, once per repository. See [`INSTALL.md`](INSTALL.md).

---

## What problem this solves

Your team ships code written partly by AI agents. Sooner or later somebody asks a question you cannot answer from memory:

> *"How do you know an AI didn't push something nobody reviewed?"*

That question arrives from an enterprise customer's security questionnaire, an auditor, your board, or a prospect's CISO. Git history alone is a weak answer — it shows what landed, not what was enforced.

kerbtap gives you two things:

1. **Rules that actually stop you** — not a style guide agents ignore, but hooks that block the commit.
2. **An evidence pack** — a set of documents describing how changes in your repository were governed, generated from your repository's own history.

That's it. It is not a scanner, not a linter, and it does not tell you whether your code is correct.

---

## The two halves

kerbtap has two parts. You can use either alone.

### 1. The scaffold — rules with teeth

Two sets of files. The machine half is the `kerbtap` plugin: it is installed once per machine and applies everywhere. The project half is the scaffold from `kerbtap init`: it is installed in each repository. Its central idea:

> **A rule that is merely written down is a rule agents will break. A rule wired to a hook is a rule that stops them.**

You get:

| Piece | What it is |
|---|---|
| **Doctrine** | A short file of working rules, loaded into every agent session — git discipline, "verify before claiming", who decides what. |
| **Hooks** | Small scripts that run before a tool call and can **block** it. Eleven ship: seven machine-wide, four per project. |
| **Agents** | Role definitions: `planner`, `builder`, `reviewer`, `scribe` and `advisor` ship in the plugin. `researcher` ships as a separate plugin, installed only where `pwm` is present. |
| **Rituals** | Repeatable procedures: start a sprint, wrap up a session, verify a claim before writing it down. |
| **Skills** | `kerbtap init` installs them in each repository's `.claude/skills/`: `sprint-start`, `ticket`, `wrap-up`, `verify-claims`, `frontend-design`, and `asd-ste100`, which rewrites agent briefs and status text in Simplified Technical English and ships a linter (`.claude/skills/asd-ste100/scripts/ste-lint.py`). |
| **Memory** | Durable notes about the project, so the next session starts informed rather than guessing. |

Hooks are the load-bearing part. Everything else is convention; a hook is a wall.

#### The gates, and what each one refuses

Machine-wide (the `kerbtap` plugin):

| Gate | Refuses |
|---|---|
| `destructive_git_guard` | `reset --hard`, `checkout .` / `checkout --`, `restore .` / `restore --worktree`, `clean -f`, `branch -D`, remote branch delete (`push --delete`, `push -d`, `push <remote> :<branch>`), and `push --force` or `push -f`, alone or grouped (`-uf`), also behind git global options (`git -C . push --force`); `--force-with-lease` passes |
| `secret_exposure_guard` | a command that would print a credential: an `env` or `printenv` dump (piped, redirected or followed by another command); a reader such as `cat`, `source`, `head`, `tail` or `grep` on a `.env` file in any command segment (`.env.example`, `.env.sample` and `.env.template` pass); an `echo` of a variable ending in `KEY`, `TOKEN`, `SECRET`, `PASSWORD` or `PASSWD` |
| `main_branch_push_guard` | a push that lands on `main`: named explicitly, through a refspec, or implied by the current branch or its upstream |
| `inline_script_guard` | an inline `python -c` or `node -e` script longer than 150 characters or spanning lines; short one-liners pass |
| `ci_config_reminder` | nothing — after an edit to a workflow, `.pre-commit-config.yaml`, `pyproject.toml` or `requirements.txt`, it reminds you that local tests do not validate CI |
| `session_start_context` | nothing — it opens each session with the branch, the working tree and the last decision |
| `doctrine_context` | nothing — it adds the doctrine to each session when your `CLAUDE.md` does not already hold it |

Per project (installed by `kerbtap init`):

| Gate | Refuses |
|---|---|
| `precommit_review_guard` | a commit touching your reviewed surface with no review recorded |
| `precommit_doc_consistency_guard` | nothing. It warns on a commit touching a canonical living doc with no stale-reference pass, and logs it. |
| `fact_claim_guard` | an edit that adds a statistic to a fact-facing document with no verified source row |
| `mangled_text_guard` | nothing — it warns on doubled words and product-name typos after a write |

Where a gate has a test suite, that suite proves it **fires** on a violation, not merely that it stays quiet on the happy path — a gate nobody proved can fail certifies nothing, which is the same argument the Evidence Pack makes about everyone else's controls. Every blocking per-project gate that `kerbtap init` installs comes with its suite; `mangled_text_guard`, which only warns, has none. Machine-wide, four of the seven have suites (`main_branch_push_guard`, `secret_exposure_guard`, `session_start_context`, `doctrine_context`); `destructive_git_guard`, `inline_script_guard` and `ci_config_reminder` were vendored before that standard existed and are unproven. Read the tests beside a gate as the record of what it is proven to catch.

Run `kerbtap doctor [repo]` after an install and after each change to `.claude/settings.json`. It runs each of the three project gates on a violating probe and a harmless probe, in a temporary repository, and changes nothing in the checked repository. Each gate gets one line: `fires`, `silent`, `stuck` (it also denies the harmless call), `crashed`, `not wired` or `not installed`, with `(shadow)` at the end when the gate is in shadow mode. The git adapter line reads `not installed` or the result of a probe commit, and the command exits 1 when a result is not `fires` or `not installed`. Doctor does not show whether Claude Code runs the hooks in a live session.

Run `kerbtap change-record <PR number | branch> [--json]` to print the Agentic Change Record for one change: its commits, then five headings (Authorized, Policy-bound, Verified, Approved and Exception-controlled). It adds this machine's local trail (gate decisions with bypass reasons, doc-pass clears and shadow denies, Codex review passes, and commit verbs no gate judged), so each part says `recorded`, `none recorded` or `no local record`. The local stores are per machine, so a change built elsewhere reads `no local record`, never zero, and the record states what is recorded, not what is verified. A PR number needs the `gh` CLI, a branch name does not; paste the record into the PR body before merge.

#### Bypasses leave a trace

Every blocking gate has a way through, because a gate with no exit gets disabled. Each one is an explicit env-var prefix named after your repository (`MYREPO_REVIEWED=1 MYREPO_REVIEWED_REASON="<why>" git commit …`, optionally with `MYREPO_REVIEWED_UNTIL=<UTC time>`) or a literal token, never a silent condition. Each blocking gate writes one line to `.kerbtap/gate-log/events.jsonl` for each deny and each bypass. A bypass with no reason, a past expiry, or a line that cannot be written is refused. The Evidence Pack counts these lines in `exception-log.md` section 5. Shadow mode shows where a gate would block before you enforce it: set the gate to `shadow` in `.kerbtap/gate-mode.json` in the main worktree, and the command runs while the deny reason appears as a warning and one `shadow-deny` line is written. `kerbtap learn` and the Evidence Pack count shadow denies per gate, apart from incidents and bypasses (`Shadow denies` column in section 5). The log is a local file that a local user or agent can edit, so the counts are recorded, not verified. The point is not to prevent every bypass. It is that the bypass rate is measurable rather than invisible. An install made before version 0.5 keeps its state in `.governor/`; every reader and writer uses `.governor/` when it exists.

### 2. The Evidence Pack — a document set you can hand someone

Point the tool at a git repository. It reads the repository's own history and produces six files describing how changes were governed.

---

## Generate your first Evidence Pack

```bash
kerbtap generate /path/to/your/repo -o ./evidence-pack
```

You'll see something like:

```
Evidence Pack written to ./evidence-pack (115 commits, 52 PRs, 20 reviewed changes)
```

Six files land in the output directory:

| File | What it holds |
|---|---|
| `report.md` | The narrative — what was assessed, what was found, over which commits. |
| `manifest.json` | The structured record. Every change, its custody trail, and what is *not* being claimed. |
| `control-map.md` | Your evidence lined up against the controls auditors ask about (SOC 2, DORA, SSDF, NIST). A **mapping**, not a certification. |
| `exception-log.md` | Where the rules were bypassed, and why. |
| `remediation-plan.md` | What to fix, in priority order. |
| `executive-summary.md` | One page for the person who won't read the rest. |

### Two flags worth knowing

By default, output is **safe to send to a customer**: absolute paths and author emails are stripped.

```bash
# Include author emails and full paths — internal use only
kerbtap generate /path/to/repo --internal

# Show what it is doing
kerbtap generate /path/to/repo -v
```

### Recording a review so the pack can count it

A change counts as attested only when the commit carries a review trailer that resolves to a review artifact stored in that commit's tree. The verdict file must list the reviewed paths in `subject`; `review record` binds each one to the content staged when it runs, so stage exactly what was reviewed first. The pack then requires the artifact to cover the commit at the content that was committed: every guarded file the commit changes, or every file it changes when none is guarded. A record on a merge or an empty commit never counts. An older artifact that names no files still counts, and the pack reports it separately as "scope not recorded". The pack calls a record that passes these checks *integrity-checked*, never *verified*:

```bash
kerbtap review package --staged
# prints the pinned diff and its manifest — hand both to the reviewer as its primary input
kerbtap review record --review-output verdict.json --verdict PASS --reviewer reviewer
# or, from the reviewer's saved final message (reviewers hold no file-writing tool):
kerbtap review record --from-message reviewer-output.md --verdict PASS --reviewer reviewer
# either prints a Governor-Review trailer block — append it to the commit message
kerbtap review verify <commit>    # re-check it later
```

Prose in a commit body saying a review happened is recorded as a *claim*, never as an attestation. That distinction is the whole point: an unverifiable assertion is exactly what the pack exists to replace.

---

## What the pack claims — and what it refuses to claim

This is the most important section in this manual.

The pack makes **exactly one** claim about itself:

> *This Evidence Pack is evidence reviewed and packaged from named systems.*

And it says so, in every file. It explicitly states that it is **not**:

- cryptographically tamper-proof, tamper-evident, or immutable
- audit-grade, notarized, non-repudiable, or certified
- a statement that your code is correct or secure
- an audit opinion or a legal-compliance certification

This isn't lawyerly hedging. It is enforced in code. A guard scans every generated file, and if a word like *tamper-proof* or *audit-grade* appears as a claim about the pack, **the build fails and no pack is written**. Overclaiming is treated as a bug, because a customer who discovers you oversold your evidence will not trust the parts that were true.

The commit hashes in the manifest are content pointers, used to find and de-duplicate evidence. They are not presented as proof that nobody tampered with anything.

### Where your data goes

By default: **nowhere**. The pack ships at *Tier 0 — proof-only*: pointers, hashes and metadata leave your repository; the content stays in your systems.

---

## `/ship` and `/steward`

`/ship` and `/steward` are Claude Code commands that watch a pull request.

- The kerbtap hooks in Claude Code examine tool calls. Each hook matches one or more of the tools `Bash`, `Edit`, `Write` and `MultiEdit`.
- When Claude Code runs a git command itself, outside a tool call, these hooks do not examine it.
- The git `pre-commit` hook that `kerbtap init` installs examines a commit only when git runs its hooks. kerbtap installs no git hook that examines a push.
- The review that `/ship` does in the session is not a kerbtap review record. Only `kerbtap review record` makes a review record.
- `/ship` and `/steward` do not merge. kerbtap does not need them to merge.

---

## Troubleshooting

**`HONESTY GUARD TRIPPED — pack not written`** (exit code 3)
A generated file contained a barred word — `tamper-proof`, `immutable`, `audit-grade` — outside the fixed disclaimer. Usually a quoted PR title. This is the guard working. The offending term is named in the error.

**Pull-request evidence is missing**
`gh` isn't installed or isn't authenticated. The pack records what it could read and does not invent the rest.

**`git commit` was blocked**
A hook stopped you. Read what it said — it names the rule and the way through.
**A claim was blocked with "unverified empirical claim"**
The fact-claim gate. A statistic entered a fact-facing document with no verified row in the sources ledger. Fetch the primary source, copy the line that states the figure, add the ledger row, retry. A citation is not verification; the row is.

---

## See also

- [`README.md`](README.md): what kerbtap is, the lifecycle it installs, and how to install it.
- [`INSTALL.md`](INSTALL.md): install the tool, the machine tier and the project tier, then verify them.
