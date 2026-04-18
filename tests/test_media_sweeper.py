"""Phase 7 / commit 5 — `src/assistant/media/sweeper.py` contract.

Covers:
  1. Phase-A age-based eviction for inbox (>N days) and outbox
     (>M days).
  2. Phase-B LRU eviction when combined bytes exceed the cap.
  3. Outbox-first eviction ordering within phase B (model-produced
     artefacts are cheaper to regenerate than user uploads).
  4. `media_sweeper_loop` responds to `stop_event.set()` promptly.
  5. One bad tick does not kill the loop (logs + continues).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest
import structlog

from assistant.config import MediaSettings, Settings
from assistant.media.paths import ensure_media_dirs, inbox_dir, outbox_dir
from assistant.media.sweeper import media_sweeper_loop, sweep_media_once


def _log() -> Any:
    # Real structlog bound logger so info/warning/debug are all
    # exercised (mirrors how the Daemon spawns the task).
    return structlog.get_logger("test.media.sweeper")


def _make_settings(
    tmp_path: Path,
    *,
    inbox_days: int = 14,
    outbox_days: int = 7,
    total_cap: int = 2_147_483_648,
    sweep_interval_s: int = 3600,
) -> Settings:
    media = MediaSettings(
        retention_inbox_days=inbox_days,
        retention_outbox_days=outbox_days,
        retention_total_cap_bytes=total_cap,
        sweep_interval_s=sweep_interval_s,
    )
    return Settings(  # type: ignore[call-arg]
        telegram_bot_token="test",
        owner_chat_id=1,
        data_dir=tmp_path,
        media=media,
    )


def _touch(path: Path, *, size: int, age_s: float) -> None:
    """Create `path` with exactly `size` bytes and mtime `now - age_s`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)
    now = time.time()
    os.utime(path, (now - age_s, now - age_s))


# --- Phase A (age-based) -------------------------------------------


async def test_age_eviction_inbox(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, inbox_days=14, outbox_days=7)
    await ensure_media_dirs(tmp_path)

    fresh = inbox_dir(tmp_path) / "fresh.jpg"
    stale = inbox_dir(tmp_path) / "stale.jpg"
    _touch(fresh, size=1024, age_s=3600)            # 1 h old → keep
    _touch(stale, size=1024, age_s=15 * 86400)      # 15 d old → evict

    summary = await sweep_media_once(tmp_path, settings, _log())
    assert summary["removed_old"] == 1
    assert summary["removed_lru"] == 0
    assert summary["bytes_freed"] == 1024
    assert fresh.exists()
    assert not stale.exists()


async def test_age_eviction_outbox(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, outbox_days=7)
    await ensure_media_dirs(tmp_path)

    fresh = outbox_dir(tmp_path) / "fresh.png"
    stale = outbox_dir(tmp_path) / "stale.png"
    _touch(fresh, size=512, age_s=86400)             # 1 d → keep
    _touch(stale, size=512, age_s=8 * 86400)         # 8 d → evict

    summary = await sweep_media_once(tmp_path, settings, _log())
    assert summary["removed_old"] == 1
    assert not stale.exists()
    assert fresh.exists()


async def test_age_outbox_shorter_than_inbox(tmp_path: Path) -> None:
    # 10 days old: inbox keeps (14d cap), outbox evicts (7d cap).
    settings = _make_settings(tmp_path, inbox_days=14, outbox_days=7)
    await ensure_media_dirs(tmp_path)

    inbox_file = inbox_dir(tmp_path) / "ten_day_in.pdf"
    outbox_file = outbox_dir(tmp_path) / "ten_day_out.pdf"
    _touch(inbox_file, size=100, age_s=10 * 86400)
    _touch(outbox_file, size=100, age_s=10 * 86400)

    summary = await sweep_media_once(tmp_path, settings, _log())
    assert summary["removed_old"] == 1
    assert inbox_file.exists()
    assert not outbox_file.exists()


# --- Phase B (LRU) -------------------------------------------------


async def test_lru_eviction_triggered_over_cap(tmp_path: Path) -> None:
    # cap 1 KB, two 800-byte outbox files, both fresh → phase A keeps
    # both; phase B must evict the older one to get under 1 KB.
    settings = _make_settings(
        tmp_path, total_cap=1000, outbox_days=999, inbox_days=999
    )
    await ensure_media_dirs(tmp_path)

    older = outbox_dir(tmp_path) / "older.png"
    newer = outbox_dir(tmp_path) / "newer.png"
    _touch(older, size=800, age_s=3600)   # 1 h old
    _touch(newer, size=800, age_s=60)     # 1 min old

    summary = await sweep_media_once(tmp_path, settings, _log())
    assert summary["removed_old"] == 0
    assert summary["removed_lru"] == 1
    assert summary["bytes_freed"] == 800
    assert not older.exists()
    assert newer.exists()


