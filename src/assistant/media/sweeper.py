"""Media retention sweeper (phase 7).

Two-phase sweep, same tick:

  **Phase A (age-based)** — unlink entries in `inbox/` older than
  `retention_inbox_days` and entries in `outbox/` older than
  `retention_outbox_days`. The daemon's `_ensure_vault` sets 0o700
  perms; stat's `st_mtime` is the source of truth for age.

  **Phase B (LRU cap)** — if the combined byte size of `inbox/` +
  `outbox/` exceeds `retention_total_cap_bytes` AFTER phase A,
  evict oldest-first until we're under the cap. `outbox/` entries
  are evicted first (model-produced artefacts are cheap to
  regenerate; user-uploaded inbox files are not).

Ordering rationale: phase A first means an old-AND-huge file is
billed to phase A, not phase B, so the LRU counters stay
proportional to "what the user actually touches recently". Doing
LRU first would waste work evicting items age-based would remove
anyway.

Concurrency:
  The sweeper is a single background task spawned by `Daemon.start`
  AFTER `ensure_media_dirs()` completes (pitfall #14). We do NOT
  take a lock against the adapter's writer path: `rm` on an open
  file-descriptor is safe on POSIX (the inode is retained until
  the last fd closes), so a concurrent download finishing after
  the sweeper unlinks its dest_path simply produces an orphaned
  inode that vanishes when the download closes. The unlink is
  atomic per-file.

  Windows semantics differ (open file can't be unlinked), but the
  project targets POSIX-only deployment; the sweeper guards the
  unlink with `unlink(missing_ok=True)` so a Windows-CI run
  wouldn't explode either.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from assistant.media.paths import inbox_dir, outbox_dir

if TYPE_CHECKING:
    from assistant.config import Settings


@dataclass(slots=True)
class _Entry:
    """One file under inbox/ or outbox/ ranked for retention.

    Kept as a dataclass (not a NamedTuple) because LRU sort keys and
    byte-size bookkeeping evolve together; a future "also weight by
    access count" extension would add a field here without breaking
    callers.
    """

    path: Path
    # True → outbox (evict first in phase B), False → inbox.
    is_outbox: bool
    mtime: float
    size: int


def _scan(dir_path: Path) -> list[_Entry]:
    """Synchronously walk `dir_path` and return every file entry.

    Broken symlinks / vanished-mid-scan entries are silently skipped
    (the race between `os.scandir` yielding a name and our `stat`
    call is narrow but real; skipping is safer than crashing the
    sweeper loop).
    """
    entries: list[_Entry] = []
    if not dir_path.exists():
        return entries
    is_outbox = dir_path.name == "outbox"
    with os.scandir(dir_path) as it:
        for item in it:
            try:
                if not item.is_file(follow_symlinks=False):
                    # Skip sub-directories -- not currently written by
                    # any tool, but the guard is cheap and prevents a
                    # future tool-that-creates-directories from
                    # confusing the sweeper.
                    continue
                stat = item.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                # permission denied / stale NFS handle -- skip to avoid
                # blowing up the sweeper.
                continue
            entries.append(
                _Entry(
                    path=Path(item.path),
                    is_outbox=is_outbox,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                )
            )
    return entries


def _safe_unlink(path: Path, log: Any) -> int:
    """Unlink `path`, swallowing FileNotFoundError; return bytes freed.

    On an unexpected OSError (e.g. permission denied) we log a
    warning and return 0 so the caller can continue sweeping.
    Returning 0 (rather than re-raising) keeps one noisy file from
    halting the sweep of 1000 legitimate ones.
    """
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    try:
        path.unlink()
        return size
    except FileNotFoundError:
        return 0
    except OSError as exc:
        log.warning("media_sweep_unlink_failed", path=str(path), error=repr(exc))
        return 0


async def sweep_media_once(
    data_dir: Path, settings: Settings, log: Any
) -> dict[str, int]:
    """Run one sweep pass over `<data_dir>/media/`.

    Returns a summary dict with three keys: `removed_old` (phase A
    count), `removed_lru` (phase B count), `bytes_freed` (total).
    Return value is suitable for structured-log emission by the
    caller so each tick can be correlated.

    Called by `media_sweeper_loop` every `settings.media.sweep_interval_s`.
    Safe to call directly from tests (it's a coroutine wrapping a
    thread-dispatched scan; no global state, no locks).
    """

    def _do_sweep() -> dict[str, int]:
        now = time.time()
        inbox_max_age_s = settings.media.retention_inbox_days * 86400
        outbox_max_age_s = settings.media.retention_outbox_days * 86400
        total_cap = settings.media.retention_total_cap_bytes

        entries = _scan(inbox_dir(data_dir)) + _scan(outbox_dir(data_dir))

        # Phase A: age-based.
        removed_old = 0
        bytes_freed = 0
        survivors: list[_Entry] = []
        for e in entries:
            age_s = now - e.mtime
            cap = outbox_max_age_s if e.is_outbox else inbox_max_age_s
            if age_s > cap:
                freed = _safe_unlink(e.path, log)
                if freed > 0:
                    removed_old += 1
                    bytes_freed += freed
                else:
                    # unlink failed -- keep in survivors for LRU so
                    # the next tick retries; otherwise a persistently
                    # un-deletable file would "block" LRU from
                    # seeing it.
                    survivors.append(e)
            else:
                survivors.append(e)

        # Phase B: LRU eviction, outbox-first then inbox-first, both
        # oldest-first. After sorting the survivors by (outbox-rank,
        # mtime ascending) we pop from the front until we're under
        # the cap.
        total_bytes = sum(e.size for e in survivors)
        removed_lru = 0
        if total_bytes > total_cap:
            # Primary key: is_outbox desc (True first), so outbox
            # entries are evicted before inbox of the same age.
            # Secondary: mtime asc (oldest first).
            survivors.sort(key=lambda e: (not e.is_outbox, e.mtime))
            i = 0
            while total_bytes > total_cap and i < len(survivors):
                freed = _safe_unlink(survivors[i].path, log)
                if freed > 0:
                    removed_lru += 1
                    bytes_freed += freed
                    total_bytes -= freed
                i += 1

        return {
            "removed_old": removed_old,
            "removed_lru": removed_lru,
            "bytes_freed": bytes_freed,
        }

    # Dispatch to a thread to avoid blocking the loop on a large
    # inbox (e.g. 10k files on spinning disk). The per-tick cost is
    # typically tens of ms; the thread dispatch keeps latency
    # predictable.
    result = await asyncio.to_thread(_do_sweep)
    return result


async def media_sweeper_loop(
    data_dir: Path,
    settings: Settings,
    stop_event: asyncio.Event,
    log: Any,
) -> None:
    """Run `sweep_media_once` every `settings.media.sweep_interval_s`.

    Uses `asyncio.wait_for(stop_event.wait(), timeout=...)` for the
    sleep step so shutdown is responsive (no up-to-one-hour wait
    when the daemon is asked to stop between ticks). `stop_event`
    is set by `Daemon.stop`; the loop exits cleanly after the
    in-flight sweep (if any) finishes.

    The loop itself is wrapped in a try/except Exception per tick so
    one transient failure (full disk, stale NFS) doesn't kill the
    sweeper forever — we log and keep going. `asyncio.CancelledError`
    is NOT caught so `Daemon._drain_bg_tasks` can cancel the task
    cleanly if `stop_event.set()` is racing with a hard-stop.
    """
    interval_s = settings.media.sweep_interval_s
    log.info("media_sweeper_started", interval_s=interval_s)
    while not stop_event.is_set():
        try:
            summary = await sweep_media_once(data_dir, settings, log)
            if (
                summary["removed_old"] > 0
                or summary["removed_lru"] > 0
            ):
                log.info("media_sweep_tick", **summary)
            else:
                log.debug("media_sweep_tick", **summary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Bounded-blast-radius: one bad tick shouldn't kill the
            # sweeper. Log and sleep normally.
            log.warning("media_sweep_tick_failed", error=repr(exc))

        # Interruptible sleep: waking immediately on stop_event.set()
        # is important for test speed (tests set interval=3600 then
        # cancel via stop_event).
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except TimeoutError:
            # Normal wake-up path: no stop signal during the interval.
            continue

    log.info("media_sweeper_stopped")
