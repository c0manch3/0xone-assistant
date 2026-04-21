"""skill_uninstall — remove + sentinel; idempotent on missing skill."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def configured_installer(tmp_path: Path) -> tuple[Path, Path]:
    from assistant.tools_sdk.installer import configure_installer

    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "skills").mkdir()
    (project_root / "tools").mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    configure_installer(project_root=project_root, data_dir=data_dir)
    return project_root, data_dir


async def test_uninstall_removes_files(
    configured_installer: tuple[Path, Path],
) -> None:
    project_root, data_dir = configured_installer
    from assistant.tools_sdk.installer import skill_uninstall

    skill_path = project_root / "skills" / "zeta"
    tool_path = project_root / "tools" / "zeta"
    skill_path.mkdir()
    tool_path.mkdir()
    (skill_path / "SKILL.md").write_text("body", encoding="utf-8")
    (tool_path / "main.py").write_text("print()", encoding="utf-8")

    result = await skill_uninstall.handler({"name": "zeta", "confirmed": True})
    assert result.get("removed") is True
    assert not skill_path.exists()
    assert not tool_path.exists()
    sentinel = data_dir / "run" / "skills.dirty"
    assert sentinel.is_file()


async def test_uninstall_idempotent(
    configured_installer: tuple[Path, Path],
) -> None:
    from assistant.tools_sdk.installer import skill_uninstall

    result = await skill_uninstall.handler({"name": "nonexistent", "confirmed": True})
    assert result.get("removed") is False
    assert result["reason"] == "not installed"


async def test_uninstall_unconfirmed(
    configured_installer: tuple[Path, Path],
) -> None:
    from assistant.tools_sdk.installer import skill_uninstall

    result = await skill_uninstall.handler({"name": "zeta", "confirmed": False})
    assert result.get("is_error") is True
    assert result["code"] == 3  # CODE_NOT_CONFIRMED


async def test_uninstall_invalid_name(
    configured_installer: tuple[Path, Path],
) -> None:
    from assistant.tools_sdk.installer import skill_uninstall

    result = await skill_uninstall.handler({"name": "../etc", "confirmed": True})
    assert result.get("is_error") is True
    assert result["code"] == 11  # CODE_NAME_INVALID
