# Installing kerbtap

kerbtap installs in two tiers.

| Tier | What it is | Install |
|---|---|---|
| Machine tier: the `kerbtap` Claude Code plugin | Hooks, agents, the `/advisor` skill, review tooling and doctrine. Applies to every repository. | Once per machine (section 1). |
| Project tier: the scaffold | The `.claude/` hooks, rules, agents and settings, adapted to one repository. | In each repository: `kerbtap init` (section 3). |

The plugin needs no Python tool. Install it first. The `kerbtap` CLI is the next step, because `kerbtap init` is one of its commands.

---

## Requirements

| Needed | For |
|---|---|
| Claude Code | the plugin; the hooks are Claude Code hooks, and no other agent runs them |
| git | the plugin hooks and the tool |
| Python 3.9+ as the `python3` on `PATH` | the plugin: each hook runs as `python3 <hook>`, and a hook that cannot start blocks nothing. `init` and `install-machine` refuse an older one |
| Python 3.11+ | the `kerbtap` tool |
| `gh`, authenticated | optional: pull-request evidence in the Evidence Pack |
| `uvx` | only with `kerbtap init --with-code-review-graph` |

---

## 1. The plugin (machine tier, once per machine)

```bash
claude plugin marketplace add kerbtap/kerbtap
claude plugin install kerbtap@kerbtap --scope user
```

The plugin carries the seven machine hooks, the five agents (`planner`, `builder`, `reviewer`, `scribe`, `advisor`), the `/advisor` fallback skill, the review tooling and the doctrine.

The research agent is a second plugin, `kerbtap-research@kerbtap`. It needs `pwm`, so install it only where `pwm` is present:

```bash
command -v pwm && claude plugin install kerbtap-research@kerbtap
```

The repository is private: the marketplace clones it with your own git access (SSH, then HTTPS with your credential helper). Claude Code has no token of its own.

`kerbtap install-machine` runs these steps in one command.

```bash
kerbtap install-machine --marketplace kerbtap/kerbtap
```

It gives the same result on a new machine. On a machine with a release before 0.3 it is required, because it also performs the cutover below. It needs the tool from section 2.

### Upgrading from a release before 0.3

`kerbtap install-machine` does three things, in this order:

1. Installs `kerbtap@kerbtap` at user scope from the marketplace. It also installs `kerbtap-research@kerbtap` when `pwm` is on `PATH`.
2. Removes the kerbtap hook wiring from `~/.claude/settings.json`. Other entries stay. The previous file is kept beside it as `settings.json.governor-backup-<UTC time>`.
3. Moves the files an earlier release copied into `~/.claude/` to `~/.claude/governor-legacy-<UTC time>/`. Files are moved, never deleted. A file kerbtap never shipped stays where it is, and `~/.claude/CLAUDE.md` is not touched.

Flags:

- `--dry-run` prints the plan and runs no `claude` command.
- `--marketplace <source>` installs the plugin from another marketplace.
- `--home <dir>` cuts over another home directory.
- `--force` and `--source` are gone. The command refuses either with exit 2.

A second run changes nothing.

It refuses, having written nothing, when `claude` is not on `PATH`, when the hook `python3` is older than 3.9, or when a settings entry runs a kerbtap hook with a command kerbtap did not write. Rewrite or remove that entry by hand, then re-run; adding a second wiring beside it would run the gate twice. If a `claude` plugin command fails, the cutover stops and the legacy install is intact. Fix the cause and re-run. Inside Claude Code, `/hooks` lists what is actually wired.

### Move from the governor plugin

A machine that holds the plugin under its old name `governor` makes `kerbtap install-machine` stop before any change. Run these two commands in order:

```bash
claude plugin marketplace remove governor
kerbtap install-machine --marketplace kerbtap/kerbtap
```

`claude plugin marketplace remove governor` also uninstalls `governor@governor` and `governor-research@governor`, and deletes their saved plugin data. Then start a new Claude Code session. `kerbtap install-machine` does not remove the old marketplace itself, because that step deletes data.

---

## 2. The tool

```bash
git clone https://github.com/kerbtap/kerbtap.git
cd kerbtap/tool
pip install -e .
kerbtap --help
```

On an externally managed Python, such as Homebrew's, `pip install` fails with `externally-managed-environment`. Use `pipx install --editable .` instead.

An editable install finds the scaffold by walking up from the installed package to the checkout, so it always tracks what you have checked out.

