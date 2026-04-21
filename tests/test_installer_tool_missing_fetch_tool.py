"""When neither gh nor git is on PATH → tools return code=9."""

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


async def test_marketplace_list_no_fetch_tool(
    configured_installer: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant.tools_sdk import _installer_core as core
    from assistant.tools_sdk.installer import marketplace_list

    # Force the core _fetch_tool path to raise by making shutil.which return None.
    monkeypatch.setattr(core.shutil, "which", lambda _name: None)

    # marketplace_list_entries calls _gh_api_async which requires gh;
    # make it raise FetchToolMissing directly.
    async def _raise(*_a: object, **_k: object) -> None:
        raise core.FetchToolMissing("no gh/git")

    monkeypatch.setattr(core, "marketplace_list_entries", _raise)
    result = await marketplace_list.handler({})
    assert result.get("is_error") is True
    assert result["code"] == 9  # CODE_NO_FETCH_TOOL


async def test_fetch_tool_raises_when_both_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant.tools_sdk import _installer_core as core

    monkeypatch.setattr(core.shutil, "which", lambda _name: None)
    with pytest.raises(core.FetchToolMissing):
        core._fetch_tool()


async def test_fetch_tool_prefers_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant.tools_sdk import _installer_core as core

    monkeypatch.setattr(core.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    assert core._fetch_tool() == "gh"

    # Only git
    monkeypatch.setattr(
        core.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None
    )
    assert core._fetch_tool() == "git"
