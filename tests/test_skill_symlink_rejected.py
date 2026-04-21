"""Symlinks (abs/rel) inside a skill bundle → validation rejects."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _base_skill(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: x\n---\n",
        encoding="utf-8",
    )


def test_relative_symlink_rejected(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    b = tmp_path / "b"
    _base_skill(b)
    (b / "tgt.txt").write_text("data", encoding="utf-8")
    os.symlink("tgt.txt", b / "link.txt")
    with pytest.raises(core.ValidationError, match="symlink"):
        core.validate_bundle(b)


def test_absolute_symlink_rejected(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    b = tmp_path / "b"
    _base_skill(b)
    os.symlink("/etc/passwd", b / "leak")
    with pytest.raises(core.ValidationError, match="symlink"):
        core.validate_bundle(b)


def test_nested_symlink_rejected(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    b = tmp_path / "b"
    _base_skill(b)
    sub = b / "sub"
    sub.mkdir()
    (sub / "real.txt").write_text("r", encoding="utf-8")
    os.symlink("real.txt", sub / "alias.txt")
    with pytest.raises(core.ValidationError, match="symlink"):
        core.validate_bundle(b)
