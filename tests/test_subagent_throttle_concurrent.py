"""Phase 7 fix-pack I4 — per-chat `asyncio.Lock` closes `_throttle` TOCTOU.

Before the fix, ``subagent/hooks.py::_throttle`` was a bare
read-then-sleep-then-write sequence:

    now = time.monotonic()
    last = last_notify_at.get(chat_id, 0.0)
    delta_ms = (now - last) * 1000.0
    if delta_ms < interval_ms:
        await asyncio.sleep(...)
    last_notify_at[chat_id] = time.monotonic()

Two concurrent Stop hooks for the same chat both read the stale
``last_notify_at`` value, both concluded the window had elapsed,
and both fired their ``adapter.send_text`` calls in the same
scheduler tick. The min-interval invariant — designed to keep
Telegram from flood-waiting us on bursty subagent completion —
got silently defeated.

The fix introduces a per-chat ``asyncio.Lock`` (stored in an
``OrderedDict`` with LRU eviction mirroring the timestamp dict)
acquired around the entire read → sleep → write critical section.
The second concurrent delivery now observes the refreshed
``last_notify_at`` value after the first one releases the lock
and genuinely sleeps for the residual interval.

This test exercises the concurrent path directly via ``_throttle``
rather than through the full hook plumbing — the lock semantics
are the thing we want to verify, and going through the hook path
adds noise (transcript read, record_finished) that doesn't change
the TOCTOU argument.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

from assistant.subagent.hooks import _throttle


async def test_two_concurrent_throttles_serialise_to_second_sleep() -> None:
    """Two concurrent ``_throttle`` calls on the SAME chat_id must
    serialise — the second call genuinely sleeps for (close to) the
    configured interval rather than short-circuiting on a stale
    read."""
    last_notify_at: OrderedDict[int, float] = OrderedDict()
    throttle_locks: OrderedDict[int, asyncio.Lock] = OrderedDict()
    interval_ms = 100  # tight enough to keep the test fast
    chat_id = 42

    start = time.monotonic()
    # Gather returns only once both coroutines complete; the second
    # one is forced to await the lock THEN the residual sleep.
    await asyncio.gather(
        _throttle(last_notify_at, throttle_locks, chat_id, interval_ms),
        _throttle(last_notify_at, throttle_locks, chat_id, interval_ms),
    )
    elapsed_ms = (time.monotonic() - start) * 1000.0

    # Lower bound: the second call must have waited ≥ the interval
    # minus a small tolerance for CI jitter. On a TOCTOU-broken
    # implementation the two calls would finish nearly instantly.
    assert elapsed_ms >= (interval_ms - 10), (
        f"expected ≥ ~{interval_ms} ms total wall time, got {elapsed_ms:.1f} ms — "
        "TOCTOU regression: both calls short-circuited"
    )
    # Upper bound: no pathological over-wait (e.g. lock contention
    # running afoul of some scheduler quirk).
    assert elapsed_ms < (interval_ms * 5), (
        f"pathological over-wait: {elapsed_ms:.1f} ms > 5x interval"
    )
    # One timestamp recorded per chat_id (the latest).
    assert list(last_notify_at.keys()) == [chat_id]
    # One lock cached.
    assert list(throttle_locks.keys()) == [chat_id]


async def test_distinct_chats_do_not_block_each_other() -> None:
    """Per-chat granularity: chat A's concurrent traffic does NOT
    serialise behind chat B's in-flight delivery."""
    last_notify_at: OrderedDict[int, float] = OrderedDict()
    throttle_locks: OrderedDict[int, asyncio.Lock] = OrderedDict()
    interval_ms = 100

    # Seed both chats as "just delivered" so their next call would
    # normally sleep. Running in parallel must still finish in
    # ~interval_ms (both sleeps run concurrently), not 2x.
    now_snapshot = time.monotonic()
    last_notify_at[1] = now_snapshot
    last_notify_at[2] = now_snapshot

    start = time.monotonic()
    await asyncio.gather(
        _throttle(last_notify_at, throttle_locks, 1, interval_ms),
        _throttle(last_notify_at, throttle_locks, 2, interval_ms),
    )
    elapsed_ms = (time.monotonic() - start) * 1000.0

    # Two parallel sleeps fire concurrently → total wall time ≈
    # interval_ms, NOT ≈ 2 * interval_ms.
    assert elapsed_ms < (interval_ms * 1.8), (
        f"chats should not serialise each other: expected ≈{interval_ms} ms, "
        f"got {elapsed_ms:.1f} ms"
    )


async def test_throttle_lru_eviction_drops_paired_lock() -> None:
    """When the timestamp dict evicts a key (LRU cap reached), the
    paired lock must also be dropped so the lock dict does not
    unboundedly grow."""
    last_notify_at: OrderedDict[int, float] = OrderedDict()
    throttle_locks: OrderedDict[int, asyncio.Lock] = OrderedDict()
    interval_ms = 1  # near-zero so the test doesn't sleep
    max_entries = 2

    for chat in range(5):
        await _throttle(
            last_notify_at,
            throttle_locks,
            chat,
            interval_ms,
            max_entries=max_entries,
        )

    # Both dicts tracked to the SAME cap — no orphan locks.
    assert len(last_notify_at) == max_entries
    assert len(throttle_locks) == max_entries
    # The most-recent two chats are retained; locks and timestamps
    # agree on which chats survive.
    assert set(last_notify_at.keys()) == set(throttle_locks.keys())
