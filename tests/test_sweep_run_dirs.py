"""Sweeper: tmp/ older than 1 h + installer-cache/ older than 7 d removed."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from assistant.config import ClaudeSettings, Settings
from assistant.main import Daemon


def _make_entry(base: Path, name: str, *, age_s: float) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    entry = base / name
    entry.mkdir()
    (entry / "marker").write_text("x", encoding="utf-8")
    past = time.time() - age_s
    os.utime(entry, (past, past))
    os.utime(entry / "marker", (past, past))
    return entry


@pytest.mark.asyncio
async def test_sweep_removes_old_entries_only(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    tmp_base = data_dir / "run" / "tmp"
    cache_base = data_dir / "run" / "installer-cache"

    old_tmp = _make_entry(tmp_base, "old", age_s=3600 * 2)
    new_tmp = _make_entry(tmp_base, "new", age_s=10 * 60)
    stale_cache = _make_entry(cache_base, "stale", age_s=8 * 86400)
    fresh_cache = _make_entry(cache_base, "fresh", age_s=86400)

    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=data_dir,
        claude=ClaudeSettings(),
    )
    daemon = Daemon(settings)
    await daemon._sweep_run_dirs()

    assert not old_tmp.exists()
    assert new_tmp.exists()
    assert not stale_cache.exists()
    assert fresh_cache.exists()


@pytest.mark.asyncio
async def test_sweep_no_crash_when_dirs_missing(tmp_path: Path) -> None:
    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "no-such-dir",
        claude=ClaudeSettings(),
    )
    daemon = Daemon(settings)
    # Must not raise — missing data_dir is the first-run case.
    await daemon._sweep_run_dirs()


@pytest.mark.asyncio
async def test_sweep_unlinks_loose_files(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    tmp_base = data_dir / "run" / "tmp"
    tmp_base.mkdir(parents=True)
    loose = tmp_base / "stray.txt"
    loose.write_text("x", encoding="utf-8")
    past = time.time() - 3600 * 3
    os.utime(loose, (past, past))

    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=data_dir,
        claude=ClaudeSettings(),
    )
    await Daemon(settings)._sweep_run_dirs()
    assert not loose.exists()
