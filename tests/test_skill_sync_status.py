"""skill_sync_status — reads status.json written by spawn_uv_sync_bg."""

from __future__ import annotations

import json
import time
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


async def test_sync_status_unknown(
    configured_installer: tuple[Path, Path],
) -> None:
    from assistant.tools_sdk.installer import skill_sync_status

    result = await skill_sync_status.handler({"name": "nothere"})
    assert result["status"] == "unknown"


async def test_sync_status_reads_record(
    configured_installer: tuple[Path, Path],
) -> None:
    _, data_dir = configured_installer
    from assistant.tools_sdk.installer import skill_sync_status

    sd = data_dir / "run" / "sync"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "foo.status.json").write_text(
        json.dumps({"status": "ok", "finished_at": time.time()}),
        encoding="utf-8",
    )
    result = await skill_sync_status.handler({"name": "foo"})
    assert result["status"] == "ok"
    assert "finished_at" in result


async def test_sync_status_invalid_name(
    configured_installer: tuple[Path, Path],
) -> None:
    from assistant.tools_sdk.installer import skill_sync_status

    result = await skill_sync_status.handler({"name": "BadName"})
    assert result.get("is_error") is True
    assert result["code"] == 11
