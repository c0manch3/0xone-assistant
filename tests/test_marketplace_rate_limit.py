"""marketplace rate-limit detection — B10 wave-2."""

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


async def test_marketplace_list_rate_limited_message(
    configured_installer: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rate-limit error message must mention `gh auth login` remedy."""
    from assistant.tools_sdk import _installer_core as core
    from assistant.tools_sdk.installer import marketplace_list

    async def _raise_rate(*_a: object, **_k: object) -> None:
        raise core.MarketplaceError(
            "GitHub API rate-limited. Run `gh auth login` to raise limit from 60 to 5000 req/hour."
        )

    monkeypatch.setattr(core, "marketplace_list_entries", _raise_rate)
    result = await marketplace_list.handler({})
    assert result.get("is_error") is True
    assert result["code"] == 10  # CODE_MARKETPLACE
    assert "gh auth login" in result["error"]
    assert "5000" in result["error"]