async def test_lru_evicts_outbox_before_inbox(tmp_path: Path) -> None:
    # Two files, one inbox + one outbox, both 800 bytes, cap 1000.
    # Outbox evicted first per sweeper policy (cheap to regenerate).
    settings = _make_settings(
        tmp_path, total_cap=1000, outbox_days=999, inbox_days=999
    )
    await ensure_media_dirs(tmp_path)

    # Inbox is OLDER — but outbox should still go first.
    inbox_file = inbox_dir(tmp_path) / "old_inbox.jpg"
    outbox_file = outbox_dir(tmp_path) / "newer_outbox.png"
    _touch(inbox_file, size=800, age_s=3600)
    _touch(outbox_file, size=800, age_s=60)

    summary = await sweep_media_once(tmp_path, settings, _log())
    assert summary["removed_lru"] == 1
    assert inbox_file.exists()
    assert not outbox_file.exists()


async def test_lru_noop_under_cap(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, total_cap=10 * 1024, outbox_days=999)
    await ensure_media_dirs(tmp_path)
    probe = outbox_dir(tmp_path) / "tiny.png"
    _touch(probe, size=512, age_s=60)
    summary = await sweep_media_once(tmp_path, settings, _log())
    assert summary["removed_lru"] == 0
    assert probe.exists()


# --- Phase A + B combined ------------------------------------------


async def test_combined_age_then_lru(tmp_path: Path) -> None:
    # Configure: outbox age-cap 1 d, total-cap 1000 bytes.
    # Layout:
    #   outbox/old_big.png   500 B  2 d old   → phase A (too old)
    #   outbox/keep1.png     400 B  1 h old
    #   outbox/keep2.png     400 B  1 h old
    # After phase A: 800 bytes remain. Under 1000 → phase B no-op.
    settings = _make_settings(tmp_path, outbox_days=1, total_cap=1000)
    await ensure_media_dirs(tmp_path)

    old_big = outbox_dir(tmp_path) / "old_big.png"
    keep1 = outbox_dir(tmp_path) / "keep1.png"
    keep2 = outbox_dir(tmp_path) / "keep2.png"
    _touch(old_big, size=500, age_s=2 * 86400)
    _touch(keep1, size=400, age_s=3600)
    _touch(keep2, size=400, age_s=3600)

    summary = await sweep_media_once(tmp_path, settings, _log())
    assert summary["removed_old"] == 1
    assert summary["removed_lru"] == 0
    assert not old_big.exists()
    assert keep1.exists() and keep2.exists()


# --- sweep on missing dirs is graceful ------------------------------


async def test_sweep_with_missing_dirs_does_not_crash(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    # Intentionally DO NOT call ensure_media_dirs.
    summary = await sweep_media_once(tmp_path, settings, _log())
    assert summary == {
        "removed_old": 0,
        "removed_lru": 0,
        "bytes_freed": 0,
    }


# --- media_sweeper_loop stop semantics -----------------------------


async def test_loop_stops_on_event(tmp_path: Path) -> None:
    # Very short interval so even a miss in the wait_for still exits
    # within the test timeout.
    settings = _make_settings(tmp_path, sweep_interval_s=3600)
    await ensure_media_dirs(tmp_path)

    stop_event = asyncio.Event()
    task = asyncio.create_task(
        media_sweeper_loop(tmp_path, settings, stop_event, _log())
    )
    # Yield so the first tick has a chance to run.
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done() and task.exception() is None


async def test_loop_survives_one_bad_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Patch `sweep_media_once` to raise once, then succeed. The loop
    # must log + continue rather than exit.
    settings = _make_settings(tmp_path, sweep_interval_s=3600)
    await ensure_media_dirs(tmp_path)

    calls = {"count": 0}

    async def _flaky(*args: Any, **kwargs: Any) -> dict[str, int]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("transient")
        return {"removed_old": 0, "removed_lru": 0, "bytes_freed": 0}

    monkeypatch.setattr(
        "assistant.media.sweeper.sweep_media_once", _flaky
    )

    stop_event = asyncio.Event()
    task = asyncio.create_task(
        media_sweeper_loop(tmp_path, settings, stop_event, _log())
    )
    await asyncio.sleep(0.1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert calls["count"] >= 1  # tick ran at least once
    assert task.exception() is None
