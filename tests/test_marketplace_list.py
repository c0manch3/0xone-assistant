"""marketplace_list — gh api returns dir entries; starts with '.' filtered."""

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


async def test_marketplace_list_parses_entries(
    configured_installer: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant.tools_sdk import _installer_core as core
    from assistant.tools_sdk.installer import marketplace_list

    fake_entries = [
        {"name": "pdf", "path": "skills/pdf", "type": "dir"},
        {"name": "csv", "path": "skills/csv", "type": "dir"},
        {"name": ".github", "path": "skills/.github", "type": "dir"},
        {"name": "README.md", "path": "skills/README.md", "type": "file"},
    ]

    async def _fake_gh(_endpoint: str) -> object:
        return fake_entries

    monkeypatch.setattr(core, "_gh_api_async", _fake_gh)
    result = await marketplace_list.handler({})
    names = [e["name"] for e in result["entries"]]
    assert "pdf" in names
    assert "csv" in names
    assert ".github" not in names  # dotfile filtered
    assert "README.md" not in names  # non-dir filtered
    assert result["content"][0]["text"].startswith("- pdf")
