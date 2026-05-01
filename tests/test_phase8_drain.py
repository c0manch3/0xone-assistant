"""Phase 8 §2.9 — Daemon.stop drain pattern (asyncio.wait F11).

The drain logic mirrors the phase-6e ``_audio_persist_pending``
pattern but runs BEFORE ``_bg_tasks`` cancel because vault sync
subprocess push tasks aren't shielded — cancelling mid-flight orphans
the SSH pipe and leaves ``.git/index.lock``.

We test the drain *primitive* in isolation (the Daemon.stop body
mostly orchestrates other side-effects); the F11 pattern itself is
the asyncio.wait + ALL_COMPLETED + cancel-leftovers shape.
"""

from __future__ import annotations

import asyncio
from typing import Any


async def _drain_pending_set(
    pending_set: set[asyncio.Task[Any]],
    *,
    timeout_s: float,
) -> tuple[int, int]:
    """Reproduces the F11 drain pattern from
    ``Daemon.stop`` so it can be tested in isolation. Returns
    ``(done_count, not_done_count)``.
    """
    if not pending_set:
        return (0, 0)
    pending = list(pending_set)
    done, not_done = await asyncio.wait(
        pending,
        timeout=timeout_s,
        return_when=asyncio.ALL_COMPLETED,
    )
    if not_done:
        for t in not_done:
            t.cancel()
        await asyncio.gather(*not_done, return_exceptions=True)
    return (len(done), len(not_done))


async def test_drain_happy_path() -> None:
    """All tasks complete within the budget → drain reports 2/0."""

    async def _quick(label: str) -> str:
        await asyncio.sleep(0.01)
        return label

    pending = {asyncio.create_task(_quick("a")), asyncio.create_task(_quick("b"))}
    done, not_done = await _drain_pending_set(pending, timeout_s=1.0)
    assert done == 2
    assert not_done == 0


async def test_drain_timeout_cancels_leftovers() -> None:
    """Tasks exceeding the budget are cancelled and the count is
    reported."""

    async def _slow() -> None:
        await asyncio.sleep(60.0)

    t1 = asyncio.create_task(_slow())
    t2 = asyncio.create_task(_slow())
    pending = {t1, t2}
    done, not_done = await _drain_pending_set(pending, timeout_s=0.05)
    assert done == 0
    assert not_done == 2
    # Both must be cancelled by the time we observe.
    assert t1.cancelled()
    assert t2.cancelled()


async def test_drain_empty_set_is_noop() -> None:
    """An empty pending set returns (0, 0) without invoking
    asyncio.wait."""
    done, not_done = await _drain_pending_set(set(), timeout_s=1.0)
    assert done == 0
    assert not_done == 0
