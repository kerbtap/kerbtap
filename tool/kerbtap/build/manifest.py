"""Component 2 — manifest.json: the structured change-attestation record (spec §4.2)."""

from __future__ import annotations

import json
from dataclasses import asdict

from kerbtap import __version__
from kerbtap.assemble import not_attested_list
from kerbtap.honesty import HONEST_CLAIM, INTEGRITY_DISCLAIMER
from kerbtap.model import EvidenceBundle


def render(bundle: EvidenceBundle) -> str:
    """Render the manifest as a JSON string."""
    scope = bundle.scope
    scope_dict: dict[str, object] = {
        "repository": scope.repository,
        "commit_range": scope.commit_range,
    }
    if bundle.internal:
        # Absolute path is PII/local-leak; internal/dogfood mode only (§5).
        scope_dict["repo_path"] = scope.repo_path
    manifest = {
        "schema_version": "governor.evidence-pack/2",
        "compatibility": (
            "Consumers may rely on these top-level keys and the changes[] record shape "
            "across minor versions of schema major 2; fields may be added but existing "
            "field meanings will not change. A major-version bump signals a breaking change."
        ),
        "claim": HONEST_CLAIM,
        "integrity_disclaimer": INTEGRITY_DISCLAIMER,
        "generated_ts": scope.as_of,
        "generator": {"tool": "governor", "version": __version__, "phase": "7A"},
        "external_safe": not bundle.internal,
        "scope": {
            **scope_dict,
            "as_of": scope.as_of,
            "assessed_commits": scope.assessed_commits,
            "assessed_prs": scope.assessed_prs,
            "landing_breakdown": {
                "merge_pr": scope.merge_pr_count,
                "squash_pr": scope.squash_pr_count,
                "pr_branch_commit": scope.pr_branch_commit_count,
                "direct_to_main": scope.direct_to_main_count,
            },
        },
        "evidence_tier": {
            "tier": "Tier 0 — Proof-Only (pointer-hash-only)",
            "note": (
                "7A default retention tier: only pointers, hashes, and metadata leave "
                "the repository; content stays in the customer's own systems."
            ),
        },
        "not_attested": not_attested_list(),
        "repo_level_attestation": bundle.repo_level_attestation,
        "merge_gate": {
            **bundle.branch_protection,
            "scope_note": (
                "Current configuration read at collection time; a repo-level control "
                "record, NOT per-change evidence that historical changes were gated."
            ),
        },
        "codeowners": bundle.codeowners,
        "enforcement_layer": bundle.enforcement_layer,
        "source_systems": [asdict(s) for s in bundle.source_systems],
        "changes": [_change_dict(c) for c in bundle.changes],
    }
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def _change_dict(change) -> dict:
    data = asdict(change)
    # Present review/custody/gates as plain nested dicts (asdict already flattens).
    return data
