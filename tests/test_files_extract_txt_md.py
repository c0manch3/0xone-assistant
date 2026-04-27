"""Phase 6a — TXT / MD extractor unit tests.

Covers:
- BOM stripping (devil L2);
- malformed bytes → ``\\ufffd`` substitute, no exception;
- MD aliases TXT (byte-for-byte identical behaviour);
- missing file → ExtractionError.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.files.extract import (
    ExtractionError,
    extract_md,
    extract_txt,
)


def test_extract_txt_simple_utf8(tmp_path: Path) -> None:
    p = tmp_path / "hello.txt"
    p.write_text("hello world\nline two", encoding="utf-8")
    text, n = extract_txt(p)
    assert text == "hello world\nline two"
    assert n == len(text)


def test_extract_txt_strips_bom(tmp_path: Path) -> None:
    """Devil L2: ``utf-8-sig`` strips a leading UTF-8 BOM. Plain
    ``utf-8`` would leave ``\\ufeff`` as a literal first char."""
    p = tmp_path / "bom.txt"
    # Write BOM + content.
    p.write_bytes(b"\xef\xbb\xbfhello")
    text, _ = extract_txt(p)
    assert text == "hello"
    assert "\ufeff" not in text


def test_extract_txt_replaces_malformed_bytes(tmp_path: Path) -> None:
    """``errors="replace"`` substitutes ``\\ufffd`` for invalid bytes."""
    p = tmp_path / "broken.txt"
    p.write_bytes(b"valid \xff\xfe broken")
    text, _ = extract_txt(p)
    # The replacement char must appear; no exception must be raised.
    assert "\ufffd" in text
    assert "valid" in text
    assert "broken" in text


def test_extract_txt_cyrillic_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "ru.txt"
    payload = "Привет, мир — это тест.\nВторая строка."
    p.write_text(payload, encoding="utf-8")
    text, _ = extract_txt(p)
    assert text == payload


def test_extract_txt_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.txt"
    p.write_text("", encoding="utf-8")
    text, n = extract_txt(p)
    assert text == ""
    assert n == 0


def test_extract_md_is_alias_of_txt(tmp_path: Path) -> None:
    """``extract_md`` is byte-for-byte identical to ``extract_txt``."""
    assert extract_md is extract_txt
    p = tmp_path / "doc.md"
    payload = "# Header\n\nSome **bold** text."
    p.write_text(payload, encoding="utf-8")
    text, _ = extract_md(p)
    assert text == payload


def test_extract_txt_missing_file_raises(tmp_path: Path) -> None:
    p = tmp_path / "ghost.txt"
    with pytest.raises(ExtractionError) as excinfo:
        extract_txt(p)
    assert "read failed" in str(excinfo.value).lower()
