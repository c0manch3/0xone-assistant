"""Path traversal / symlink / size-limit validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_validate_bundle_rejects_symlink(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: x\n---\n",
        encoding="utf-8",
    )
    # Create a symlink inside the bundle (relative).
    (bundle / "target.txt").write_text("data", encoding="utf-8")
    os.symlink("target.txt", bundle / "link.txt")

    with pytest.raises(core.ValidationError, match="symlink not allowed"):
        core.validate_bundle(bundle)


def test_validate_bundle_rejects_absolute_symlink(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: x\n---\n",
        encoding="utf-8",
    )
    os.symlink("/etc/passwd", bundle / "leak")
    with pytest.raises(core.ValidationError):
        core.validate_bundle(bundle)


def test_validate_bundle_name_regex(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text(
        "---\nname: Bad Name!\ndescription: x\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(core.ValidationError, match="invalid skill name"):
        core.validate_bundle(bundle)


def test_validate_bundle_size_limits(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: x\n---\n",
        encoding="utf-8",
    )
    # Write a file larger than MAX_SINGLE_BYTES.
    big = bundle / "big.bin"
    big.write_bytes(b"\x00" * (core.MAX_SINGLE_BYTES + 1))
    with pytest.raises(core.ValidationError, match="file too large"):
        core.validate_bundle(bundle)


def test_validate_bundle_too_many_files(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: x\n---\n",
        encoding="utf-8",
    )
    # MAX_FILES already includes SKILL.md itself
    for i in range(core.MAX_FILES + 5):
        (bundle / f"f{i}.txt").write_text("x", encoding="utf-8")
    with pytest.raises(core.ValidationError, match="too many files"):
        core.validate_bundle(bundle)


def test_validate_bundle_missing_skill_md(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "README.md").write_text("just a readme", encoding="utf-8")
    with pytest.raises(core.ValidationError, match=r"SKILL\.md missing"):
        core.validate_bundle(bundle)


def test_validate_bundle_missing_description(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text(
        "---\nname: alpha\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(core.ValidationError, match="description is required"):
        core.validate_bundle(bundle)


def test_validate_bundle_bad_yaml(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: [unclosed\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(core.ValidationError, match="frontmatter YAML parse failed"):
        core.validate_bundle(bundle)


def test_validate_bundle_py_syntax_error(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: x\n---\n",
        encoding="utf-8",
    )
    (bundle / "broken.py").write_text("def f(:\n", encoding="utf-8")
    with pytest.raises(core.ValidationError, match="py syntax error"):
        core.validate_bundle(bundle)
