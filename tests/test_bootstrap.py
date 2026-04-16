from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.bootstrap import ensure_skills_symlink


def test_ensure_skills_symlink_idempotent(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    ensure_skills_symlink(tmp_path)
    link = tmp_path / ".claude" / "skills"
    assert link.is_symlink()
    assert Path(link.readlink()) == Path("../skills")

    # Second invocation must keep the same link (no clobber, no exception).
    ensure_skills_symlink(tmp_path)
    assert link.is_symlink()
    assert Path(link.readlink()) == Path("../skills")


def test_ensure_skills_symlink_replaces_wrong_target(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "skills").symlink_to("../wrong_target", target_is_directory=True)

    ensure_skills_symlink(tmp_path)
    link = claude_dir / "skills"
    assert Path(link.readlink()) == Path("../skills")


def test_ensure_skills_symlink_tolerates_empty_real_dir(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "skills").mkdir()  # empty real dir

    ensure_skills_symlink(tmp_path)
    assert (claude_dir / "skills").is_symlink()


def test_ensure_skills_symlink_refuses_nonempty_real_dir(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    real = claude_dir / "skills"
    real.mkdir()
    (real / "rogue").write_text("do not clobber", encoding="utf-8")

    with pytest.raises(RuntimeError):
        ensure_skills_symlink(tmp_path)
