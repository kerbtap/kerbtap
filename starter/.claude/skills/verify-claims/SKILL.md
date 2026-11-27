---
name: verify-claims
description: "Source-grounded verification of factual assertions via a review tool. Reduces Claude's failure mode of asserting unverified state ('X is deployed on the production server', 'N tests passing', 'PR #N is open', 'function Y does Z') as fact in synthesis prose. Three modes: pre-draft (gather verified facts before writing), post-draft (verify a draft you have already written), doc-pass (stale-ref / count / cross-section sweep on a long living doc). Invoke as /verify-claims <mode> [args]. Use whenever the response will assert state about the production server, deploy state, test counts, PR/branch state, file existence, function behavior, schema state, merged-vs-open status, or counts — and on every status / planning / decision-log turn."
---

<!-- Installed example — this file contains project-specific assumptions. Adapt it to this repository or delete it before relying on it. -->
<!--   - the verification backend is a CLI review subagent — swap for your reviewer/tool; -->
<!--   - "the production server" / "prod" are external-state examples — replace with yours; -->
<!--   - the issue-tracker keys, the decision log, and the eval paths under data/eval/ are project-specific. -->
<!-- Keep the structure (3 modes, claim ledger, hard rules); replace the nouns. -->

# Verify Claims

Routes factual assertions through a review tool for source-grounded confirmation before they land in user-facing prose. Backstops the inference-as-fact pattern that Claude exhibits under synthesis load.

## Modes

| Mode | When | Input | Output |
|---|---|---|---|
| `pre-draft` | BEFORE writing synthesis prose. **Build a claim ledger first** (see below), submit ledger items as claims | Claim ledger (claim ↔ evidence pairs) + files | Per-claim verification report |
| `post-draft` | AFTER writing synthesis prose, BEFORE sending. Use as retrofit when no ledger was built upstream — costlier than `pre-draft` because the conversion point has already happened | The draft prose + cited files | Per-claim verification report + revision guidance |
| `doc-pass` | On long living docs with cross-refs / counts | Target doc path | List of stale §X.Y refs, count mismatches, broken paths |

## Mandatory triggers

Run this skill (default mode: `pre-draft`) on every:

<!-- TODO: adapt for <PROJECT> — the trigger list below names project docs (CLAUDE.md, a master-context doc, a repo map) and external state (the production server, prod). Replace with this project's living docs + external surfaces. -->
- Project-status / planning / "what's next" / kanban / backlog answer
- Decision-log entry draft
- `CLAUDE.md`, your master-context doc, your repo map, spec or architecture-doc edit
- Cross-reference fixup pass on any long doc
- Any prose paragraph that asserts state about the production server, prod, test counts, PR or branch state, file existence, function behavior, schema state, or counts of anything

If the answer is small enough to be a one-line fact derived from a single command output that just ran in this conversation, the skill is optional. If the answer synthesizes across multiple sources or relies on memory of files read earlier, the skill is mandatory.

## Claim ledger (synthesis-class turns)

The dominant /verify-claims failure mode is not "the reviewer confirmed a false claim". It is **the synthesis-time slip**: weak evidence becomes confident prose because the conversion point ("I think X" → "X is true") happens silently inside the draft. Once the answer is written, the slip is invisible — the reviewer can refute the claim, but the act of having drafted confidence has already shaped the response.

A claim ledger exposes that conversion point while the answer is still editable. Build it BEFORE writing the prose, in your scratch / thinking layer, in this exact format:

```
Claims I am about to assert:
1. ...
2. ...
3. ...

Evidence:
1. <file:line OR command output OR issue-tracker key OR `unknown`>
2. ...
3. ...
```

Then `/verify-claims` (mode `pre-draft`) checks the ledger, not the finished prose. The ledger is what gets submitted as the claim list — one ledger entry per claim, one evidence anchor per entry.

**When the ledger is required** (synthesis-class turns):

