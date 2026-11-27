"""Evidence-bound review attestation.

A review produces a *content-addressed artifact* (the reviewer's structured verdict)
and a set of git *trailers* on the reviewed commit that reference it by hash. The
Evidence Pack can then tell an **integrity-checked** review record (structured trailer +
resolvable artifact that covers the change) from a **claimed** one (free-text prose, or a
trailer that does not resolve), an explicit **override** (the gate's bypass variable), or
**none**.

*Integrity-checked* means nothing stronger: the artifact resolves, hashes to the ref the
trailer names, and names the committed content. It does NOT mean an independent reviewer
produced it, and it is never called *verified* (decision log 2026-08-16; manifest schema
``governor.evidence-pack/2`` renamed the state value accordingly) — do not read it as the
vision §6 "makes evidence true" property, which is unbuilt and owner-open
(``trust-architecture.md`` §11.1, decision T8).

Scope, stated honestly: the trailer records that a review artifact was produced and
binds the commit to it by content hash. It is an audit/discipline record, NOT a
cryptographic authorization — without a signing key the committer does not hold, a
trailer can be authored by the same party it attests. Identity binding and server-side
merge enforcement are deliberately out of scope here (7B / owner-gated).
"""

from __future__ import annotations

from kerbtap.review.trailer import (
    ReviewSignal,
    classify_review,
    format_trailers,
    parse_trailers,
)

__all__ = [
    "ReviewSignal",
    "classify_review",
    "format_trailers",
    "parse_trailers",
]
