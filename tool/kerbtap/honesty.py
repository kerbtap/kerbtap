"""The honesty guard for the Evidence Pack.

This module is the single source of truth for what an Evidence Pack is allowed to
claim about its own integrity. It exists because the *worst* failure this tool can
commit — named as such by the product spec (§5.4) — is to overclaim: to describe
manually reviewed, human-packaged evidence as "tamper-proof" or "audit-grade".

The rules, verbatim from the spec:

* The pack makes exactly ONE integrity claim (:data:`HONEST_CLAIM`).
* A fixed set of stronger words (:data:`BARRED_TERMS`) may never appear as a claim
  about the pack. For v0 the simplest-safe rule is enforced: they may appear *only*
  inside the fixed :data:`INTEGRITY_DISCLAIMER`, where every one of them is negated.

:func:`assert_clean` is called on every rendered component before it is written, so a
barred term leaking into any output fails the build loudly rather than shipping.
"""

from __future__ import annotations

import re

# The ONLY integrity claim an Evidence Pack may make (spec §5.4, verbatim).
HONEST_CLAIM = "This Evidence Pack is evidence reviewed and packaged from named systems."

# Terms that must never appear as a claim about the pack (spec §5.4). Matched
# case-insensitively as substrings. They are permitted only inside INTEGRITY_DISCLAIMER,
# where each one is explicitly negated.
BARRED_TERMS: tuple[str, ...] = (
    "tamper-proof",
    "tamper-evident",
    "immutable",
    "audit-grade",
    "notarized",
    "non-repudiable",
    "certified",
)

# The fixed disclaimer block. This is the one and only place a barred term may appear,
# and every occurrence here is negated. It is embedded verbatim (byte-for-byte) into
# every component so that assert_clean can strip it before scanning. Do NOT reflow or
# reword per-file: assert_clean removes this exact string.
INTEGRITY_DISCLAIMER = (
    "Integrity scope — read this before relying on the pack. "
    "This Evidence Pack is evidence reviewed and packaged from named systems. "
    "That is the only integrity claim it makes. "
    "It is NOT cryptographically tamper-proof, NOT tamper-evident, NOT immutable, "
    "NOT audit-grade, NOT notarized, NOT non-repudiable, and NOT certified. "
    "Commit SHAs and content hashes recorded in this pack are content pointers and "
    "fingerprints, used to reference and de-duplicate evidence in the named source "
    "systems; they are not presented as tamper-evidence or as a non-repudiation "
    "guarantee. Actor identity is recorded as reported by the source system (git), "
    "not cryptographically bound to a custodied key. Cryptographic manifest signing, "
    "actor-identity key custody, and an immutable/tamper-evident ledger are 7B "
    "capabilities and are absent by design from this 7A pack."
)


class HonestyViolation(RuntimeError):
    """Raised when generated pack content overclaims the pack's integrity."""


def assert_clean(text: str, *, context: str = "output") -> None:
    """Fail loudly if ``text`` contains a barred integrity term outside the disclaimer.

    Args:
        text: A fully rendered component (markdown or JSON serialized to a string).
        context: A label for the component, used in the raised error message.

    Raises:
        HonestyViolation: If any :data:`BARRED_TERMS` entry survives after the fixed
            :data:`INTEGRITY_DISCLAIMER` is stripped out.
    """
    scrubbed = text.replace(INTEGRITY_DISCLAIMER, " ").lower()
    hits = sorted({term for term in BARRED_TERMS if term in scrubbed})
    if hits:
        raise HonestyViolation(
            f"Barred integrity term(s) {hits} found in {context!r} outside the fixed "
            f"disclaimer. The pack may only claim: {HONEST_CLAIM!r}"
        )


def contains_claim(text: str) -> bool:
    """Return whether the honest claim string is present in ``text``."""
    return HONEST_CLAIM in text


# Placeholder deliberately contains no barred substring so the guard stays satisfied.
_NEUTRALIZE_PLACEHOLDER = "[integrity-term omitted]"
_BARRED_RE = re.compile("|".join(re.escape(term) for term in BARRED_TERMS), re.IGNORECASE)


def neutralize_barred(text: str) -> tuple[str, bool]:
    """Redact barred integrity terms from *quoted external evidence*.

    The pack embeds excerpts of source material (PR bodies, commit review notes) that may
    themselves use words like "immutable" — describing the product's future capabilities,
    not claiming the pack has them. Because the v0 simplest-safe rule bars those words
    anywhere but the disclaimer, they are replaced here with a marker before embedding, and
    the caller notes that the quote was neutralized. This keeps quoted evidence honest
    without letting the pack emit a self-overclaim.

    Returns:
        ``(neutralized_text, changed)``.
    """
    neutralized, count = _BARRED_RE.subn(_NEUTRALIZE_PLACEHOLDER, text)
    return neutralized, count > 0


_PATH_PLACEHOLDER = "[path-redacted]"
# Absolute home-directory paths that may appear in quoted third-party text: POSIX
# /Users/<name>/… and /home/<name>/…, and Windows C:\Users\<name>\… / c:/Users/<name>/….
_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/]Users[\\/][^\s\"'<>)\]]+"          # C:\Users\name\... or c:/Users/name/...
    r"|/(?:Users|home)/[^\s/\"'<>)\]]+(?:/[^\s\"'<>)\]]*)?",  # /Users/name/... or /home/name/...
    re.IGNORECASE,
)


def scrub_paths(text: str) -> tuple[str, bool]:
    """Redact absolute home-directory paths from *quoted external evidence*.

    Quoted PR/commit text can embed a third party's absolute path (e.g. a pasted
    ``…/c:/Users/<name>/…`` deep-link). A customer-facing pack must not carry anyone's
    home-directory path, so those tokens are replaced here before embedding. Applies ONLY
    to quoted third-party excerpts — the repo-under-assessment's own path fields are handled
    separately by the external-safe redaction.

    Returns:
        ``(scrubbed_text, changed)``.
    """
    scrubbed, count = _PATH_RE.subn(_PATH_PLACEHOLDER, text)
    return scrubbed, count > 0


_EMAIL_PLACEHOLDER = "[email-redacted]"
# Any email embedded in quoted repo/external free-form text (a CTA email or a contributor
# address pasted into a PR body). A customer-facing pack must not carry arbitrary third-party
# emails from quoted content; git-author emails are handled separately by the external-safe
# redaction, and no-reply service/trailer addresses reach the pack only via structured fields.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def scrub_emails(text: str) -> tuple[str, bool]:
    """Redact email addresses from *quoted external evidence* (PR/commit bodies, subjects).

    Applies ONLY to quoted third-party excerpts; structured author/trailer emails are handled
    by the external-safe redaction and the trailer classifier.

    Returns:
        ``(scrubbed_text, changed)``.
    """
    scrubbed, count = _EMAIL_RE.subn(_EMAIL_PLACEHOLDER, text)
    return scrubbed, count > 0


def redact_quoted(text: str) -> tuple[str, bool]:
    """Redact quoted repo/external text: neutralize barred terms, scrub absolute paths, and emails.

    Every field that embeds repository- or externally-sourced free-form text (commit
    subjects, PR titles/bodies, branch names, …) must pass through this before entering the
    pack, so only kerbtap's own controlled prose can ever reach the fail-closed guard and
    the guard never trips on a repo's legitimate content.

    Returns:
        ``(redacted_text, changed)``.
    """
    redacted, barred = neutralize_barred(text)
    redacted, path = scrub_paths(redacted)
    redacted, email = scrub_emails(redacted)
    return redacted, (barred or path or email)
