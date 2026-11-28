# kerbtap

[![PyPI](https://img.shields.io/pypi/v/kerbtap)](https://pypi.org/project/kerbtap/)
[![CI](https://github.com/kerbtap/kerbtap/actions/workflows/ci.yml/badge.svg)](https://github.com/kerbtap/kerbtap/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://pypi.org/project/kerbtap/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

**The delivery discipline of a senior engineering team, installed into Claude Code.**

![kerbtap demo](demo.gif)

## Why

- **Every project works differently.** kerbtap installs one way of working into each repository, so all your projects follow the same process.
- **Teaching Claude a new project takes too long.** `kerbtap init .` adds the agents, skills and rules in one command.
- **Claude can ignore the rules you write.** A rule in `CLAUDE.md` is text that Claude can skip. A hook runs before the tool call and refuses the command every time.
- **Requirements and process change from project to project.** Each change goes through the same stages: spec, plan review, build, code review, done.

## What it does

Once installed, Claude Code in your repository:
- plans each change before it writes code, and a second model (Codex) reviews the plan and the diff;
- cannot commit to your reviewed paths until a review is recorded;
- cannot push to `main`, force-push, `reset --hard` or print a credential;
- starts each session from the branch, the working tree and the last decision, and ends it with a handoff.

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
- [USER-MANUAL.md](USER-MANUAL.md): how to use it: the lifecycle stage by stage, every gate, and the Evidence Pack.
- [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [LICENSE](LICENSE) (Apache 2.0)
