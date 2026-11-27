# Contributing to kerbtap

## Before you start

Open an issue before a large change, so that the approach is agreed before you write the code. A small fix can go straight to a pull request.

## Set-up

```
python -m pip install -e "tool[dev]"
```

## Checks

CI runs these checks. Run them before you open a pull request.

```
(cd tool && python -m pytest)
```

```
sh -ec 'for t in global/hooks/test_*.py starter/.claude/hooks/test_*.py; do
  [ -f "$t" ] || { echo "no hook test matches: $t"; exit 1; }
  python "$t"
done'
```

## Pull requests

A change ships with its tests. A change to behavior needs a test that fails without it.

## Licence

kerbtap is under the Apache License 2.0. When you contribute a change, you license it under the same terms (section 5 of the license). There is no CLA.

## Security

Do not report a vulnerability in an issue or a pull request. See `SECURITY.md`.
