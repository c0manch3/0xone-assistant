"""Sweep run dirs + legacy staging dirs after a crash."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


async def test_sweep_tmp_older_than_ttl(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    base = tmp_path / "run" / "tmp"
    base.mkdir(parents=True)
    stale = base / "old"
    stale.mkdir()
    fresh = base / "new"
    fresh.mkdir()
    # Stale: >1h old.
    old_mtime = time.time() - (core.INSTALLER_TMP_TTL_SEC + 100)
    os.utime(stale, (old_mtime, old_mtime))
    await core.sweep_run_dirs(tmp_path)
    assert not stale.exists()
    assert fresh.exists()


async def test_sweep_cache_older_than_ttl(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    base = tmp_path / "run" / "installer-cache"
    base.mkdir(parents=True)
    stale = base / "old"
    stale.mkdir()
    (stale / "file").write_text("x", encoding="utf-8")
    fresh = base / "new"
    fresh.mkdir()
    old_mtime = time.time() - (core.INSTALLER_CACHE_TTL_SEC + 100)
    os.utime(stale, (old_mtime, old_mtime))
    await core.sweep_run_dirs(tmp_path)
    assert not stale.exists()
    assert fresh.exists()


async def test_sweep_missing_dirs_ok(tmp_path: Path) -> None:
    """No ``run/`` subtrees present — must not raise."""
    from assistant.tools_sdk import _installer_core as core

    # data_dir is empty tmp_path; run/ doesn't exist yet.
    await core.sweep_run_dirs(tmp_path)  # no crash


async def test_sweep_legacy_stage_dirs(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    pr = tmp_path / "proj"
    (pr / "skills").mkdir(parents=True)
    (pr / "tools").mkdir(parents=True)
    # Legacy staging dirs from a crashed atomic_install.
    (pr / "skills" / ".tmp-x-abc123").mkdir()
    (pr / "tools" / ".tmp-x-def456").mkdir()
    # Real skill that should survive.
    (pr / "skills" / "real").mkdir()
    (pr / "skills" / "real" / "SKILL.md").write_text("body", encoding="utf-8")

    await core.sweep_legacy_stage_dirs(pr)
    assert not (pr / "skills" / ".tmp-x-abc123").exists()
    assert not (pr / "tools" / ".tmp-x-def456").exists()
    assert (pr / "skills" / "real").exists()


@pytest.mark.skip("covered by test_sweep_tmp_older_than_ttl")
async def test_duplicate() -> None:
    pass
