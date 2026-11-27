# OpenSpec changes

The planner writes each plan as an OpenSpec change. The reviewer builds its brief from that change.

## Layout

A change goes in `openspec/changes/<ID>/`. `<ID>` is the ticket ID.

- `proposal.md` starts with `## Source`. Quote the request there, word for word. Then give `## Why`, `## Out of scope` and `## Impact`.
- `specs/<capability>/spec.md` starts with `## ADDED Requirements`. Each requirement has one `SHALL`, a `Verification:` line, and at least one `#### Scenario:` with `WHEN` and `THEN`.
- `tasks.md` lists the tasks as `- [ ] N. text`. It ends with a `## Files` section. The section lists each path that the change touches, one backticked path per `- ` bullet, with the change's own `openspec/changes/<ID>/` included. A path that ends in `/` covers each path below it. The reviewer's `scope` check compares this list with the branch diff.

`openspec/specs/review/spec.md` is the review contract (REV-001 to REV-010). Do not edit it.

## Check a change

```bash
npx -y @fission-ai/openspec@1.16.0 validate <ID> --strict
```

The command needs Node 22.12 or later. Fix each error before review.

After the code review passes, archive the change:

```bash
npx -y @fission-ai/openspec@1.16.0 archive <ID> --yes
```

## Example change

`openspec/changes/demo/proposal.md`

```markdown
## Source

> Request: print a greeting when the program starts.

## Why

The program prints nothing at start, so the user cannot see that it runs.

## Out of scope

Greeting text in other languages.

## Impact

`main.py` prints one line at start.
```

`openspec/changes/demo/specs/greeting/spec.md`

```markdown
## ADDED Requirements

### Requirement: GRT-001 The program prints a greeting
The program SHALL print `hello` on its first output line when it starts.
Verification: `python -m pytest tests/test_greeting.py`.

#### Scenario: Start
- **WHEN** the program starts
- **THEN** the first output line is `hello`
`Verification: test_first_line_is_hello`
```

`openspec/changes/demo/tasks.md`

```markdown
## Tasks

- [ ] 1. Print the greeting in `main.py` (GRT-001).
- [ ] 2. Add `tests/test_greeting.py`.

## Files

- `main.py`
- `tests/test_greeting.py`
- `openspec/changes/demo/`
```
