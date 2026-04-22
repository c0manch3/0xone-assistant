"""FTS5 query builder tests — Russian morphology + mixed Latin/Cyrillic."""

from __future__ import annotations

import pytest

from assistant.tools_sdk._memory_core import _build_fts_query


def test_build_fts_query_russian_noun_inflections() -> None:
    """All Russian noun forms collapse to a prefix-wildcard on the stem."""
    q = _build_fts_query("жены")
    # PyStemmer yields ``жен`` (length 3 passes the H2.1 gate).
    assert q.endswith("*")
    assert "жен" in q


def test_build_fts_query_mixed_latin_cyrillic() -> None:
    """Latin tokens are phrase-quoted; Cyrillic tokens stem+wildcard."""
    q = _build_fts_query("flowgent проект")
    assert '"flowgent"' in q
    # ``проект`` stems to ``проект`` — wildcard appended.
    assert "проект*" in q


def test_build_fts_query_punctuation_tolerance() -> None:
    """Punctuation drops out via the ``[\\w]+`` tokenizer; no MATCH errors."""
    q = _build_fts_query("API: почему?")
    # API is ascii; wrapped in quotes.
    assert '"api"' in q
    # ``почему`` stems to ``почем`` (4 chars) — wildcard.
    assert any(part.endswith("*") for part in q.split())


def test_build_fts_query_empty_raises() -> None:
    """Whitespace-only / punctuation-only input has no tokens → raises."""
    with pytest.raises(ValueError):
        _build_fts_query("   !!!   ")


def test_memory_query_short_stem_no_wildcard() -> None:
    """H2.1: stems shorter than 3 chars are dropped entirely.

    ``я`` stems to ``я`` (length 1); a lone ``я*`` wildcard would match
    every word starting with ``я`` (poisoning recall). Drop it.
    """
    # Mixed: "я flowgent" — the ``я`` MUST NOT appear as ``я*``.
    q = _build_fts_query("я flowgent")
    assert "я*" not in q
    assert '"flowgent"' in q


def test_build_fts_query_cyrillic_я_dropped() -> None:
    """Pure-short-stem query raises (nothing searchable after drop)."""
    with pytest.raises(ValueError):
        _build_fts_query("я")


def test_build_fts_query_yo_folding() -> None:
    """``ё`` folds to ``е`` before stemming to match both corpora forms."""
    # ``жёны`` with ё-fold → ``жены`` → stem ``жен`` → ``жен*``.
    q = _build_fts_query("жёны")
    assert "жен*" in q


def test_build_fts_query_rejects_non_string() -> None:
    with pytest.raises(ValueError):
        _build_fts_query(123)  # type: ignore[arg-type]
