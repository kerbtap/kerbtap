"""Audit a repository's co-author trailers against the AI-trailer classifier.

Prints every distinct co-authored-by line found in the full history, how the classifier
labels it (AI / not-AI), and the resulting commit/line totals — so a classifier change
can be verified against the raw ground truth instead of a remembered number.

Usage:
    python scripts/audit_trailers.py <repo_path>
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kerbtap.assemble import _ai_coauthor_lines, _COAUTHOR_RE  # noqa: E402
from kerbtap.collect.git import collect_commits  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    repo = Path(sys.argv[1]).expanduser().resolve()
    commits = collect_commits(repo)

    all_lines: Counter[str] = Counter()
    ai_lines: Counter[str] = Counter()
    ai_commits = 0
    ai_line_total = 0
    for commit in commits:
        text = f"{commit.subject}\n{commit.body}"
        found = [m.group(0).strip() for m in _COAUTHOR_RE.finditer(text)]
        ai = _ai_coauthor_lines(text)
        all_lines.update(found)
        ai_lines.update(ai)
        if ai:
            ai_commits += 1
            ai_line_total += len(ai)

    print(f"repo: {repo}")
    print(f"commits scanned: {len(commits)}")
    print(f"AI trailer commits: {ai_commits}")
    print(f"AI trailer lines:   {ai_line_total}")
    print()
    print("--- distinct trailer lines (count | AI? | line) ---")
    for line, n in all_lines.most_common():
        label = "AI " if line in ai_lines else "not"
        print(f"{n:5d} | {label} | {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
