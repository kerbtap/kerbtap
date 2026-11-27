# kerbtap

**The delivery discipline of a senior engineering team, installed into Claude Code.**

A spec before any code. The mechanism checked before the fix. A second model's eye on every plan and diff. Proof before anyone says "done". And hooks that hold the line when Claude wants to cut a corner.

![kerbtap demo](demo.gif)

## Why

- **Every project works differently.** kerbtap installs one way of working into each repository, so all your projects follow the same process.
- **Teaching Claude a new project takes too long.** `kerbtap init .` adds the agents, skills and rules in one command.
- **Claude can ignore the rules you write.** A rule in `CLAUDE.md` is text that Claude can skip. A hook runs before the tool call and refuses the command every time.
- **Requirements and process change from project to project.** Each change goes through the same stages: spec, plan review, build, code review, done.

## The lifecycle it installs

Claude Code does not get to skip from prompt to commit. Each change goes through these stages, and each stage has an owner.

| Stage | What happens | Held by |
|---|---|---|
| **Spec** | Every change becomes an [OpenSpec](https://github.com/Fission-AI/OpenSpec) change: the request quoted word for word, one `SHALL` per requirement, a scenario, and the test that will prove it. | `planner` agent, `openspec/` from `kerbtap init` |
| **Mechanism check** | Before any code, the plan reads each mechanism the fix depends on (its predicate, its inputs, whether it runs once) and names the cases the fix does not reach. A ticket's proposed fix is treated as a hypothesis. | `/ticket` |
| **Failure dimensions** | The plan lists what the change can break before it is built. A new gate ships with the inputs it must refuse, each bound to a test that proves the gate fires. | `/ticket` |
| **Plan review** | A different model ([Codex](https://github.com/openai/codex)) checks the plan against the quoted request: anything the request did not ask for fails the scope check, and every requirement gets PASS or FAIL. | `reviewer` agent, review contract |
| **Build** | The builder builds from the approved plan and nothing else, commits locally, and hands back its proofs: the command, the output, and the mutation that turns the test red. | `builder` agent |
| **Code review** | Codex grades the diff on seven criteria: effectiveness, efficiency, security, privacy, error handling, tests, compatibility. Only a serious finding the change introduced blocks. The same class of finding twice sends the work back to the plan, not into another patch. | `reviewer` agent, review contract |
| **Commit** | A commit to the paths you mark as reviewed is refused until the verdict is recorded. The verdict travels in the commit as a trailer. | `precommit_review_guard` hook |
| **Claims** | No "tests pass", "merged" or count in a status report without a source checked in that turn. What could not be checked stays "unknown" in the text. | `/verify-claims` |
| **Done** | Landed, tests re-run at the named commit, docs reconciled, tracker matching reality. The ticket moves before the commit, not after. | `/ticket` |
| **Sessions** | Each session starts from measured state: branch, working tree, last decision. Each ends with a compliance check and a handoff. | `session_start_context` hook, `/wrap-up` |

Agent briefs are written in Simplified Technical English (ASD-STE100), with a linter, because a subagent cannot ask back what you meant. A `/sprint-start` skill runs the same discipline across a sprint.

## Gates, with an exit you have to sign

A hook runs before the tool call. Claude cannot reinterpret it, forget it or decide this case is the exception.

- **Review gate:** no commit to reviewed paths without a recorded review.
- **Fact gate:** no statistic added to a fact-facing document without a verified source.
- **Doc gate:** a warning when a canonical document is committed without a consistency pass.
- **Machine-wide:** no push to `main`, no force-push or `reset --hard`, nothing that prints a credential.

Every blocking gate has a way through: a bypass that requires a written reason and an optional expiry, and each one is logged. Set any gate to `shadow` to see what it would refuse before you enforce it. `kerbtap doctor` fires each gate and tells you which ones actually work.

## Proof of process

`kerbtap change-record <PR>` prints how one change was authorized, bound to policy, verified and approved, with every bypass on the way. `kerbtap generate <repo>` does the same for the whole repository: an Evidence Pack of six documents for whoever asks how AI-written code gets checked. Both state what they do not claim.

## Install

```bash
pipx install kerbtap
claude plugin marketplace add kerbtap/kerbtap
claude plugin install kerbtap@kerbtap --scope user
cd /path/to/your/repo
kerbtap init .
kerbtap doctor
```

The plugin adds the agents, the review contract and the machine-wide hooks to every Claude Code session. `kerbtap init` adds the project gates, the skills, `openspec/` and a git `pre-commit` hook to one repository. Reviews need `codex` installed. Details: [INSTALL.md](INSTALL.md).

## Limits

- The hooks see only what Claude Code does. Outside it, the git `pre-commit` hook runs the commit gates, and `git commit --no-verify` skips it.
- The skills are procedure, not enforcement: a hook holds the commit, not every stage before it.
- The gate log is a local file. It is not tamper-evident.

## Documentation

- [INSTALL.md](INSTALL.md): install, upgrade and verify.
- [USER-MANUAL.md](USER-MANUAL.md): every gate, what it refuses, and the claims kerbtap does not make.
- [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [LICENSE](LICENSE) (Apache 2.0)
