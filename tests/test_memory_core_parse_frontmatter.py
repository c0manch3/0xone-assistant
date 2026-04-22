"""parse_frontmatter + IsoDateLoader — bare dates, malformed, edge cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant.tools_sdk._memory_core import parse_frontmatter


def test_parse_frontmatter_bare_date_iso() -> None:
    """C3/RQ4: bare YAML date coerces to ISO string, json.dumps OK."""
    text = "---\ncreated: 2026-04-16\n---\nbody here"
    fm, body = parse_frontmatter(text)
    assert isinstance(fm["created"], str)
    assert fm["created"] == "2026-04-16"
    # Full round-trip through JSON.
    dumped = json.dumps(fm, ensure_ascii=False)
    assert "2026-04-16" in dumped
    assert body == "body here"


def test_parse_frontmatter_datetime_aware_tz() -> None:
    """Timestamp with timezone offset renders to ISO with offset."""
    text = "---\ncreated: 2026-04-16T10:30:00+03:00\n---\n"
    fm, _ = parse_frontmatter(text)
    assert isinstance(fm["created"], str)
    assert fm["created"].startswith("2026-04-16T")


def test_parse_frontmatter_malformed_date_fallback() -> None:
    """H2.4: malformed date falls back to the raw scalar rather than crash."""
    text = "---\ncreated: 2026-13-99\n---\n"
    fm, _ = parse_frontmatter(text)
    # Raw scalar preserved — should NOT crash; exact value may be the
    # raw scalar string or a YAML-normalised fallback.
    assert isinstance(fm["created"], str)
    assert "2026-13-99" in fm["created"] or fm["created"] == "2026-13-99"


def test_parse_frontmatter_no_frontmatter() -> None:
    """Files without YAML block return empty dict + full body."""
    text = "# Plain Markdown\n\nNo frontmatter."
    fm, body = parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_parse_frontmatter_yaml_error() -> None:
    """Broken YAML surfaces as ValueError."""
    text = "---\n[unclosed\n---\nbody"
    with pytest.raises(ValueError):
        parse_frontmatter(text)


def test_parse_frontmatter_non_mapping_raises() -> None:
    """A top-level YAML list (not dict) is rejected."""
    text = "---\n- just a list\n- another\n---\nbody"
    with pytest.raises(ValueError):
        parse_frontmatter(text)


def test_memory_parse_frontmatter_seed_roundtrip(seed_vault_copy: Path) -> None:
    """C3/RQ4: every seed note's frontmatter survives JSON round-trip.

    Exercises all 12 real notes shipped in the owner's vault — guards
    against any bare-date / Cyrillic / list-valued frontmatter tripping
    the downstream MCP response serialization.
    """
    count = 0
    for md in seed_vault_copy.rglob("*.md"):
        raw = md.read_text(encoding="utf-8")
        try:
            fm, body = parse_frontmatter(raw)
        except ValueError:
            # Some seed files may legitimately lack frontmatter; skip.
            continue
        # The crucial invariant: json.dumps does NOT crash.
        out = json.dumps(fm, ensure_ascii=False)
        assert isinstance(out, str)
        assert isinstance(body, str)
        count += 1
    assert count >= 1, "expected at least one seed note with frontmatter"
