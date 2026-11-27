# review Specification

## Purpose
Codex plan review and code review: input, criteria, Definition of Done, passes and review level.

## Requirements

### Requirement: REV-001 No scope creep
The builder and the reviewer SHALL add no deliverable that the source does not contain, except a direct
dependency: existing code the deliverable calls or uses that must change for the deliverable to work.
Verification: inspection of the review prompts in `global/review/review-contract.md`.

#### Scenario: Reviewer asks for unrelated work
- **WHEN** a finding asks for work outside the source and is not a direct dependency
- **THEN** it does not block; if P1 or P2 it goes to the owner as a candidate ticket, if P3 it is dropped

#### Scenario: Security exception
- **WHEN** the reviewer finds a security vulnerability outside the scope
- **THEN** it reports it; it blocks if this change introduced it, and goes to the owner as an urgent ticket if not

### Requirement: REV-002 What versus how
The plan review SHALL fail scope only for a new deliverable, never for a means the plan chose to deliver a
sourced one: the source decides what is delivered, the plan decides how.
Verification: inspection of the plan-review prompt in `global/review/review-contract.md`.

#### Scenario: Plan adds a telemetry line to measure a sourced change
- **WHEN** the plan adds a mechanism that serves a sourced deliverable
- **THEN** the scope check does not fail on it

### Requirement: REV-003 Plan review
The plan review SHALL receive the proposal (the brief), the specs and `tasks.md`, and return a scope PASS or FAIL
followed by one PASS or FAIL per requirement ID.
Verification: `test_plan_brief_copies_source_verbatim` in `scripts/test_review_brief.py`.

#### Scenario: Plan adds an unrequested feature
- **WHEN** the plan includes a deliverable that traces to no source item and is not a direct dependency
- **THEN** the scope check fails

### Requirement: REV-004 Plan review Definition of Done
The plan review SHALL be done when scope is PASS, every requirement is PASS, and no P1 or P2 finding in the plan is open.
Verification: inspection of the Definition of Done in `global/review/review-contract.md` `## plan`.

#### Scenario: One requirement fails
- **WHEN** scope is PASS and one requirement is FAIL
- **THEN** the plan review is not done

### Requirement: REV-005 Code review
The code review SHALL receive the approved plan (the proposal as the brief, the specs and `tasks.md`) and the
diff, and return PASS or FAIL on each of seven criteria that the builder also receives before it builds.
Verification: `test_code_brief_has_seven_criteria` in `scripts/test_review_brief.py`.

#### Scenario: Criteria
- **WHEN** the reviewer scores the diff
- **THEN** each criterion is PASS only when:
  - Effectiveness: every requirement is delivered as the approved spec says
  - Efficiency: no code the spec does not need; no new layer where existing code does the job
  - Security: the change adds no exploitable vulnerability
  - Privacy: no secret, credential or personal data exposed in code, logs or output
  - Error handling: every error on a path the change adds or modifies is caught, has a clear message, and is logged
  - Tests: every code behaviour has a test that passes; every other requirement is proven as its Verification line
    states
  - Compatibility: existing callers, data and config still work, unless the spec says otherwise

#### Scenario: Missing evidence
- **WHEN** the reviewer cannot find evidence for a criterion
- **THEN** that row is FAIL with the reason, never an assumed PASS

### Requirement: REV-006 Code review Definition of Done
The code review SHALL be done when all seven criteria are PASS, no P1 or P2 finding introduced by the change is
open, and CI is green on the reviewed commit.
Verification: inspection of the Definition of Done in `global/review/review-contract.md` `## code`.

#### Scenario: CI red after approval
- **WHEN** the reviewer passes all seven criteria and CI fails on that commit
- **THEN** the code review is not done

### Requirement: REV-007 Findings
Each finding SHALL name its requirement ID or criterion, its severity (P1-P3), and whether this change introduced
it; only an introduced P1 or P2 blocks.
Verification: inspection of the finding rules in `global/review/review-contract.md` `## pass1`.

#### Scenario: Pre-existing defect
- **WHEN** the reviewer finds a P2 defect in code the change did not touch
- **THEN** it does not block and goes to the owner as a candidate ticket

### Requirement: REV-009 Telemetry
Each review SHALL append one line to `.governor/telemetry/reviews.jsonl` with the change ID, review type, passes,
blockers raised and out-of-scope findings.
Verification: `.governor/telemetry/reviews.jsonl` has one line after the first review.

#### Scenario: Pilot read-out
- **WHEN** five Tier 2 tickets have closed under this process
- **THEN** the owner reads the telemetry at `/wrap-up` and decides whether to port it

### Requirement: REV-010 Review level
`GOVERNOR_REVIEW_LEVEL` SHALL set which work gets each Codex review: 1 all Tier 2+ work gets both; 2 (default)
the code review covers Tier 2+ and the plan review Tier 1 and architectural work only; 3 both cover Tier 1 and
architectural work only.
Verification: `python3 scripts/test_review_brief.py`, the review-level matrix.

#### Scenario: Variable unset
- **WHEN** `GOVERNOR_REVIEW_LEVEL` is unset and the work is Tier 2
- **THEN** the code review is required and the plan review is not

#### Scenario: Variable unset, Tier 1
- **WHEN** `GOVERNOR_REVIEW_LEVEL` is unset and the work is Tier 1
- **THEN** both reviews are required

#### Scenario: Unknown value
- **WHEN** `GOVERNOR_REVIEW_LEVEL` is `4`
- **THEN** `scripts/review_brief.py required` exits 2 and names the valid values

### Requirement: REV-011 A repeated finding class stops the review
A review SHALL stop on a failed pass whose blocking finding is in a class already fixed on this change, or on the fifth failed pass, and on a stop the author closes the PR if one is open, keeps the branch and goes back to the plan.
Verification: inspection of `CLAUDE.md` `## Codex` and ticket skill §6.

#### Scenario: Same class again
- **WHEN** a pass fails with a blocking finding in the class that an earlier fix on this change closed
- **THEN** the review stops, the PR closes if one is open, the branch stays, and the work goes back to the plan

#### Scenario: Fifth failed pass
- **WHEN** the fifth pass fails, with blocking findings in classes not fixed before
- **THEN** the review stops the same way

#### Scenario: A new class before the fifth failed pass
- **WHEN** the third pass fails with a blocking finding in a class not fixed before
- **THEN** the author fixes the class and the review continues

### Requirement: REV-012 The contract carries the stop rule
The `## pass2` rules of `global/review/review-contract.md` SHALL tell the reviewer to label a blocking finding "repeated class" when it is in the class of a finding fixed in an earlier pass, and SHALL state that the author stops the review on a repeated class or on the fifth failed pass.
Verification: `check("pass 2 brief carries the stop rule (REV-012)", ...)` in `scripts/test_review_brief.py`.

#### Scenario: Pass 2 brief
- **WHEN** `review_brief.py brief <ID> code --pass 2` builds a brief
- **THEN** the brief contains "repeated class" and "fifth failed pass"
`Verification: the check above`
