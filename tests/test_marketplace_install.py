"""marketplace_install — delegates to skill_preview on the tree URL."""

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


async def test_marketplace_install_runs_preview(
    configured_installer: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant.tools_sdk import _installer_core as core
    from assistant.tools_sdk.installer import marketplace_install

    captured: list[str] = []

    async def _fetch(url: str, dest: Path) -> None:
        captured.append(url)
        dest.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — test stub
        (dest / "SKILL.md").write_text(
            "---\nname: pdf\ndescription: PDF skill\n---\n", encoding="utf-8"
        )

    monkeypatch.setattr(core, "fetch_bundle_async", _fetch)
    result = await marketplace_install.handler({"name": "pdf"})
    assert "preview" in result
    assert result["preview"]["name"] == "pdf"
    # The tree URL should have been built from the marketplace template.
    assert len(captured) == 1
    assert "anthropics/skills" in captured[0]
    assert "/tree/main/skills/pdf" in captured[0]


async def test_marketplace_install_invalid_name(
    configured_installer: tuple[Path, Path],
) -> None:
    from assistant.tools_sdk.installer import marketplace_install

    result = await marketplace_install.handler({"name": "../etc"})
    assert result.get("is_error") is True
    assert result["code"] == 11  # CODE_NAME_INVALID
