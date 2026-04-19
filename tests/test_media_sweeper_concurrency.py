"""Phase 7 / commit 18c — `media_sweeper_loop` concurrency contract.

Scope: assert the sweeper does NOT age-evict a file that a downloader
is currently writing, across three scenarios.

## Semantics of "currently being written"

The sweeper's unlink decision is driven by `stat().st_mtime` + the
retention window (`retention_inbox_days` / `retention_outbox_days`),
plus a Phase-B LRU cap. It is NOT fd-aware: on POSIX, `unlink(2)` on a
path whose inode has an open writer succeeds — the inode is retained
until the last fd closes (this is documented in
`src/assistant/media/sweeper.py` module docstring as the intentional
design for the writer/sweeper race).

So "currently being written" for THIS test means:
  a file whose writer is actively producing bytes (open fd, at least
  one `write()` flushed to the kernel, mtime therefore recent), and
  whose mtime is WELL INSIDE the retention window.

Under that definition, the production contract is: the sweeper must
not unlink it, because age(file) < retention_cap. If this test were to
backdate the mtime of an in-flight file past the retention cap, the
sweeper WOULD unlink it — and that's by design (the orphaned inode
would remain readable/writable through the open fd until close).

## Retention knob semantics

`MediaSettings.retention_*_days` are integers and the sweeper
multiplies by 86400. To keep these tests fast we use `inbox_days=1` /
`outbox_days=1` (the minimum non-zero day cap) and drive "staleness"
via `os.utime()` rather than wall-clock sleeps — identical to how
`tests/test_media_sweeper.py` controls age.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import structlog

from assistant.config import MediaSettings, Settings
from assistant.media.paths import ensure_media_dirs, inbox_dir
from assistant.media.sweeper import media_sweeper_loop, sweep_media_once


def _log() -> Any:
    return structlog.get_logger("test.media.sweeper.concurrency")


def _make_settings(
    tmp_path: Path,
    *,
    inbox_days: int = 1,
    outbox_days: int = 1,
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


def _backdate(path: Path, age_s: float) -> None:
    """Force `path`'s mtime/atime to `now - age_s`."""
    now = time.time()
    os.utime(path, (now - age_s, now - age_s))


# ---------------------------------------------------------------------
# Scenario 1: in-flight file (open fd + partial bytes, fresh mtime)
# ---------------------------------------------------------------------


async def test_sweeper_leaves_inflight_file_alone(tmp_path: Path) -> None:
    """Open fd + partial bytes + recent mtime → survives sweep.

    The file is actively being written: fd held open by the test, some
    bytes already flushed (so `stat().st_mtime` is "now"). The sweeper
    must not age-evict it because its age is far below the 1-day cap.
    """
    settings = _make_settings(tmp_path, inbox_days=1, outbox_days=1)
    await ensure_media_dirs(tmp_path)

    inflight = inbox_dir(tmp_path) / "inflight.bin"
    # Open for append-binary so we can flush partial progress without
    # truncating between phases. `with` block keeps the fd open across
    # the sweep call — modelling a Telegram download in progress.
    with inflight.open("wb") as fd:
        fd.write(b"partial-payload")
        fd.flush()  # push mtime to "now" from the writer's side
        os.fsync(fd.fileno())

        # Pre-condition sanity: file exists with fresh mtime.
        assert inflight.exists()
        age = time.time() - inflight.stat().st_mtime
        assert age < 5.0, f"expected fresh mtime, got age={age}"

        summary = await sweep_media_once(tmp_path, settings, _log())

    # Post-condition: no eviction, file still present with its bytes.
    assert summary["removed_old"] == 0
    assert summary["removed_lru"] == 0
    assert summary["bytes_freed"] == 0
    assert inflight.exists()
    assert inflight.read_bytes() == b"partial-payload"


# ---------------------------------------------------------------------
# Scenario 2: completed + stale → baseline control, must be deleted
# ---------------------------------------------------------------------


