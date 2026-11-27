"""Text helpers that guarantee excerpts never end mid-sentence.

Truncated evidence ("...deliverables were.") destroys reviewer confidence, so every
excerpt this tool emits is cut only at a real sentence boundary — or not at all.
"""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def normalize_ws(text: str) -> str:
    """Collapse all runs of whitespace (including newlines) to single spaces."""
    return " ".join(text.split())


def first_sentence(text: str) -> str:
    """Return the first complete sentence of ``text`` (whitespace-normalized).

    If no sentence boundary is found, the whole normalized string is returned. The result
    always ends on real punctuation or is the complete input — never a mid-sentence cut.
    """
    normalized = normalize_ws(text)
    match = _SENTENCE_END.search(normalized)
    if match is None:
        return normalized
    return normalized[: match.start() + 1]


def clean_excerpt(text: str, max_chars: int = 600) -> str:
    """Return a bounded excerpt of ``text`` that always ends on a sentence boundary.

    The excerpt is whitespace-normalized, at most ``max_chars`` long, and cut at the last
    sentence boundary that fits. If the first sentence alone already exceeds ``max_chars``,
    that full first sentence is returned rather than a mid-sentence fragment.
    """
    normalized = normalize_ws(text)
    if len(normalized) <= max_chars:
        return normalized

    window = normalized[:max_chars]
    boundaries = [m.start() for m in _SENTENCE_END.finditer(window)]
    if boundaries:
        return normalized[: boundaries[-1] + 1]
    # No boundary within the window: fall back to the full first sentence.
    return first_sentence(normalized)


def ends_cleanly(text: str) -> bool:
    """Return whether ``text`` ends on sentence-final punctuation (not a bare fragment)."""
    stripped = text.rstrip()
    return bool(stripped) and stripped[-1] in ".!?"
