"""marketplace_info — gh api returns base64 SKILL.md body."""

from __future__ import annotations

import base64
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


async def test_marketplace_info_returns_decoded_body(
    configured_installer: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant.tools_sdk import _installer_core as core
    from assistant.tools_sdk.installer import marketplace_info

    body = "---\nname: pdf\ndescription: PDF skill\n---\n\nBody text"

    async def _fake_gh(_endpoint: str) -> object:
        return {
            "encoding": "base64",
            "content": base64.b64encode(body.encode()).decode(),
        }

    monkeypatch.setattr(core, "_gh_api_async", _fake_gh)
    result = await marketplace_info.handler({"name": "pdf"})
    assert result["content"][0]["text"] == body
    assert result["name"] == "pdf"


async def test_marketplace_info_invalid_name(
    configured_installer: tuple[Path, Path],
) -> None:
    from assistant.tools_sdk.installer import marketplace_info

    result = await marketplace_info.handler({"name": "../etc"})
    assert result.get("is_error") is True
    assert result["code"] == 11  # CODE_NAME_INVALID


async def test_marketplace_info_404(
    configured_installer: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant.tools_sdk import _installer_core as core
    from assistant.tools_sdk.installer import marketplace_info

    async def _fake_gh(_endpoint: str) -> object:
        raise core.MarketplaceError("GitHub API 404: Not Found")

    monkeypatch.setattr(core, "_gh_api_async", _fake_gh)
    result = await marketplace_info.handler({"name": "unknown"})
    assert result.get("is_error") is True
    assert result["code"] == 10
    assert "404" in result["error"]