<!-- TODO: adapt for <PROJECT> — same project-state nouns (production server / prod / your tracker) as above. -->
- Project-status / planning / "what's next" / kanban / backlog answers
- Decision-log entry drafts
- Any prose paragraph that asserts state about the production server / prod / test counts / PR or branch state / file existence / function behavior / schema state / counts

**When the ledger is NOT required** (don't ritualise it):

- Coding turns where the response is a code change + a one-line summary derived from the diff itself
- Single-fact lookups where the answer traces directly to one command output that just ran
- Conversational replies, clarifying questions, scoping discussions

**Ledger constraints:**

- **Cap at 5 claims** unless the answer truly needs more. If you find yourself listing 7–8, the answer is probably bundling multiple sub-questions; either narrow the response or split it.
- **Every claim has an evidence row at the same index.** If you cannot name the evidence, the evidence row is `unknown` — and that claim must carry an inline uncertainty marker in the final prose (the existing hard rule below). Empty evidence rows are not allowed.
- **Build the ledger BEFORE drafting prose, not as a retrofit afterwards.** A retrofit ledger is post-draft mode — useful but lossy, because the prose has already been shaped by the unaudited claim-to-confidence conversion the ledger is supposed to police.

## How to invoke (Claude-side procedure)

<!-- TODO: adapt for <PROJECT> — step 4 dispatches a review subagent (`<your-review-subagent>`). Replace with whatever verification backend/agent this project uses, or drop if none. -->
1. Identify the mode (`pre-draft`, `post-draft`, `doc-pass`). For synthesis-class turns: prefer `pre-draft` and build the claim ledger above first.
2. For `pre-draft`: the ledger IS the claim list. Submit each ledger entry as one claim; each evidence row becomes the source-of-truth pointer the reviewer consults.
3. Build a tight review prompt of the shape below.
4. Dispatch via `Agent(subagent_type="<your-review-subagent>", prompt=<the prompt>)`. Do not invoke it as a re-entrant slash command — that re-enters the slash command and hangs.
5. Receive structured output. If the reviewer returns prose instead of the schema, re-prompt requesting the schema.
6. Apply the result. For `pre-draft`: draft on top of the verified facts; carry every `unknown` verdict to the final prose with an inline uncertainty marker. For `post-draft`: revise the draft per `rewrite_guidance`. For `doc-pass`: fix the flagged refs/counts, then run `kerbtap doc-pass record <doc>...` on the passed docs and stage them. The doc gate then allows the commit with no bypass; a doc edited after the record is denied.

### Prompt template

```
Read-only review. Do not edit any files.

Mode: <pre-draft | post-draft | doc-pass>

Claim ledger (one claim + one evidence anchor per entry):
1. claim:    <claim>
   evidence: <file:line | command output | issue-tracker key | unknown>
2. claim:    <claim>
   evidence: <file:line | command output | issue-tracker key | unknown>
...

Files / paths to consult:
- <file:line range or whole file>
- <file>

For each claim in the ledger, verify the claim against the named evidence
and return JSON of this shape:

{
  "claims": [
    {
      "claim": "<exact assertion verbatim>",
      "status": "confirmed | refuted | unknown",
      "evidence": "<file:line, command output, or 'no source consulted'>",
      "rewrite_guidance": "<concrete revision text>"
    }
  ],
  "missing_coverage": ["<state assertion that should have been in the claim list but wasn't, if any>"]
}

Status semantics:
- confirmed = source reviewed, claim matches source verbatim
- refuted = source reviewed, claim contradicts source
- unknown = no source available, source ambiguous, or claim is about external state (the production server, prod, network) that cannot be checked from the repo

Rewrite guidance must be concrete revision text, not advice. For unknown: provide the uncertainty-marked sentence.
For doc-pass mode: claims are stale §X.Y refs, count mismatches, broken paths, dangling sections.
```

## Required output schema

The review tool MUST return JSON of this shape. If it returns prose, re-prompt:

```json
{
  "claims": [
    {
      "claim": "<exact assertion>",
      "status": "confirmed | refuted | unknown",
      "evidence": "<file:line OR command:output OR 'no source consulted'>",
      "rewrite_guidance": "<concrete revision text>"
    }
  ],
  "missing_coverage": ["<claim that should have been in the claim list but wasn't>"]
}
```

## Hard rule: `unknown` survives to final prose

If the review tool returns `status: unknown` for any claim, the final user-facing prose MUST carry that uncertainty explicitly. Examples:

<!-- TODO: adapt for <PROJECT> — the deploy-state examples below are illustrative; the rule itself is universal. -->
- ✅ "The production server may not be on the latest release (unverified — last decision-log entry says the rollout is pending)."
- ❌ "The production server is not on the latest release."
- ❌ Silently dropping the claim from the response.

Smoothing `unknown` into a confident sentence reintroduces the failure this skill exists to prevent. Re-state the claim with an inline uncertainty marker; do not omit and do not collapse to confidence.

## Hard rule: `missing_coverage` is non-negotiable

If the review tool flags `missing_coverage` items — claims that should have been in the verification list but weren't — those claims are added to a fresh verification round before the response goes out. Do not write prose that includes any claim outside the verified set.

## Anti-patterns (do not do)

- Skipping the skill on a synthesis-class turn because "I am sure of these facts." Confidence is exactly the failure mode.
- **Drafting prose without surfacing the claim ↔ evidence pairs first.** Skipping the ledger and going straight to writing is the conversion-point slip the ledger exists to expose. If you find yourself drafting first and ledger-ing afterwards, you are in `post-draft` mode by accident.
- Submitting an incomplete claim list. Include every state assertion the response will make, even ones that feel obvious.
- Leaving an evidence row blank. If you cannot name a file/command/issue-tracker source, the evidence is `unknown` — and the claim has to carry an inline uncertainty marker in the final prose. Don't silently drop the claim.
- Treating `unknown` as a soft confirmation. It is not.
- Generalizing the reviewer's findings beyond the claims it ruled on (per `feedback_review_finding_scope`).
- Writing prose that re-introduces an unverified claim that wasn't in the submitted list (the failure mode returns through the back door).
- Dispatching with `--write` for review work. This skill is read-only — pass that explicitly in the review prompt.

## Cost (per invocation)

| Mode | Claude tokens | Review-tool tokens |
|---|---|---|
| `pre-draft` | ~3k | ~10–15k |
| `post-draft` | ~2k | ~5–10k |
| `doc-pass` | ~3k | ~15–25k |

<!-- TODO: adapt for <PROJECT> — references a decision log, which may not exist in your project. -->
Coding-only sessions: zero cost (skill not invoked). Synthesis-heavy sessions: 2–5 invocations is typical. See your decision log for the projection rationale.

## Bite history

<!-- TODO: adapt for <PROJECT> — this section is a worked example of how the discipline -->
<!-- was rolled out incrementally (in small "bites"). Keep it as a template for your own -->
<!-- changelog, or delete and start fresh. The ticket refs / eval paths are illustrative. -->
- **Bite 1** — this skill body, three modes, opt-in.
- **Bite 2** — a `precommit_doc_consistency_guard.py` that soft-blocks commits to canonical living docs unless a `DOC_REVIEWED=1` bypass env var is set.
- **Bite 3** — eval set + measurement harness at `data/eval/verify-claims-eval.jsonl` and `scripts/dev/run_verify_claims_eval.py`. A small corpus over six failure-mode classes (`pr_state`, `branch_state`, `test_count`, `deploy_state`, `file_existence`, `function_behavior`, `schema_state`). The live baseline run is gated behind `--live --allow-token-spend`; metrics land in your decision log. Disputed (first-pass `confirmed`) records are re-run at a higher reasoning level via `--adjudicate`.
- **Bite 4** — claim ledger procedure (this section above). Forces the claim ↔ evidence conversion point into the open while the answer is still editable. Discipline is enforced by the skill body and the eval, not by a shell hook (per `feedback_hooks_stay_boring`).
