"""Inspect a generated Evidence Pack: summarize the manifest and confirm honesty.

Usage:
    python scripts/inspect_pack.py <pack_dir>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Allow running from tool/ without install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kerbtap.honesty import BARRED_TERMS, HONEST_CLAIM, INTEGRITY_DISCLAIMER  # noqa: E402

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_ABS_PATH_RE = re.compile(r"/(?:Users|home)/\S+|[A-Za-z]:[\\/]Users[\\/]\S+", re.IGNORECASE)


def main(pack_dir: str) -> int:
    root = Path(pack_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    changes = manifest["changes"]
    reviewed = [c for c in changes if c["review"]["attested"]]
    landing: dict[str, int] = {}
    for c in changes:
        landing[c["landed_via"]] = landing.get(c["landed_via"], 0) + 1

    print(f"claim: {manifest['claim']}")
    print(f"scope: {manifest['scope']['commit_range']}  as_of {manifest['scope']['as_of']}")
    print(f"commits={len(changes)}  prs={manifest['scope']['assessed_prs']}  landing={landing}")
    print(f"review-attested changes: {len(reviewed)}")
    for c in reviewed:
        print(f"  {c['change_id']} [{c['landed_via']:10}] {c['subject'][:66]}")

    print("\n--- barred-term scan across all files (must be 0 outside disclaimer) ---")
    total = 0
    for path in sorted(root.glob("*")):
        text = path.read_text(encoding="utf-8")
        scrubbed = text.replace(INTEGRITY_DISCLAIMER, " ").lower()
        hits = [t for t in BARRED_TERMS if t in scrubbed]
        disclaimer_present = INTEGRITY_DISCLAIMER in text
        claim_present = HONEST_CLAIM in text
        total += len(hits)
        print(f"  {path.name:22} barred_outside_disclaimer={hits or 'none':<8} "
              f"disclaimer={'yes' if disclaimer_present else 'no ':<3} claim={'yes' if claim_present else 'no'}")
    print(f"\nTOTAL barred terms outside disclaimer: {total}")

    pii_total = 0
    if manifest.get("external_safe", False):
        print("\n--- external-safe PII scan (emails / absolute home paths; must be 0) ---")
        for path in sorted(root.glob("*")):
            text = path.read_text(encoding="utf-8")
            emails = _EMAIL_RE.findall(text)
            paths = _ABS_PATH_RE.findall(text)
            pii_total += len(emails) + len(paths)
            print(f"  {path.name:22} emails={len(emails)} abs_paths={len(paths)}"
                  + (f"  e.g. {(emails + paths)[0]!r}" if emails or paths else ""))
        print(f"TOTAL PII leaks: {pii_total}")
    total += pii_total

    print("\n--- first enriched pull_request record (if any) ---")
    enriched = next((c for c in changes if c.get("pull_request")), None)
    if enriched is None:
        print("  none (git-only run or no PR-landed changes)")
    else:
        print(f"  on change {enriched['change_id']} [{enriched['landed_via']}]")
        print(json.dumps(enriched["pull_request"], indent=2, ensure_ascii=False))

    return 0 if total == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
