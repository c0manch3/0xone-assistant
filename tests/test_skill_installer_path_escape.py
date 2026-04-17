"""Validator rejects SKILL.md frontmatter that would produce a malicious
`name` or that contains no SKILL.md at all."""

from __future__ import annotations

from pathlib import Path

import pytest

from _lib.validate import ValidationError, validate_bundle


def test_missing_skill_md_rejected(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("no frontmatter here", encoding="utf-8")
    with pytest.raises(ValidationError, match=r"missing SKILL\.md"):
        validate_bundle(tmp_path)


def test_missing_name_rejected(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("---\ndescription: no name\n---\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="name"):
        validate_bundle(tmp_path)


def test_missing_description_rejected(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("---\nname: foo\n---\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="description"):
        validate_bundle(tmp_path)


def test_invalid_name_character_rejected(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text(
        "---\nname: ../etc/passwd\ndescription: bad\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="name"):
        validate_bundle(tmp_path)


def test_uppercase_name_rejected(tmp_path: Path) -> None:
    # Defence against case-preserving filesystems (APFS/NTFS default) +
    # future tooling that expects slug-style names.
    (tmp_path / "SKILL.md").write_text(
        "---\nname: FooBar\ndescription: uppercase\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="name"):
        validate_bundle(tmp_path)


def test_unknown_allowed_tool_rejected(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text(
        "---\nname: foo\ndescription: ok\nallowed-tools: [Bash, NukeServers]\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="unknown tool"):
        validate_bundle(tmp_path)


def test_minimal_valid_bundle_accepted(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("---\nname: foo\ndescription: ok\n---\n", encoding="utf-8")
    report = validate_bundle(tmp_path)
    assert report["name"] == "foo"
    assert report["description"] == "ok"
    assert report["allowed_tools"] is None
    assert report["file_count"] == 1
    assert report["has_inner_tools"] is False
