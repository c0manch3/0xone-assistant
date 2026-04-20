from __future__ import annotations

import pytest

from assistant.adapters.telegram import TELEGRAM_MSG_LIMIT, _split_for_telegram


def test_empty_returns_single_empty_chunk() -> None:
    """SW4: the split function is the inverse of ``"".join``; an empty
    string falls into the ``len <= limit`` fast path and returns a single
    empty chunk. The telegram adapter replaces empty output with
    ``"(пустой ответ)"`` BEFORE calling split, so this branch is not
    exercised in production — but pinning the behaviour here prevents
    accidental regressions that could cause an infinite loop."""
    assert _split_for_telegram("") == [""]


def test_short_text_returns_single_chunk() -> None:
    text = "hello"
    assert _split_for_telegram(text) == ["hello"]


def test_text_at_exactly_limit_stays_single_chunk() -> None:
    text = "x" * TELEGRAM_MSG_LIMIT
    parts = _split_for_telegram(text)
    assert parts == [text]
    assert len(parts) == 1


def test_text_just_over_limit_splits_into_two() -> None:
    text = "x" * (TELEGRAM_MSG_LIMIT + 1)
    parts = _split_for_telegram(text)
    assert len(parts) == 2
    # Reassembly must preserve the original content (no byte loss).
    # ``lstrip`` in the split function strips leading whitespace of the
    # remainder — ``x`` is not whitespace, so concatenation is lossless.
    assert "".join(parts) == text
    assert all(len(p) <= TELEGRAM_MSG_LIMIT for p in parts)


def test_text_10000_chars_splits_into_at_most_three() -> None:
    text = "x" * 10_000
    parts = _split_for_telegram(text)
    assert 2 <= len(parts) <= 3
    assert all(len(p) <= TELEGRAM_MSG_LIMIT for p in parts)
    assert "".join(parts) == text


def test_prefers_paragraph_break() -> None:
    """The splitter should prefer ``\\n\\n`` boundaries over hard cuts
    when a paragraph fits within the limit."""
    first = "a" * 3000
    second = "b" * 2000
    text = first + "\n\n" + second
    parts = _split_for_telegram(text)
    assert len(parts) == 2
    assert parts[0] == first
    assert parts[1] == second


def test_prefers_single_newline_when_no_paragraph() -> None:
    """If no ``\\n\\n`` boundary fits, the splitter falls back to
    ``\\n`` — a single newline is still a clean break."""
    first = "a" * 3000
    second = "b" * 2000
    text = first + "\n" + second
    parts = _split_for_telegram(text)
    assert len(parts) == 2
    assert parts[0] == first
    # The newline is the split point; ``lstrip()`` drops the leading
    # newline on the remainder.
    assert parts[1] == second


def test_hard_cut_when_no_newline() -> None:
    """Single line longer than the limit — hard cut at exactly ``limit``."""
    text = "a" * 10_000
    parts = _split_for_telegram(text)
    for p in parts[:-1]:
        assert len(p) == TELEGRAM_MSG_LIMIT
    assert len(parts[-1]) <= TELEGRAM_MSG_LIMIT
    assert "".join(parts) == text


@pytest.mark.parametrize("char", ["ё", "漢", "🎉"])
def test_unicode_counted_as_chars_not_bytes(char: str) -> None:
    """Telegram's 4096 limit is in UTF-16 code units, not bytes, and
    Python counts characters — they happen to line up for the BMP but
    astral characters like ``🎉`` count as 2 UTF-16 units on the wire.
    The split function operates on Python characters (``len(text)``);
    pinning the char-based semantics here documents the contract.
    """
    # Stay conservative: construct a string that is exactly at the char
    # limit and verify it is not split.
    text = char * TELEGRAM_MSG_LIMIT
    parts = _split_for_telegram(text)
    assert parts == [text]
    assert len(parts) == 1


def test_multi_paragraph_content_roundtrip() -> None:
    """Larger multi-paragraph text — reassembly MAY lose whitespace at
    split points (by design: ``lstrip()``), but each non-empty paragraph
    must survive intact."""
    paragraphs = ["para " + "x" * 1500 for _ in range(10)]
    text = "\n\n".join(paragraphs)
    parts = _split_for_telegram(text)
    assert all(len(p) <= TELEGRAM_MSG_LIMIT for p in parts)
    # All paragraphs are still present in the combined output.
    combined = "\n\n".join(parts)
    for para in paragraphs:
        assert para in combined


def test_custom_limit_parameter() -> None:
    """The function accepts a ``limit`` kwarg; scheduler-originated
    messages in phase 5 may use a different cap."""
    text = "abcdefghij"
    parts = _split_for_telegram(text, limit=4)
    assert all(len(p) <= 4 for p in parts)
    assert "".join(parts) == text
