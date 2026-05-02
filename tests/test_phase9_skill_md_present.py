"""Phase 9 §3.7 + AC#30 — ``skills/render_doc/SKILL.md`` presence +
frontmatter validation.

Mirrors phase-8 ``test_phase8_skill_md_present.py`` shape (if exists).
The SDK auto-discovers SKILL.md via ``setting_sources=["project"]``;
this test ensures the file is present + parses + carries the required
metadata + Cyrillic triggers.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "render_doc"
    / "SKILL.md"
)


def _parse_frontmatter(text: str) -> dict[str, object]:
    m = re.match(
        r"^---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL
    )
    assert m is not None, "SKILL.md missing YAML frontmatter delimited by ---"
    parsed = yaml.safe_load(m.group(1))
    assert isinstance(parsed, dict)
    return parsed


def test_skill_md_exists() -> None:
    assert SKILL_PATH.is_file(), (
        f"skills/render_doc/SKILL.md missing at {SKILL_PATH}"
    )


def test_frontmatter_has_required_keys() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    fm = _parse_frontmatter(text)
    assert fm.get("name") == "render_doc"
    desc = fm.get("description")
    assert isinstance(desc, str) and len(desc) > 0
    # Description must mention all three formats.
    for fmt in ("PDF", "DOCX", "XLSX"):
        assert fmt in desc, f"description missing {fmt!r}: {desc!r}"
    allowed = fm.get("allowed-tools")
    assert allowed == ["mcp__render_doc__render_doc"], (
        f"allowed-tools must list the single render_doc @tool; "
        f"got {allowed!r}"
    )


def test_body_contains_cyrillic_triggers() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    triggers = (
        "сделай PDF",
        "сгенерь docx",
        "дай excel таблиц",
        "сделай отчёт",
    )
    matches = [t for t in triggers if t in text]
    assert matches, (
        f"SKILL.md must mention at least one Cyrillic trigger; "
        f"checked {triggers!r}, none found"
    )


def test_body_references_mcp_tool_name() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "mcp__render_doc__render_doc" in text


def test_body_has_anti_pattern_section() -> None:
    """AC#30: must include a ``Не вызывай`` (anti-pattern) section."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "Не вызывай" in text
    # Anti-pattern section warns against memory_* and WebFetch reuses.
    assert "memory" in text.lower()
    assert "WebFetch" in text
