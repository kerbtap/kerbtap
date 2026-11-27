---
name: ticket
description: "Drive one <KEY> ticket through the delivery discipline: verify live state, verify the fix's mechanism, enumerate the failure dimensions, plan (with an independent plan review only for architectural or high-blast-radius work), route the build, self-review, transition the tracker before the commit, run the pre-push battery, then push, open the PR and merge. Use whenever the user says /ticket <KEY>-NNN, asks to work or pick up a ticket, or starts a sprint ticket. This is the authority for everything inside a single ticket."
---

<!-- Installed example — this file contains project-specific assumptions. Adapt it to this repository or delete it before relying on it. -->
<!-- Placeholders: <KEY> is the tracker project key, <OWNER> the person who decides business and product questions. -->
<!-- The battery table in section 8 names this scaffold's own checks; replace the rows with yours. -->

# Ticket

One ticket per invocation, never batched. This file is the **sequencer and the authority** for a single ticket;
`/sprint-start` wraps it for sprint-wide sequencing and must not restate anything here. Every step ends in one artifact
naming the later step that consumes it. None is a control — nothing verifies a pasted command ran, only that a missing
paste is visible in the transcript.

**Weight first.** A typo, a stale path or a one-line correction skips this skill: make the change, show it, batch it
onto the open branch. Routine work skips §3's independent plan review. A change to a reviewed deliverable, an
architectural change, a gate, or anything irreversible runs every step.

## 1 — Verify, never remember

- The <KEY> ticket from the tracker: status, parent, acceptance criteria, the **full description and every comment**.
  Tracker unreachable → say so; never work from a remembered spec. Re-query the moment <OWNER> reports a state change.
- **Name the reader.** Which decision or measurement changes when this lands, and who reads the output? No answer → the
  ticket is closed, not planned.
- `git status`, `git branch -avv` (full reconciliation, not `--short`), `git log --oneline -5`, and the project's latest
  decision record.
- Read every target file whole before planning against it, and confirm every symbol the plan will name with a search.
  A read path returning zero means naming two others, never probing IDs.
- Resuming a branch, stash or worktree older than this session: search its key across the project's logs, then list
  every path its diff touches against the default branch. A WIP dies by ruling or by deletion, and its own notes record
  neither.

A log hit is **context only**, never the evidence for an assertion about current state; a current source read is.

Artifact: those outputs plus each target's path and line count — §1b keys its reads on them.

## 1b — Verify the ticket's mechanism, not only its defect

§1 establishes the defect is real. This establishes the proposed **fix** closes it. A ticket's expected fix is a
hypothesis written by whoever saw the symptom — wrong exactly when the defect is structural.

For every mechanism the fix relies on, read it and write one line naming the `file:line` and what it guarantees:

- **Its predicate**, in full. A filter, a glob or a cutoff decides what the fix reaches; a fix that runs and matches
  nothing looks exactly like a fix that worked.
- **Where its inputs come from** — a clock at execution, a stored value, a constant, a hand-written list.
- **Whether it runs once or repeatedly**, and whether anything re-drives it.
- **What it is barred from touching** — the project's rules may put the surface it needs out of reach.

Then answer in the plan: does this close the defect for **every** case, or only the ones the ticket named? Name the
population it does not reach, or state that none exists. "Pre-existing", "also true elsewhere", "out of scope" and
"harmless" are load-bearing claims, not asides — check what each implicates before using it.

If the mechanism does not hold, re-plan here. Found after a build, it costs the build, the review and the plan.

Artifact: the per-mechanism lines and the "closes it for X, does not reach Y" sentence — §3 carries both.

## 2 — Enumerate the failure dimensions, before building

The expensive defects were never ones a reviewer missed; nobody had named the dimension. Write the matched rows only.