### On a machine without the checkout

The wheel bundles the scaffold and the plugin marketplace, so a machine needs no checkout and no access to the repository. Install it from PyPI:

```bash
pipx install kerbtap
kerbtap init .
```

`kerbtap init` also installs the machine tier from the bundled marketplace when `claude` is on `PATH` and the plugin is not yet installed. Without `claude`, it tells you to run `kerbtap install-machine` later. To upgrade, run `pipx upgrade kerbtap`.

To install a wheel built from a checkout instead, run `python -m pip wheel --no-deps -w dist ./tool` from the checkout root, then `pipx install <wheel>`.

---

## 3. The project tier (per repository)

```bash
cd /path/to/your/repo
kerbtap init
```

`kerbtap init` copies the scaffold into `.claude/` and adapts it to the repository:

- It derives the bypass prefix from the directory name, so `carbonLedger` gets `CARBONLEDGER_REVIEWED`, `CARBONLEDGER_DOC_REVIEWED` and so on. Use `--prefix` to override it.
- It points each gate at the surfaces the repository has and **drops** any gate with no surface to guard. The dropped gates are printed.
- It writes `CLAUDE.md` with a hook table generated from the gates it installed, but only if the repository has no `CLAUDE.md` yet. When you already have one it is never written to; see [Your own `CLAUDE.md`](#your-own-claudemd) below.
- When `docs/` is a surface, it creates `docs/sources-ledger.md`.
- It runs the installed gate tests and exits `1` if any fail.
- It leaves no `TODO` markers in the hooks, rules or settings, and writes nothing outside the repository. A `CLAUDE.md` it writes is a skeleton: its `TODO` comments are yours to fill.

| Surface detected | What it changes |
|---|---|
| `docs/` | The review gate covers `.md` and `.json` under `docs/`; the fact-claim gate is installed. |
| `src/`, `tests/` or a top-level `.py` | The review gate covers `.py` under `src/` and `tests/` only with `--review-code`; a code-only repository gets no review gate without it. |
| Neither | Treated as `docs/`: a new repository acquires docs first. |

The doc-consistency gate and the text-quality warning are installed in every case. Enforcement starts at the first commit. A code-only repository installed without `--review-code` gets only those two.

`kerbtap init` does not write `.mcp.json` unless you pass `--with-code-review-graph`. That file makes Claude Code run a third-party MCP server through `uvx`. All flags are listed in `kerbtap init --help`.

In a git work tree, `kerbtap init` also installs a git `pre-commit` hook, so a commit made outside Claude Code runs the two commit gates. The hook runs `.claude/git-hooks/pre_commit.py`. An existing `pre-commit` hook is renamed to `pre-commit.governor-chained` and runs after the gates; it is never overwritten. In a tree with no `.claude/git-hooks/pre_commit.py`, the hook runs only the chained hook. `kerbtap git-hook status` reports the hook, `kerbtap git-hook install` adds it, and `kerbtap git-hook remove` takes it out and puts the old hook back. The hook does not see `git commit --no-verify`, the `HEAD` paths of `--amend`, or a `merge`, `cherry-pick`, `revert`, `rebase` or `am` that git completes without `pre-commit`.

### A repository that already has `.claude/`

`kerbtap init` merges into it:

- files that the scaffold does not ship are left alone, and a file identical to kerbtap's copy is not rewritten;
- `.claude/settings.json` is **merged**: the hook entries kerbtap owns are replaced, every other hook, event and key you had is kept, and the previous file is saved as `settings.json.governor-backup-<UTC time>`. A settings entry that runs a kerbtap hook with a command kerbtap does not write stops the install before anything is written;
- a file under `.claude/hooks`, `rules`, `agents` or `skills` that differs from the scaffold file at the same path stops the install. It names each such file and writes nothing.

Diff each named file. `--force` then overwrites exactly those files and nothing else; commit first, so you can restore what you want to keep. Neither form touches your `CLAUDE.md`.

### Your own `CLAUDE.md`

That file states what your repository is. kerbtap will not replace it, with or without `--force`. Instead `init` parks its gate block as a merge brief at `.kerbtap/pending/claude-md.md` and tells you so.

```bash
kerbtap merge-claude-md          # in the repository
```

The merge is done by the Claude Code CLI already on your machine. It reads your file and places the gate block where your document's own structure says it belongs. The model runs with **no tools**, from an empty scratch directory, so it can only return text.

What it returns is checked before a byte is written:

- every heading of your file still exists;
- every paragraph of your file still exists, ignoring re-wrapping;
- the gate block appears exactly once and is byte-equal to what `init` renders;
- no claim about tamper-proofing, immutability or certification was introduced.

A proposal failing any of those is refused: your file is untouched and the proposal is left at `.kerbtap/pending/claude-md.proposal.md` for you to read. A merge that passes is written after saving the previous file as `CLAUDE.md.governor-backup-<UTC time>`, and the diff is printed. Running it again when the block is already current does nothing. An install made before version 0.5 keeps its state folder `.governor/`, and the paths above sit there.

Flags: `--no-apply` verifies and writes the proposal without touching `CLAUDE.md`; `--model` and `--timeout` control the call. Without the `claude` CLI on `PATH`, nothing is written and the brief stays pending.

### Copying `starter/.claude` by hand is not supported

The template's `TODO` markers are choices: which paths are guarded, where the sources ledger lives, what the reviewer is called. An unadapted copy installs a review guard that denies, but only for kerbtap's own paths. It reads as coverage and does not gate your repository. If you have no alternative, follow `starter/README.md` and treat every `TODO` as blocking.

---

## 4. Verify

Run the tests after installing and after every upgrade:

```bash
# machine tier: the plugin's hook tests, from the checkout
for t in global/hooks/test_*.py; do python3 "$t" || echo "FAILED: $t"; done
python3 /path/to/governor/scripts/check_installed_hooks.py   # needs a checkout

# project tier, from the repository root
kerbtap doctor   # each gate fires, as wired
for t in .claude/hooks/test_*.py; do python3 "$t" || echo "FAILED: $t"; done

# plugin status: shows kerbtap@kerbtap
claude plugin list
```

From a wheel there is no `scripts/` directory. Run `kerbtap install-machine --marketplace kerbtap/kerbtap` again instead: a machine that is already cut over changes nothing.

`kerbtap doctor` runs each project gate on a violating probe and a harmless probe, in a temporary repository, reports whether each one fires as wired, and exits 1 on a broken gate; it changes nothing in the repository. It does not show whether Claude Code runs the hooks in a live session, so check `/hooks` as well.

---

## 5. Upgrade

```bash
cd /path/to/governor && git pull
claude plugin marketplace update kerbtap
claude plugin update kerbtap@kerbtap
```

- **The machine tier:** run the two plugin update commands above. From a release before 0.3, run `kerbtap install-machine --marketplace kerbtap/kerbtap` instead. A machine already cut over changes nothing. Check the plugin's state with `python3 scripts/check_installed_hooks.py`; it reports `plugin` and flags `DOUBLE` for any legacy wiring.
- **The tool:** an editable install picks up the change with nothing to reinstall.
- **The project tier:** run `kerbtap init`. A gate that changed upstream differs from your installed copy, so `init` names it and writes nothing; once you have diffed those files, re-run with `--force`.
- **From PyPI:** run `pipx upgrade kerbtap`, then `kerbtap doctor`.

Then repeat section 4.

---

## 6. Troubleshooting

**`error: externally-managed-environment`**
Install with `pipx install --editable .`.

**`scaffold not found at … — is this the kerbtap repository?`**
The tool is neither an editable install inside a checkout nor a wheel built by the command above — a wheel built some other way carries no scaffold. Rebuild it, or pass `--source /path/to/governor`.

**`… the hooks run as python3 <hook> and need Python 3.9+`** (or `no python3 on PATH`)
Put a Python 3.9 or later `python3` first on `PATH`, then re-run. Claude Code runs the hooks with its own `PATH`; if you start it from the desktop rather than a shell, check that it finds the same `python3`.

**`these files in … differ from kerbtap's copies; … nothing was written`**
A file in your `.claude/` sits at a scaffold path with other content. Read the section on an existing `.claude/` above before re-running with `--force`.

**`cannot derive an env-var prefix from directory name …`**
The directory name has no letters or digits. Pass `--prefix NAME`.

**`hook tests … FAILED`** (exit code 1)
The scaffold is installed, but those gates are unproven. Run each failing `test_*.py` directly to see the case that failed.

---

## See also

- [`README.md`](README.md): what kerbtap is, the lifecycle it installs, and how to install it.
- [`USER-MANUAL.md`](USER-MANUAL.md): what each gate refuses, the Evidence Pack, and the claims kerbtap does not make.
