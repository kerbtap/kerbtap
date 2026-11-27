---
name: sprint-start
description: "Standing operating doctrine for the start of a <PROJECT> sprint. Loads the verify-first, plan-first, orchestrate-don't-implement way of working: heavy investment in a reviewed plan up front to maximise one-shot delivery, mandatory architect adversarial review before any build, mandatory specialised-subagent fan-out with Claude as orchestrator, and the project's quality gates (tests + review + tracker-before-commit). Use whenever the user says /sprint-start, kicks off a sprint, or asks Claude to take on the sprint's work."
---

<!-- Installed example — this file contains project-specific assumptions. Adapt it to this repository or delete it before relying on it. -->
<!-- ID + project key), a code-review gate, a domain-specific compliance gate, the "Workflow" -->
<!-- multi-agent tool, named memory keys (feedback_*), and an owner named <OWNER>. Replace the -->
<!-- tracker + gate references with yours; the verify→plan→approve→orchestrate→reconcile spine is universal. -->

# Sprint Start — Operating Doctrine

**Argument (optional).** `/sprint-start <N>` targets sprint number `N`
(e.g. `/sprint-start 16`). With no argument, target the active sprint via
`openSprints()`. Use the argument when kicking off a specific or future
sprint that `openSprints()` would not yet return.

This is **how you work this sprint**, not a checklist to race through. The
bet: a great plan, hardened before a line is written, collapses later
iterations and lets each ticket land in one shot. Spend lavishly on the plan;
the build is cheap by comparison.

You are an **orchestrator**, not an implementer. Your job is to decompose,
fan out specialised sub-agents, validate and follow up on their work, and
collect their outcomes into a coherent whole. Sub-agent use is **not
optional** — fan out whenever the work is parallelisable. You implement
directly only for trivial changes (<3 lines), per CLAUDE.md `## NEVER`.

<!-- TODO: adapt for <PROJECT> — the named compliance gate is domain-specific. -->
Respect every project rule and hook already in place (CLAUDE.md, the
`.claude/hooks/` guards, and any domain-specific compliance gate). They take
precedence over anything here.

---

## Phase 0 — Verify-first (no memory, ever)

Establish ground truth before a single claim or decision. Memory and prior
context are stale by default (`feedback_verify_before_asserting`).

<!-- TODO: adapt for <PROJECT> — board bullet is issue-tracker-MCP-specific (cloud/workspace ID, project key, sprint-container field). Replace with your tracker, or drop if no sprint board. -->
- **Live board** — reuse the `/wrap-up` board queries (your tracker's MCP, `cloudId`/workspace `<YOUR_CLOUD_ID>`, project `<KEY>`): sprint contents, orphan Subtasks under Done parents, `sprint-NN` label vs sprint-container field mismatches, carried-over Done issues. **Scope to the target sprint:** if argument `N` was given, query `sprint = "Sprint N"` (by name) and the `sprint-NN` label (`feedback_tracker_sprint_container_required` — container is source of truth, label is query convenience); otherwise `sprint in openSprints()`.
- **Repo** — `git status` + `git branch -avv` (full reconciliation, not `--short`; `feedback_status_means_full_repo_state`), `git log --oneline -5`, latest `docs/decisions/log.md` entry.
- Surface every hygiene issue (carry-over candidates, orphans, mismatches) and **stop for <OWNER>'s call** — do not auto-move tickets (`feedback_decision_authority`).

State facts only after the commands confirm them.

---

## Phase 1 — The plan (highest-quality stage of the pipeline)

The plan is where quality is won. Treat it as the most rigorous artifact you
produce all sprint.

<!-- TODO: adapt for <PROJECT> — "Workflow tool (Opus 4.8 multi-agent orchestration)" is environment-specific. Keep if available; otherwise plan with a single rigorous pass. -->
**Generate with a Workflow when the sprint is non-trivial.** Use the Workflow
tool (Opus 4.8 multi-agent orchestration) to produce a plan that is broader
and better-stress-tested than a single pass:

1. **Diverse-candidate panel** — fan out parallel plan candidates, each from a
   distinct angle (simplest-first, risk-first, value-first, scale-first).
2. **Judge + synthesise** — score the candidates against the ticket's
   acceptance criteria, then synthesise the winner while grafting the best
   ideas from the runners-up.
3. **Adversarial verify** — spawn skeptic agents to refute the synthesised
   plan (hidden coupling, missing migration axis, untested edge, scope creep).

Match the workflow's weight to the sprint's difficulty — a one-ticket bugfix
needs a single planning pass, not a panel (`feedback_dont_overengineer_obvious_decisions`).
Workflow is an explicit opt-in; only reach for it when the planning fan-out
earns its cost.

**Then: mandatory architect review before building.** Hand the synthesised
plan to the `architect` agent for an adversarial pass — structure, data flow
between agents, scaling, refactor decisions. The plan is not "ready" until the
architect's findings are resolved (CLAUDE.md `## Always` #8).

A plan worth approving names, per ticket: scope + acceptance criteria, the
target modules (read in full first — `feedback_read_full_target_first`), the
sub-agents to fan out and their isolation needs, the test + perf + logging
deltas (`feedback_testing_performance_logs`), the PR grouping, and the risks
the adversarial pass surfaced.

---

## Phase 2 — Approval gate (hard stop)

Present the reviewed plan. **Write nothing until <OWNER> explicitly approves**
(CLAUDE.md `## NEVER`; `feedback_ok_not_authorization` — "OK"/"sure" is
acknowledgement, not a go). One blocking question at a time if anything is
unclear (`feedback_one_question_at_a_time`). You are not autonomous; you are a
disciplined orchestrator who waits for the call.

---

## Phase 3 — Execute as orchestrator

For each approved ticket, run the `/ticket` ritual (it carries its own
verify-first, tests, review, tracker-transition-before-commit, and
grouped-commit discipline). Across tickets:

- **Fan out, don't serialise.** Independent tickets/files run as parallel
  sub-agents. Parallel agents that mutate files **must** use
  `isolation: worktree` (`feedback_parallel_agents_need_worktree_isolation`).
<!-- TODO: adapt for <PROJECT> — the code-review gate and its bypass env var (PROJECT_REVIEWED) match the precommit hook in this scaffold. Rename both together. -->
- **The review step hardens the gate** — every `src/**/*.py` / `tests/**/*.py`
  commit runs your code-review tool first (hook-enforced). Never self-set
  `PROJECT_REVIEWED=1` (`feedback_review_hard_rule`); fix only
  what the review flagged (`feedback_review_finding_scope`).
<!-- TODO: adapt for <PROJECT> — prod-deploy ownership is project-specific. -->
- Push is <OWNER>'s — surface the exact command, never run it. Never touch
  `prod` (`feedback_never_touch_prod`).

---

## Phase 4 — Reconcile + log

- Per-ticket **verification log**: what was claimed, what was checked, the
  evidence. Done = reviewed code landed + tests verified + state files updated
  + board matches reality (`feedback_done_definition`) — never on intent.
- Reconcile the tracker board to actual progress (the `/wrap-up` Step 2 patterns).
- Append a dated entry to `docs/decisions/log.md`.

---

## The one rule under all of it

Plan deeply, review adversarially, verify before asserting, orchestrate the
build, harden with review, and let <OWNER> decide. Quality up front; fewer
iterations after.
