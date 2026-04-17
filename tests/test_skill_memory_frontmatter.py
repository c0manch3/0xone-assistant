"""Phase 4: `skills/memory/SKILL.md` declares `[Bash, Read]`."""

from __future__ import annotations

from pathlib import Path

from assistant.bridge.skills import parse_skill

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_memory_skill_metadata_shape() -> None:
    meta = parse_skill(_PROJECT_ROOT / "skills" / "memory" / "SKILL.md")
    assert meta["name"] == "memory"
    assert meta["allowed_tools"] == ["Bash", "Read"]
    assert "память" in meta["description"].lower() or "memory" in meta["description"].lower()
