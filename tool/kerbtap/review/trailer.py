"""Parse, format, and classify kerbtap review trailers — pure, dependency-free.

This module is imported by both the collector (``collect.git.detect_review``, which runs
against *arbitrary* repos and only ever sees a commit body) and the ``review`` CLI. It
must therefore stay pure: no filesystem, no git, no repo-specific assumptions. Artifact
resolution (does the referenced ``<state folder>/reviews/<hash>.json`` actually exist and
hash-match?) is a separate, repo-aware step in :mod:`kerbtap.review.record`.

Trailer shape (git-trailer compatible — ``Key: value`` lines in the commit body)::

    Governor-Review: PASS
    Governor-Reviewer: architect
    Governor-Review-Ref: sha256:<64 hex>

A body classifies as **integrity-checked** only when a ``PASS`` verdict trailer is paired
with a well-formed review-artifact reference (the repo-aware step then confirms the
artifact). A free-text "adversarial review … PASS" note in the body is explicitly *not* —
it is the spoofable pattern this replaces.

The word marks an *integrity* property, not a *provenance* one: the trailer and its
artifact agree, and neither has drifted since the commit. The same party can author
both, so this state must never be reported as independent review. See
``kerbtap.review`` for the scope, and ``trust-architecture.md`` §11.1 / T8 for the
binding that would earn the stronger reading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --- trailer grammar ----------------------------------------------------------------

VERDICT_KEY = "Governor-Review"
REVIEWER_KEY = "Governor-Reviewer"
REF_KEY = "Governor-Review-Ref"

VALID_VERDICTS = ("PASS", "NEEDS-CHANGES")

# sha256:<64 lowercase hex>. The only reference form an integrity-checked trailer may carry.
_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# One of our recognized trailer keys: "Key: value" at line start.
_TRAILER_LINE_RE = re.compile(
    r"^(?P<key>Governor-Review|Governor-Reviewer|Governor-Review-Ref):[ \t]*(?P<value>.*?)[ \t]*$"
)
# A generic git-trailer line ("Token: value" / "Token #value"), used only to decide
# whether the message's final paragraph *is* a trailer block. Matching git's own rule that
# a trailer block's lines all look like trailers keeps a `Governor-Review` line quoted in
# the middle of a prose body from being read as an attestation.
_GENERIC_TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*(?::[ \t]|[ \t]#)")

# Legacy free-text pattern — a review is *claimed* in prose but not structurally bound.
_PROSE_PHRASE_RE = re.compile(r"adversarial review", re.IGNORECASE)
_PROSE_PASS_RE = re.compile(r"\bPASS\b")
# The gate's bypass variable. Installs rename it per repository (``<PREFIX>_REVIEWED``), so
# the collector passes the name read from each commit's own hook; this is the default.
DEFAULT_OVERRIDE_NAMES = ("GOVERNOR_REVIEWED",)
_OVERRIDE_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Verification states, strongest first.
INTEGRITY_CHECKED = "integrity-checked"
CLAIMED = "claimed"
OVERRIDE = "override"
NONE = "none"

# Reason codes — a machine label for *why* a signal landed in its state. Deliberately
# separate from any repo-sourced value: the human ``evidence`` string is built only from a
# reason and safe fields (a validated ``sha256:`` ref), never from untrusted trailer text,
# so no commit-authored string can ever leak into the pack through this path.
REASON_INTEGRITY_CHECKED = "integrity-checked"
REASON_REF_INVALID = "trailer-ref-invalid"
REASON_NOT_PASS = "trailer-verdict-not-pass"
REASON_UNRECOGNIZED = "trailer-verdict-unrecognized"
REASON_PROSE = "prose-claim"
REASON_OVERRIDE = "override"
REASON_NONE = "none"
REASON_ARTIFACT_UNVERIFIED = "artifact-unverified"  # set by the repo-aware pack resolver
REASON_SUBJECT_MISMATCH = "artifact-subject-mismatch"  # set by the repo-aware pack resolver

# Whether an integrity-checked record's artifact states which files it covered (repo-aware step).
SCOPE_CONFIRMED = "confirmed"  # the commit's guarded files are covered at the committed content
SCOPE_NOT_RECORDED = "not-recorded"  # the artifact names no reviewed files

# Whether an integrity-checked record's artifact was made under the current review contract.
CONTRACT_CURRENT = "current"
CONTRACT_DIFFERS = "differs"
CONTRACT_NOT_RECORDED = "not-recorded"
CONTRACT_UNREADABLE = "current contract unreadable"  # the current contract could not be read to compare


@dataclass(frozen=True, slots=True)
class ReviewSignal:
    """The review signal derived from a commit body (pure, body-only).

    ``attested`` is true ONLY for :data:`INTEGRITY_CHECKED` — a structured trailer with a PASS
    verdict and a well-formed artifact reference. Everything weaker (prose claims,
    overrides) is recorded honestly but never counted as attested.

    Field trust contract (this is the leak-safety invariant):

    * ``verdict`` is a *recognized* verdict (``PASS`` / ``NEEDS-CHANGES``) or ``None`` —
      an unrecognized commit-authored value is never stored.
    * ``ref`` is a validated ``sha256:<64hex>`` (safe to print) or ``None``.
    * ``reviewer`` is the ONE raw, commit-authored field; the pack collector redacts it
      exactly once at its boundary.
    * ``evidence`` is derived from ``reason`` plus at most a validated ``ref`` — it never
      contains a raw trailer value, so it is safe to embed verbatim in the pack.
    """

    verification: str  # INTEGRITY_CHECKED | CLAIMED | OVERRIDE | NONE
    attested: bool
    verdict: str | None
    reviewer: str | None
    ref: str | None
    reason: str
    evidence: str
    # Set only by the repo-aware resolver for an integrity-checked signal: SCOPE_CONFIRMED or
    # SCOPE_NOT_RECORDED. A body-only classification never knows it.
    scope: str | None = None
    # Set with ``scope``: the review-contract hash the artifact recorded ("" when none) and
    # its CONTRACT_* state against the current contract.
    contract: str = ""
    contract_state: str = ""


def _trailer_block(body: str) -> list[str]:
    """Return the lines of the message's trailing trailer block, or ``[]``.

    Mirrors git's trailer semantics closely enough for classification: only the LAST
    paragraph (the run of non-blank lines after the final blank line) is a trailer block,
    and only when every one of its non-blank lines looks like a trailer. A
    ``Governor-Review`` line sitting in a prose paragraph, a quoted old message, or a code
    block is therefore never read as an attestation.
    """
    lines = (body or "").splitlines()
    # Drop trailing blank lines.
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return []
    # Walk up to the blank line that precedes the final paragraph.
    start = len(lines)
    saw_blank = False
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].strip():
            saw_blank = True
            break
        start = i
    # A trailer footer must be a *separate* trailing paragraph — git parses no trailers
    # from a message with no blank line before the block (e.g. subject + trailer glued
    # together). Require the separator, so `feat: x\nGovernor-Review: PASS` is not a footer.
    if not saw_blank or start == 0:
        return []
    block = lines[start:]
    # The first line must be a trailer; later lines may be trailers or git-style
    # continuation lines (leading whitespace) that extend the preceding trailer. Anything
    # else means the final paragraph is prose, not a trailer footer.
    for idx, line in enumerate(block):
        if _GENERIC_TRAILER_RE.match(line):
            continue
        if idx > 0 and (line.startswith(" ") or line.startswith("\t")):
            continue
        return []
    return block


def parse_trailers(body: str) -> dict[str, str]:
    """Return the last value for each recognized kerbtap trailer key in the footer block.

    Only the trailing trailer block is considered (see :func:`_trailer_block`); if a key
    repeats there, the last occurrence wins. Git *folds* continuation lines (those starting
    with whitespace) into the preceding trailer's value, so we do the same — otherwise a
    footer like ``Governor-Review: PASS`` + ``  NEEDS-CHANGES`` would be read as an exact
    ``PASS`` here while git sees ``PASS NEEDS-CHANGES``. Folding makes the strict keys
    (exact verdict / ``sha256:<64hex>`` ref) fail closed on any continuation, matching git.
    """
    logical: list[list[str | None]] = []
    for line in _trailer_block(body):
        if line[:1].isspace():
            # A continuation of the previous logical trailer (if it was a kerbtap key).
            if logical and logical[-1][0] is not None:
                logical[-1][1] = f"{logical[-1][1]} {line.strip()}"
            continue
        match = _TRAILER_LINE_RE.match(line)
        if match:
            logical.append([match.group("key"), match.group("value")])
        else:
            # A non-kerbtap trailer line — tracked as a placeholder so its own
            # continuations fold onto it, not onto a preceding kerbtap trailer.
            logical.append([None, None])

    found: dict[str, str] = {}
    for key, value in logical:
        if key is not None and value is not None:
            found[key] = value
    return found


def is_valid_ref(ref: str | None) -> bool:
    """Whether ``ref`` is a well-formed ``sha256:<64 hex>`` artifact reference."""
    return bool(ref) and bool(_REF_RE.match(ref))  # type: ignore[arg-type]


def format_trailers(verdict: str, reviewer: str, ref: str) -> str:
    """Render the three-line trailer block for a recorded review.

    Raises:
        ValueError: on an unknown verdict or a malformed reference — a record command
            must never emit a trailer that its own classifier would reject.
    """
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {VALID_VERDICTS}, got {verdict!r}")
    if not is_valid_ref(ref):
        raise ValueError(f"ref must be 'sha256:<64 hex>', got {ref!r}")
    reviewer = reviewer.strip() or "unnamed"
    return f"{VERDICT_KEY}: {verdict}\n{REVIEWER_KEY}: {reviewer}\n{REF_KEY}: {ref}"


def _mentions_override(body: str, names: tuple[str, ...]) -> bool:
    valid = [n for n in names if _OVERRIDE_NAME_RE.match(n)]
    if not valid:
        return False
    pattern = r"\b(?:" + "|".join(re.escape(n) for n in valid) + r")\b"
    return re.search(pattern, body) is not None


def classify_review(body: str, override_names: tuple[str, ...] = DEFAULT_OVERRIDE_NAMES) -> ReviewSignal:
    """Classify the review signal in a commit body. Pure; never raises.

    Precedence: a structured trailer (in any shape) is classified first, then a legacy
    prose claim, then a bare override, then nothing. Only an exact ``PASS`` verdict paired
    with a validated ``sha256`` ref is ``integrity-checked``. Per the field-trust contract, an
    unrecognized verdict value is dropped (``verdict=None``) rather than stored, and a
    malformed ref is dropped (``ref=None``) — so no raw trailer value survives except the
    ``reviewer`` field, which the collector redacts.

    ``override_names`` are the bypass-variable names that count as an explicit override;
    names that are not plain upper-case identifiers are ignored.
    """
    trailers = parse_trailers(body)
    raw_verdict = trailers.get(VERDICT_KEY)
    reviewer = trailers.get(REVIEWER_KEY)  # the sole raw field; redacted at the pack boundary
    ref = trailers.get(REF_KEY)
    ref = ref if is_valid_ref(ref) else None

    if raw_verdict is not None:
        # A structured Governor-Review trailer is present — classify by its exactness.
        if raw_verdict == "PASS" and ref is not None:
            return ReviewSignal(
                INTEGRITY_CHECKED, True, "PASS", reviewer, ref, REASON_INTEGRITY_CHECKED,
                f"Structured review trailer bound to review artifact {ref}; "
                f"re-checkable with `kerbtap review verify`.",
            )
        if raw_verdict == "PASS":  # ref missing or malformed
            return ReviewSignal(
                CLAIMED, False, "PASS", reviewer, None, REASON_REF_INVALID,
                "A PASS review trailer is present but its review-artifact reference is "
                "missing or malformed; not counted as attested.",
            )
        if raw_verdict == "NEEDS-CHANGES":
            return ReviewSignal(
                CLAIMED, False, "NEEDS-CHANGES", reviewer, ref, REASON_NOT_PASS,
                "The review trailer records a non-passing verdict; not an attestation.",
            )
        # Unrecognized verdict value — never stored raw (it could be hostile free text).
        return ReviewSignal(
            CLAIMED, False, None, reviewer, ref, REASON_UNRECOGNIZED,
            "The review trailer carries an unrecognized verdict; not counted as attested.",
        )

    # Legacy free-text claim: the exact spoofable pattern the trailer replaces.
    if _PROSE_PHRASE_RE.search(body or "") and _PROSE_PASS_RE.search(body or ""):
        return ReviewSignal(
            CLAIMED, False, None, None, None, REASON_PROSE,
            "Free-text review claim in the commit body; no structured trailer bound to an "
            "artifact — not counted as attested.",
        )

    if _mentions_override(body or "", override_names):
        return ReviewSignal(
            OVERRIDE, False, None, None, None, REASON_OVERRIDE,
            "Explicit review-gate override (the gate's bypass variable) with no recorded review artifact.",
        )

    return ReviewSignal(
        NONE, False, None, None, None, REASON_NONE,
        "No recorded review evidence in the commit body.",
    )
