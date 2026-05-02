"""Phase 9 §2.8 + R3.13 — pipe-table parser tests.

Researcher recommended ≥10 cases; this file enumerates 12 covering
the spec §3 Wave B B1 list verbatim.
"""

from __future__ import annotations

import pytest

from assistant.render_doc.markdown_tables import (
    MarkdownTableError,
    parse,
)


def test_happy_path_single_table() -> None:
    md = (
        "| col A | col B | col C |\n"
        "|-------|-------|-------|\n"
        "| val 1 | val 2 | val 3 |\n"
        "| val 4 | val 5 | val 6 |\n"
    )
    tables = parse(md)
    assert len(tables) == 1
    t = tables[0]
    assert t.header == ["col A", "col B", "col C"]
    assert len(t.rows) == 2
    assert t.rows[0] == ["val 1", "val 2", "val 3"]


def test_empty_content_returns_no_tables() -> None:
    assert parse("") == []
    assert parse("   \n\n  \n") == []


def test_no_table_syntax_returns_no_tables() -> None:
    md = "# Just a heading\n\nSome prose here.\n"
    assert parse(md) == []


def test_multi_table_yields_multiple_tables() -> None:
    md = (
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "| c | d |\n|---|---|\n| 3 | 4 |\n"
    )
    tables = parse(md)
    assert len(tables) == 2
    assert tables[0].header == ["a", "b"]
    assert tables[1].header == ["c", "d"]


def test_header_without_separator_not_a_table() -> None:
    """Header-shaped line without a separator row underneath is
    treated as prose; no table emitted."""
    md = "| a | b |\nplain text not separator\n"
    assert parse(md) == []


def test_mismatched_column_count_raises() -> None:
    md = (
        "| a | b | c |\n"
        "|---|---|---|\n"
        "| 1 | 2 |\n"
    )
    with pytest.raises(MarkdownTableError) as ei:
        parse(md)
    assert ei.value.code == "markdown-malformed"


def test_escaped_pipe_preserved_inside_cell() -> None:
    md = (
        "| col |\n"
        "|-----|\n"
        r"| has \| pipe |"
        + "\n"
    )
    tables = parse(md)
    assert len(tables) == 1
    assert tables[0].rows[0] == ["has | pipe"]


def test_leading_trailing_whitespace_stripped() -> None:
    md = (
        "|   col A   |   col B   |\n"
        "|-----------|-----------|\n"
        "|   foo     |   bar     |\n"
    )
    tables = parse(md)
    assert tables[0].header == ["col A", "col B"]
    assert tables[0].rows[0] == ["foo", "bar"]


def test_alignment_markers_accepted() -> None:
    md = (
        "| L | R | C |\n"
        "|:----|----:|:----:|\n"
        "| 1 | 2 | 3 |\n"
    )
    tables = parse(md)
    assert tables[0].alignments == ["left", "right", "center"]


def test_separator_with_no_alignment_marker() -> None:
    md = (
        "| a | b |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
    )
    tables = parse(md)
    assert tables[0].alignments == ["none", "none"]


def test_cyrillic_content_accepted() -> None:
    md = (
        "| Имя | Возраст |\n"
        "|-----|---------|\n"
        "| Виталий | 35 |\n"
    )
    tables = parse(md)
    assert tables[0].header == ["Имя", "Возраст"]
    assert tables[0].rows[0] == ["Виталий", "35"]


def test_empty_cells_accepted() -> None:
    md = (
        "| a | b | c |\n"
        "|---|---|---|\n"
        "|   |   |   |\n"
    )
    tables = parse(md)
    assert tables[0].rows[0] == ["", "", ""]
