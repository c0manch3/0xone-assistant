"""Memory frontmatter: tags normalization (string / list / null)."""

from __future__ import annotations

import pytest

from _memlib.frontmatter import FrontmatterError, parse_note, serialize_note


def test_tags_string_normalizes_to_list() -> None:
    text = "---\ntitle: T\ntags: foo\n---\nbody\n"
    fm, body = parse_note(text)
    assert fm["tags"] == ["foo"]
    assert body == "body\n"


def test_tags_list_preserved() -> None:
    text = "---\ntitle: T\ntags:\n- a\n- b\n---\nbody\n"
    fm, _ = parse_note(text)
    assert fm["tags"] == ["a", "b"]


def test_tags_null_becomes_empty_list() -> None:
    text = "---\ntitle: T\ntags:\n---\nbody\n"
    fm, _ = parse_note(text)
    assert fm["tags"] == []


def test_serialize_auto_fills_created() -> None:
    out = serialize_note(
        {"title": "T", "tags": [], "area": None, "related": []}, "body\n"
    )
    assert "title: T" in out
    assert "created:" in out


def test_roundtrip_preserves_fields() -> None:
    original = {
        "title": "My note",
        "tags": ["a", "b"],
        "area": "inbox",
        "created": "2026-04-17T12:00:00Z",
        "related": ["other"],
    }
    text = serialize_note(original, "body text\n")
    parsed, body = parse_note(text)
    assert parsed["title"] == "My note"
    assert parsed["tags"] == ["a", "b"]
    assert parsed["area"] == "inbox"
    assert parsed["created"] == "2026-04-17T12:00:00Z"
    assert parsed["related"] == ["other"]
    # serialize_note prepends a blank line between the closing fence and
    # the body for readability; parse_note returns that leading newline
    # verbatim. The actual content round-trips stripped of leading/
    # trailing whitespace.
    assert body.strip() == "body text"


def test_missing_title_raises() -> None:
    text = "---\ntags: [a]\n---\nbody\n"
    with pytest.raises(FrontmatterError) as exc_info:
        parse_note(text)
    assert "title" in str(exc_info.value).lower()


def test_missing_frontmatter_raises() -> None:
    with pytest.raises(FrontmatterError):
        parse_note("no fences here\n")


def test_malformed_yaml_raises() -> None:
    text = "---\ntitle: T\ntags: [unclosed\n---\nbody\n"
    with pytest.raises(FrontmatterError):
        parse_note(text)
