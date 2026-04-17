"""B-CRIT-3: skill-installer warns when a SKILL.md declares >3 tools.

Unknown tool names are still a hard reject (pre-existing behaviour);
oversize permitted tool-sets get a warning surfaced in the preview so
the operator gives informed consent before `install --confirm`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _lib import preview as preview_mod
from _lib import validate as validate_mod


def _mk_bundle(tmp_path: Path, frontmatter: str) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\ncontent\n", encoding="utf-8")
    return bundle


def test_oversize_allowed_tools_emits_warning(tmp_path: Path) -> None:
    bundle = _mk_bundle(
        tmp_path,
        "name: bigskill\n"
        "description: uses everything\n"
        "allowed-tools: [Bash, Read, Write, Edit, Grep, Glob, WebFetch]\n",
    )
    report = validate_mod.validate_bundle(bundle)
    warnings = report.get("warnings") or []
    assert len(warnings) == 1
    assert "bigskill" in warnings[0]
    assert "exceeds safe limit" in warnings[0]
    assert "7" in warnings[0]  # the declared count


def test_at_limit_no_warning(tmp_path: Path) -> None:
    bundle = _mk_bundle(
        tmp_path,
        "name: midskill\ndescription: three tools\nallowed-tools: [Bash, Read, Write]\n",
    )
    report = validate_mod.validate_bundle(bundle)
    assert (report.get("warnings") or []) == []


def test_missing_allowed_tools_no_warning(tmp_path: Path) -> None:
    bundle = _mk_bundle(
        tmp_path,
        "name: legacy\ndescription: no declaration",
    )
    report = validate_mod.validate_bundle(bundle)
    assert (report.get("warnings") or []) == []


def test_unknown_tool_still_rejected(tmp_path: Path) -> None:
    bundle = _mk_bundle(
        tmp_path,
        "name: weird\ndescription: x\nallowed-tools: [Bash, Redis]\n",
    )
    with pytest.raises(validate_mod.ValidationError) as exc:
        validate_mod.validate_bundle(bundle)
    assert "Redis" in str(exc.value)


def test_preview_surfaces_warnings(tmp_path: Path) -> None:
    bundle = _mk_bundle(
        tmp_path,
        "name: bigskill\n"
        "description: uses everything\n"
        "allowed-tools: [Bash, Read, Write, Edit, Grep, Glob, WebFetch]\n",
    )
    report = validate_mod.validate_bundle(bundle)
    text = preview_mod.render_preview("https://example.com/x", bundle, "0" * 64, report)
    assert "⚠ warnings:" in text
    assert "bigskill" in text
    assert "exceeds safe limit" in text


def test_preview_without_warnings_omits_section(tmp_path: Path) -> None:
    bundle = _mk_bundle(
        tmp_path,
        "name: midskill\ndescription: x\nallowed-tools: [Bash]\n",
    )
    report = validate_mod.validate_bundle(bundle)
    text = preview_mod.render_preview("https://example.com/x", bundle, "0" * 64, report)
    assert "⚠ warnings:" not in text
