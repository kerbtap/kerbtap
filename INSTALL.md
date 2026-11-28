# Installing kerbtap

## 1. Requirements

| Needed | For |
|---|---|
| Claude Code, git | the plugin, its hooks and the tool |
| Python 3.11+ for the tool, `python3` 3.9+ for the hooks | each hook runs as `python3 <hook>`. `init` and `install-machine` refuse an older `python3` |
| `codex` | reviews, as the README states |
| `gh`, authenticated (optional) | pull-request evidence in the Evidence Pack |

## 2. What the README commands do

- `pipx install kerbtap`: installs the `kerbtap` tool.
- `claude plugin marketplace add kerbtap/kerbtap`: adds the plugin marketplace to Claude Code.
- `claude plugin install kerbtap@kerbtap --scope user`: installs the plugin for every repository on this machine.
- `kerbtap init .`: installs the project tier into the current repository.
- `kerbtap doctor`: checks the project gates.
- `kerbtap install-machine --marketplace kerbtap/kerbtap`: runs the two plugin steps in one command, for the machine tier.

The research agent is a second plugin. It needs `pwm`, so install it only where `pwm` is present:

```bash
command -v pwm && claude plugin install kerbtap-research@kerbtap
```

## 3. What `kerbtap init` does

- Derives the bypass prefix from the directory name: `carbonLedger` gets `CARBONLEDGER_REVIEWED`. `--prefix` overrides it.
- Points each gate at the surfaces it finds, and drops any gate with no surface. It prints the dropped gates.
- Writes `CLAUDE.md`, with a hook table, only when the repository has none.
- In a git work tree, installs a `pre-commit` hook that runs the commit gates on commits made outside Claude Code.
- Runs the installed gate tests and exits `1` if any fail. Flags: `kerbtap init --help`.
- Surface `docs/`: the review gate covers `.md` and `.json` under `docs/`. The fact-claim gate is installed, and `docs/sources-ledger.md` is created.
- Surface code (`src/`, `tests/` or a top-level `.py`): the review gate covers `.py` only with `--review-code`.

## 4. An existing `.claude/` or `CLAUDE.md`

- `init` merges `.claude/settings.json`: it replaces the kerbtap hook entries, keeps your other entries, and saves the old file as `settings.json.governor-backup-<UTC time>`.
- A changed scaffold file under `.claude/hooks`, `rules`, `agents` or `skills` stops the install and writes nothing. Diff the named files and commit, then use `--force`, which overwrites only those files.
- `init` never replaces your `CLAUDE.md`. Run `kerbtap merge-claude-md`. The merge model has no tools, and kerbtap checks the result before it writes it.

## 5. Check the install

`kerbtap doctor` shows whether each project gate fires as wired, and exits `1` on a broken gate. It does not check a live session: `/hooks` inside Claude Code lists the wired hooks. `claude plugin list` shows `kerbtap@kerbtap`.

## 6. Upgrade

```bash
pipx upgrade kerbtap
claude plugin marketplace update kerbtap
claude plugin update kerbtap@kerbtap
kerbtap init
kerbtap doctor
```

When `init` names a file, diff it, then re-run with `--force`.

## 7. Older installs

A release before 0.3 needs `kerbtap install-machine`. It installs the plugin, removes the kerbtap hook wiring from `~/.claude/settings.json` (the old file is kept as `settings.json.governor-backup-<UTC time>`), and moves the files an earlier release copied into `~/.claude/` to `~/.claude/governor-legacy-<UTC time>/`. It moves files and never deletes them. A machine that holds the plugin under the old name `governor` stops before any change.

Run these two commands in order, then start a new Claude Code session. `claude plugin marketplace remove governor` also uninstalls `governor@governor` and `governor-research@governor`, and deletes their saved plugin data:

```bash
claude plugin marketplace remove governor
kerbtap install-machine --marketplace kerbtap/kerbtap
```

## 8. From source (contributors)

```bash
git clone https://github.com/kerbtap/kerbtap.git
cd kerbtap/tool
pipx install --editable .
```

To install a wheel built from the checkout, run `python -m pip wheel --no-deps -w dist ./tool` from the checkout root, then `pipx install <wheel>`.

## 9. Troubleshooting

- **`error: externally-managed-environment`**: install with `pipx install --editable .`.
- **`scaffold not found at … — is this the kerbtap repository?`**: rebuild the wheel from a checkout.
- **`… need Python 3.9+`** (or `no python3 on PATH`): put a Python 3.9+ `python3` first on `PATH`, then re-run.
- **`these files in … differ from kerbtap's copies`**: diff the named file before you use `--force`.
- **`cannot derive an env-var prefix from directory name …`**: pass `--prefix NAME`.
- **`hook tests … FAILED`** (exit code 1): run each failing `test_*.py` directly to see the case that failed.

## 10. See also

- [`README.md`](README.md): what kerbtap is, how to install it, and its limits.
- [`USER-MANUAL.md`](USER-MANUAL.md): how to use it: the lifecycle, every gate and the Evidence Pack.