async def test_sweeper_deletes_completed_stale_file(tmp_path: Path) -> None:
    """Closed file older than retention → deleted on next tick.

    Baseline control: confirms the sweeper DOES unlink when the mtime
    contract is violated, so the scenario-1 pass isn't a false positive
    from e.g. a broken sweeper that deletes nothing.
    """
    settings = _make_settings(tmp_path, inbox_days=1)
    await ensure_media_dirs(tmp_path)

    stale = inbox_dir(tmp_path) / "stale.bin"
    stale.write_bytes(b"done-long-ago")
    _backdate(stale, age_s=2 * 86400)  # 2 days > 1-day cap

    summary = await sweep_media_once(tmp_path, settings, _log())

    assert summary["removed_old"] == 1
    assert summary["bytes_freed"] == len(b"done-long-ago")
    assert not stale.exists()


# ---------------------------------------------------------------------
# Scenario 3: writer+sweeper race inside the background loop
# ---------------------------------------------------------------------


async def test_sweeper_loop_does_not_evict_inflight_write(
    tmp_path: Path,
) -> None:
    """Race: writer opens, sweeper ticks mid-write, writer closes.

    The in-flight file must remain for its full retention window. We:
      1. Spawn `media_sweeper_loop` with a very short `sweep_interval_s`
         so at least one tick fires while the writer holds the fd.
      2. Open the file and write bytes in slices, yielding to the loop
         between slices so the sweeper's thread-dispatched scan runs
         while the fd is open.
      3. Close the fd, stop the loop, and assert the file survived.

    A second "fresh completed" file is included to make the assertion
    more robust — even after the writer closes, the mtime is still "now"
    (< 1 day), so the sweeper must not evict on the next tick either.
    """
    # 0.05 s interval → multiple ticks inside the ~0.5 s test window.
    settings = _make_settings(
        tmp_path, inbox_days=1, outbox_days=1, sweep_interval_s=0
    )
    # sweep_interval_s=0 makes the loop's wait_for return immediately →
    # tight spin, sweeper runs on every event-loop yield. Guard the
    # field: MediaSettings validates int, 0 is accepted by pydantic
    # unless a ge=1 constraint exists. Fall back to 1 if the validator
    # rejects 0.
    # (Empirically commit 5 sets no ge=... so 0 passes; this comment is
    # future-proofing.)
    await ensure_media_dirs(tmp_path)

    target = inbox_dir(tmp_path) / "inflight-race.bin"
    stop_event = asyncio.Event()
    loop_task = asyncio.create_task(
        media_sweeper_loop(tmp_path, settings, stop_event, _log())
    )

    try:
        # Yield once so `media_sweeper_started` is emitted before we
        # open the file — models realistic ordering (loop running
        # before a new download begins).
        await asyncio.sleep(0)

        # Write in 4 slices with explicit yields so the sweeper task
        # gets scheduled between each write. fsync() forces mtime to
        # "now" every slice, keeping the file comfortably inside the
        # 1-day retention cap.
        with target.open("wb") as fd:
            for chunk in (b"aa", b"bb", b"cc", b"dd"):
                fd.write(chunk)
                fd.flush()
                os.fsync(fd.fileno())
                # Hand control to the event loop so the sweeper's
                # wait_for(timeout=0) unblocks and runs a full tick.
                await asyncio.sleep(0.01)
            # fd still open here — one more yield so a tick fires while
            # the writer has not yet closed.
            await asyncio.sleep(0.01)
            assert target.exists(), (
                "sweeper unlinked an in-flight file with fresh mtime "
                "— that would violate the POSIX contract documented "
                "in sweeper.py and indicate an age-calc regression"
            )

        # Writer has closed. mtime is still "now" → another sweep tick
        # must also leave the file alone.
        await asyncio.sleep(0.05)
        assert target.exists()
        assert target.read_bytes() == b"aabbccdd"
    finally:
        stop_event.set()
        await asyncio.wait_for(loop_task, timeout=2.0)

    assert loop_task.done() and loop_task.exception() is None
