"""Bundle validator rejects ALL symlinks before copytree is reached."""

from __future__ import annotations

from pathlib import Path

import pytest

from _lib.validate import ValidationError, validate_bundle


def _minimal_skill(tmp_path: Path) -> Path:
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("---\nname: valid\ndescription: ok\n---\n", encoding="utf-8")
    return tmp_path


def test_symlink_pointing_outside_bundle_rejected(tmp_path: Path) -> None:
    _minimal_skill(tmp_path)
    (tmp_path / "evil").symlink_to("/etc/passwd")
    with pytest.raises(ValidationError, match="symlink not allowed"):
        validate_bundle(tmp_path)


def test_symlink_pointing_inside_bundle_still_rejected(tmp_path: Path) -> None:
    _minimal_skill(tmp_path)
    # Even a harmless intra-bundle symlink is refused — no carve-outs.
    (tmp_path / "loop").symlink_to("SKILL.md")
    with pytest.raises(ValidationError, match="symlink not allowed"):
        validate_bundle(tmp_path)


def test_symlink_in_subdir_rejected(tmp_path: Path) -> None:
    _minimal_skill(tmp_path)
    sub = tmp_path / "scripts"
    sub.mkdir()
    (sub / "smuggle").symlink_to("../../etc/hosts")
    with pytest.raises(ValidationError, match="symlink not allowed"):
        validate_bundle(tmp_path)


def test_broken_symlink_rejected(tmp_path: Path) -> None:
    _minimal_skill(tmp_path)
    (tmp_path / "orphan").symlink_to("/nonexistent/absolute/path")
    with pytest.raises(ValidationError, match="symlink not allowed"):
        validate_bundle(tmp_path)
