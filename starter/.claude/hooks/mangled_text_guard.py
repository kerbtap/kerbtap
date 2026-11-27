#!/usr/bin/env python3
"""PostToolUse hook — warn about mangled text after Edit/Write.

Fires after Edit/Write to a text file and scans the written content for the
signatures of sed/regex bulk-rename damage and sub-agent typos that have
needed manual cleanup before:

  - Project-name typos (project-specific — see TYPO_PATTERNS, empty by default).
  - Doubled consecutive words ("free free trial", "the the").
  - Doubled tokens across an underscore/hyphen ("_trial_trial", "free-free").

Non-blocking. Surfaces line numbers so the damage is caught in the same turn.
Plain file/content scan — no intent guessing.
"""
import json
import re
import sys

# Only scan human-readable text; skip lockfiles/binaries.
TEXT_EXTENSIONS = (
    ".py", ".md", ".ts", ".tsx", ".js", ".jsx",
    ".json", ".yaml", ".yml", ".txt", ".html", ".css", ".sh",
)

# TODO: adapt for <PROJECT> — add your project's recurring product-name typos here.
# Word-boundary, case-insensitive. Empty by default so only the generic
# doubled-word / doubled-token checks run out of the box.
# Example — a common misspelling of a product name:
#     re.compile(r"\bacmee\b", re.IGNORECASE),  # typo for "Acme"
TYPO_PATTERNS: list[re.Pattern] = []

# Doubled consecutive word: "free free", "the the". Case-sensitive, len>=3 to
# trim the common valid-English doubles ("had had"). Non-blocking, so the rare
# false positive is harmless.
DOUBLED_WORD = re.compile(r"\b([A-Za-z]{3,})\s+\1\b")

# Doubled token across _ or -: "trial_trial", "free-free".
DOUBLED_GLUED = re.compile(r"\b([A-Za-z]{3,})([_-])\1\b")

# TODO: adapt for <PROJECT> — glued tokens that legitimately look doubled
# (known identifiers/handles, not rename damage). Case-insensitive match against
# the whole snippet. Empty by default. Example: a service-account handle like "svc_svc".
GLUED_ALLOWLIST: frozenset[str] = frozenset()


# Code span (markdown `x` AND RST ``x`` — one-or-more backticks each side) and
# a double-quoted span. The captured group is the inner content to shelter.
_CODE_SPAN = re.compile(r"`+([^`]+)`+")
_QUOTE_SPAN = re.compile(r'"([^"]*)"')


def _sheltered_spans(line):
    """Inner-content index ranges inside code spans / double-quotes on the line.

    These shelter the recurring false positives: code identifiers in backticks
    and *quoted examples* of past mangles (a log literally documenting a fixed
    "free free trial"). A real prose mangle is unquoted and still caught.
    """
    spans = []
    for pat in (_CODE_SPAN, _QUOTE_SPAN):
        for m in pat.finditer(line):
            spans.append((m.start(1), m.end(1)))
    return spans


def scan(content):
    """Return list of (lineno, snippet, reason) for every mangle signature.

    Filters the known false positives so the warning stays signal, not noise:
      - matches inside backticks/double-quotes (code identifiers; quoted
        examples of already-fixed mangles);
      - doubled WORDS whose first token is the tail of a hyphen/underscore
        compound (``migrate-init init``, ``named-contact contact-route``);
      - allow-listed glued identifiers.
    Unquoted prose mangles (a real ``free free``) are still caught.
    """
    hits = []
    patterns = [(p, "product-name typo") for p in TYPO_PATTERNS]
    patterns += [(DOUBLED_WORD, "doubled word"), (DOUBLED_GLUED, "doubled token")]
    for i, line in enumerate(content.splitlines(), start=1):
        spans = _sheltered_spans(line)
        for pat, reason in patterns:
            for m in pat.finditer(line):
                start = m.start()
                if any(a <= start < b for a, b in spans):
                    continue
                if reason == "doubled word" and start > 0 and line[start - 1] in "-_":
                    continue
                if reason == "doubled token" and m.group(0).lower() in GLUED_ALLOWLIST:
                    continue
                hits.append((i, m.group(0), reason))
    return hits


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if data.get("tool_name", "") not in ("Edit", "Write"):
        sys.exit(0)

    file_path = data.get("tool_input", {}).get("file_path", "")
    if not file_path or not file_path.endswith(TEXT_EXTENSIONS):
        sys.exit(0)

    try:
        with open(file_path, encoding="utf-8") as fh:
            content = fh.read()
    except (OSError, UnicodeDecodeError):
        sys.exit(0)

    hits = scan(content)
    if not hits:
        sys.exit(0)

    lines = "\n".join(
        f"  L{lineno}: {snippet!r} ({reason})" for lineno, snippet, reason in hits
    )
    warning = (
        f"POSSIBLE MANGLED TEXT in {file_path}:\n{lines}\n\n"
        "Check these are intentional — sed/regex bulk renames and sub-agent "
        "edits have produced doubled words and product-name typos before. "
        "Fix in this turn if mangled."
    )
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": warning,
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
