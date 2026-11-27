# kerbtap — CLI (v0)

Two jobs. `kerbtap init` installs the scaffold into a repository; `kerbtap generate`
points at a repository, collects governance evidence from its own systems, and emits the
six-component **AI SDLC Evidence Pack**.

The pack is **evidence reviewed and packaged from named systems** —
not tamper-proof, immutable, or audit-grade, and never sold as such. That honesty rule is
enforced in code (`kerbtap/honesty.py`): every generated component is scanned before it is
written, and the build fails loudly if a barred integrity term leaks in.

## Install / run

No third-party runtime dependencies (standard library only). Python 3.11+.

```bash
pipx install kerbtap
kerbtap generate /path/to/repo -o /tmp/evidence-pack
```

The wheel carries the scaffold, so `kerbtap init` needs no configuration. `--source` is
only for installing from a checkout of the kerbtap repository.

From a checkout:

```bash
cd tool
pip install -e .            # on an externally-managed Python (e.g. Homebrew) this
                            # fails; use: pipx install --editable .
```

## `kerbtap init` — install the scaffold into a repository

```bash
cd /path/to/your-repo
kerbtap init
```

One command, no TODO markers left behind. It:

- copies the scaffold, preferring the hardened gate variants from kerbtap's own
  `.claude/` where they exist — `starter/` ships a review guard in `"ask"` mode, which is
  silently auto-approved when `skipAutoPermissionPrompt` is set, so installing that copy
  installs a gate that does not gate;
- derives the bypass env prefix from the repo directory name (`carbonLedger` →
  `CARBONLEDGER_REVIEWED`, `CARBONLEDGER_DOC_REVIEWED`, …) and renames every occurrence
  across the hooks, `settings.json`, and `CLAUDE.md`;
- detects which surfaces the repo actually has — docs, Python code — and points each gate
  at those;
- **drops** gates whose surface is absent rather than leaving them wired at defaults, and
  prints what it dropped. A gate pointed at nothing reads as coverage; so does a silent
  omission;
- creates `docs/sources-ledger.md`, which the fact-claim gate reads;
- runs the installed gate tests and fails the install if any fail.

| Flag | Effect |
|---|---|
| `--prefix NAME` | Override the derived bypass prefix. |
| `--source PATH` | kerbtap checkout to install from (default: the scaffold bundled in the wheel, or the checkout an editable install runs from). |
| `--force` | Overwrite an existing `.claude/`. |
| `--no-tests` | Skip the post-install gate-test run. |

Default output directory for `generate` is `<repo>/evidence-pack/`. Use `-o` to write
elsewhere (e.g. `/tmp`) so you do not dirty the assessed repo's working tree.

## What it collects

| Source system | How | Required? |
|---|---|---|
| git (`git log`) | full commit history, merge/branch structure, per-commit signals | yes |
| GitHub (`gh`) | pull-request records and review evidence | optional (degrades to git-only) |
| governance signals | the review hook, scaffold config, reviewer agent, decision log, dogfood log | detected, cited |

Per-commit signals parsed from commit bodies: recorded adversarial-review evidence
(`Adversarial review` + `PASS`), ticket refs, and how each change landed
(merge-PR / squash-PR / direct-to-main).

## The six components

`report.md`, `manifest.json`, `control-map.md`, `exception-log.md`,
`remediation-plan.md`, `executive-summary.md` — see the spec for each component's contract.

## Layout

```
kerbtap/
  init.py             # scaffold install: surface detection, prefix renaming, gate selection
  honesty.py          # the claim string, barred-terms list, assert_clean guard
  model.py            # typed dataclasses (manifest schema, no signature field)
  assemble.py         # collected evidence -> EvidenceBundle with per-change records
  cli.py / __main__.py
  collect/            # git.py, github.py, governance.py
  build/              # one module per component + common.py; __init__ runs honesty guard
  data/
    control_clauses.json   # static control-clause lookup table (data, not code)
tests/                # git-signal parsing, control-map lookup, honesty
```

## Tests

```bash
cd tool
python -m pytest
```
