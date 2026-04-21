"""parse_skill emits warnings per allowed-tools shape (phase 3 per-skill gating prep)."""

from __future__ import annotations

from pathlib import Path

from assistant.bridge import skills as skills_mod


def _skill(root: Path, name: str, body: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_allowed_tools_missing_returns_none(tmp_path: Path) -> None:
    """Missing ``allowed-tools`` → permissive default (None sentinel)."""
    p = _skill(
        tmp_path,
        "alpha",
        "---\nname: alpha\ndescription: X\n---\n",
    )
    meta = skills_mod.parse_skill(p)
    assert meta["allowed_tools"] is None


def test_allowed_tools_empty_list_preserved(tmp_path: Path) -> None:
    """Empty list → explicit lockdown (but unenforced in phase 3)."""
    p = _skill(
        tmp_path,
        "beta",
        "---\nname: beta\ndescription: X\nallowed-tools: []\n---\n",
    )
    meta = skills_mod.parse_skill(p)
    assert meta["allowed_tools"] == []


def test_allowed_tools_list_passes_through(tmp_path: Path) -> None:
    p = _skill(
        tmp_path,
        "gamma",
        "---\nname: gamma\ndescription: X\nallowed-tools: [Bash, Read]\n---\n",
    )
    meta = skills_mod.parse_skill(p)
    assert meta["allowed_tools"] == ["Bash", "Read"]


def test_allowed_tools_scalar_wrapped_in_list(tmp_path: Path) -> None:
    p = _skill(
        tmp_path,
        "delta",
        "---\nname: delta\ndescription: X\nallowed-tools: Bash\n---\n",
    )
    meta = skills_mod.parse_skill(p)
    assert meta["allowed_tools"] == ["Bash"]


def test_allowed_tools_malformed_mapping_returns_none(tmp_path: Path) -> None:
    """A mapping (not a list or str) is treated as malformed → permissive."""
    p = _skill(
        tmp_path,
        "epsilon",
        "---\nname: epsilon\ndescription: X\nallowed-tools: {foo: bar}\n---\n",
    )
    meta = skills_mod.parse_skill(p)
    assert meta["allowed_tools"] is None


def test_normalize_allowed_tools_helper_matches_inline() -> None:
    """bridge.skills._normalize_allowed_tools and _installer_core's inline
    copy must produce identical outputs on a shared matrix (NH-style).
    """
    from assistant.bridge.skills import _normalize_allowed_tools
    from assistant.tools_sdk._installer_core import _normalize_allowed_tools_inline

    matrix = [None, "Bash", ["A", "B"], [], {"a": 1}, 42]
    for item in matrix:
        assert _normalize_allowed_tools(item) == _normalize_allowed_tools_inline(item)