| The diff touches | Enumerate |
|---|---|
| A reviewed deliverable (the review gate's surface) | Each figure's source; agreement with every other document carrying it; only decided decisions relied on. |
| A gate or hook | The violating input it must deny, driven through the real hook. The bypass matrix: env-assignment prefix, the token quoted inside an argument, a message whose quoting breaks the tokenizer, a command run from another worktree or with `git -C`. Fail-closed on unreadable git state. The deny message names only its recorded override. |
| Removal of a capability | Every alternate entry point — env var, CLI flag, direct module call — fails closed. |
| A rule or skill file | Every path it cites resolves; every skill it invokes ships alongside it. |
| "X is safe because Y exists", or any filter, glob or allowlist | Which direction an omission fails, and whether the set is derived or hand-written. Before calling a check strict, run its exact CI command and ask what it declined to judge. |

Each row names its expected observable; the builder runs it and pastes the command, the observation, and what would have
failed had the work been wrong. Every plan also carries a testing, performance and logging section, or an
`N/A because…`.

Artifact: the matched rows.

## 3 — Plan

Decompose. Simplest thing that works first. The plan carries an explicit blocked-on — `build` / `review` / `merge` /
`nothing`, or a decision that is <OWNER>'s. Before promoting a "new risk" into a ticket or a gate, search the project's
decisions and rules for the general class; a re-discovery folds into what already covers it.

**Send the plan for an independent review only when the work is architectural or high blast radius.** Ask for
**findings** — severity, the `file:line` proving each, the correction — never "is this ready?". Verify each finding
against source before acting on it and correct what it names, no more.

Artifact: the plan carrying §1b's lines and any findings table.

## 4 — Start

Record the plan and build.

Artifact: the recorded plan. §5 dispatches against it.

## 5 — Build

- **Route by domain** when an agent genuinely owns the work and briefing it costs less than doing it. The reviewer
  reviews and never authors.
- The brief carries §2's matched rows verbatim, and any population as the command and its output, never a list typed
  from memory. It says "commit locally, hand me the commit", every round.
- Every file-mutating agent works in its own worktree. Follow up with an agent that has context, never a fresh
  dispatch. Review what comes back as if a stranger wrote it and re-run at least one binding proof yourself.
- Tests, performance and structured logging ship with the change.

Artifact: the builder's diff and its pasted binding proofs — §6 reviews them; §8 re-runs them.

## 6 — Self-review, then review

Every path confirmed, every symbol
searched, every count beside its population, one mutation per asserted property with its own pasted red.

**A change on the review gate's surface** then gets the reviewer agent's adversarial review. Record the verdict with
`kerbtap review record` and append the printed `Governor-Review` trailer to the commit message.

**At the second round returning the same class of finding, go back to §1b.**

Artifact: per round, the class named.

## 7 — Tracker before the commit

The transition and its status comment land **before** the matching commit, never after. If the commit is then blocked,
revert the transition. Filing a new ticket needs <OWNER>'s ask; something found while working this ticket is worked in
this ticket.

Artifact: the re-queried ticket — never the write's success response.

## 8 — Commit, the pre-push battery, then land

Feature branch always. One git operation per shell call. Read every modified file end-to-end first, then run the battery
rows the diff matches; a matched row deliberately skipped owes an `N/A because…`.

| The diff touches | Last commands before the push |
|---|---|
| `.claude/hooks/` | `for t in .claude/hooks/test_*.py; do [ -f "$t" ] \|\| continue; python3 "$t"; done` — paste the suite count. |
| Source code | The project's test suite — paste the pass count. |
| A reviewed deliverable | The recorded review: `kerbtap review verify HEAD` after the commit. |

Commit with `git commit -F <message-file>`, not a heredoc: an apostrophe in a heredoc breaks the gates' tokenizer, and
the bypass prefix is then not seen.

When the battery is green, push the branch, open the PR and merge it. End the turn naming the next buildable ticket, or why none.

Artifact: the pasted battery. `## Done` re-reads it against the landed head.

## Done

Four conditions, each with its own pasted proof; any one failing is stated as failing, never "essentially Done".

1. **Reviewed and landed** — the merged head matches the head that was reviewed.
2. **Tests verified** — the battery's pass counts at that head, run rather than assumed.
3. **Docs reconciled** — a dated entry in the project's decision record.
4. **Board matches reality** — the re-queried ticket, a clean `git status --short`, `git branch -avv` and the open PRs.
