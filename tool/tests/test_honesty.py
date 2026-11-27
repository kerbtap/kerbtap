"""The load-bearing honesty tests.

These prove the single non-negotiable product constraint: a rendered pack never claims
integrity it does not have. Barred terms appear ONLY inside the fixed disclaimer, and the
exact honest claim string IS present on the surfaces that carry a trust claim.
"""

from __future__ import annotations

import json

import pytest

from kerbtap.build import render_pack
from kerbtap.honesty import (
    BARRED_TERMS,
    HONEST_CLAIM,
    INTEGRITY_DISCLAIMER,
    HonestyViolation,
    assert_clean,
)
from tests.factories import make_bundle


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    return render_pack(make_bundle())


def test_all_six_components_render(rendered: dict[str, str]) -> None:
    assert set(rendered) == {
        "report.md",
        "manifest.json",
        "control-map.md",
        "exception-log.md",
        "remediation-plan.md",
        "executive-summary.md",
    }


def test_no_barred_term_appears_outside_the_disclaimer(rendered: dict[str, str]) -> None:
    for filename, text in rendered.items():
        # Removing the fixed disclaimer must leave zero barred terms anywhere.
        scrubbed = text.replace(INTEGRITY_DISCLAIMER, " ").lower()
        for term in BARRED_TERMS:
            assert term not in scrubbed, f"barred term {term!r} leaked into {filename}"


def test_honesty_guard_passes_on_every_component(rendered: dict[str, str]) -> None:
    for filename, text in rendered.items():
        assert_clean(text, context=filename)  # must not raise


def test_honest_claim_present_on_trust_surfaces(rendered: dict[str, str]) -> None:
    for filename in ("manifest.json", "report.md", "executive-summary.md"):
        assert HONEST_CLAIM in rendered[filename], f"missing honest claim in {filename}"


def test_manifest_is_valid_json_with_no_signature_field(rendered: dict[str, str]) -> None:
    manifest = json.loads(rendered["manifest.json"])
    assert manifest["claim"] == HONEST_CLAIM
    # The 7A manifest deliberately carries NO cryptographic signature.
    assert "signature" not in manifest
    for change in manifest["changes"]:
        assert "signature" not in change
        assert change["source_refs"]["commit_sha_role"].startswith("content pointer")


def test_assert_clean_raises_on_a_barred_term() -> None:
    with pytest.raises(HonestyViolation):
        assert_clean("This pack is tamper-proof and audit-grade.", context="unit")


def test_assert_clean_allows_the_disclaimer_verbatim() -> None:
    assert_clean(INTEGRITY_DISCLAIMER, context="disclaimer")  # must not raise
