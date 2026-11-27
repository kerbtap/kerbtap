"""Tests for excerpt helpers that guarantee no mid-sentence truncation."""

from __future__ import annotations

from kerbtap.textutil import clean_excerpt, ends_cleanly, first_sentence, normalize_ws


class TestFirstSentence:
    def test_returns_full_first_sentence(self) -> None:
        text = "The self-attestation bypass leaves no evidence artifact. The hook does not record."
        assert first_sentence(text) == "The self-attestation bypass leaves no evidence artifact."

    def test_reconstructs_across_wrapped_lines(self) -> None:
        wrapped = "The whole program ran three sprints without ever\nstarting one; deliverables were\nmarked Done. Next sentence."
        assert first_sentence(wrapped) == (
            "The whole program ran three sprints without ever starting one; deliverables were marked Done."
        )

    def test_no_boundary_returns_whole(self) -> None:
        assert first_sentence("no terminal punctuation here") == "no terminal punctuation here"

    def test_never_ends_on_a_dangling_fragment(self) -> None:
        for text in (
            "The harness blocked self-merging PRs, and the cadence carries no signal. More.",
            "Closed by a verbal grant, not by the attestation. Then more text.",
        ):
            assert ends_cleanly(first_sentence(text))


class TestCleanExcerpt:
    def test_short_text_unchanged(self) -> None:
        assert clean_excerpt("Short. Done.", max_chars=100) == "Short. Done."

    def test_bounded_and_ends_on_boundary(self) -> None:
        text = "One sentence here. Two sentence here. Three sentence here. Four sentence here."
        excerpt = clean_excerpt(text, max_chars=40)
        assert len(excerpt) <= 40
        assert ends_cleanly(excerpt)
        assert not excerpt.endswith("sentence")  # no mid-sentence cut

    def test_falls_back_to_first_sentence_when_first_exceeds_max(self) -> None:
        text = "This first sentence is definitely longer than the tiny cap allows. Short."
        excerpt = clean_excerpt(text, max_chars=10)
        assert excerpt == "This first sentence is definitely longer than the tiny cap allows."

    def test_normalize_ws(self) -> None:
        assert normalize_ws("a\n b\t c") == "a b c"
