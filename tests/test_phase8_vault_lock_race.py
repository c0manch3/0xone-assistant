"""Phase 8 fix-pack F7 - AC#12 vault_lock x memory_write race.

The vault sync subsystem holds the SAME fcntl-based ``vault_lock``
that ``memory_write`` uses (the one in
``assistant.tools_sdk._memory_core``). The contract:

  - vault sync acquires the lock around git status / add / commit.
  - memory_write acquires the lock around its tempfile + os.replace
    pipeline.

The serialisation guarantees that a half-written ``.tmp/.tmp-XXX.md``
cannot land in a commit AND that a concurrent vault sync tick does
not see the in-flight tempfile.

This test exercises the actual fcntl primitive (no mock) by:

  1. Acquiring vault_lock from one task.
  2. Trying to acquire it from another task (blocks until the first
     releases).
  3. Asserting that the second task waits, then proceeds.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from assistant.tools_sdk._memory_core import vault_lock


@pytest.mark.asyncio
async def test_concurrent_acquisitions_serialise(tmp_path: Path) -> None:
    """AC#12 — two concurrent vault_lock holders serialise. The
    second waiter blocks until the first releases.
    """
    lock_path = tmp_path / "memory-index.db.lock"
    events: list[str] = []

    async def _holder_a() -> None:
        with vault_lock(lock_path, blocking=True, timeout=5.0):
            events.append("a-acquired")
            await asyncio.sleep(0.2)
            events.append("a-releasing")

    async def _holder_b() -> None:
        # Start slightly after A so A wins the race deterministically.
        await asyncio.sleep(0.05)
        with vault_lock(lock_path, blocking=True, timeout=5.0):
            events.append("b-acquired")

    await asyncio.gather(
        asyncio.to_thread(asyncio.run, _holder_a()),
        asyncio.to_thread(asyncio.run, _holder_b()),
    )
    # Order must be: A acquires → A releases → B acquires.
    assert events == ["a-acquired", "a-releasing", "b-acquired"]


def test_lock_contention_timeout(tmp_path: Path) -> None:
    """If a holder keeps the lock past the waiter's timeout, the
    waiter raises ``TimeoutError`` (the daemon-side cycle classifies
    this as ``result=lock_contention`` per W2-C1)."""
    lock_path = tmp_path / "memory-index.db.lock"
    holder_started = []

    def _holder() -> None:
        with vault_lock(lock_path, blocking=True, timeout=5.0):
            holder_started.append(True)
            time.sleep(1.0)

    import threading

    t = threading.Thread(target=_holder)
    t.start()
    # Wait for holder to acquire.
    while not holder_started:
        time.sleep(0.01)
    # Try to acquire with a tight timeout — must raise.
    with (
        pytest.raises(TimeoutError),
        vault_lock(lock_path, blocking=True, timeout=0.2),
    ):
        pass
    t.join()


def test_lock_atomicity_no_partial_state(tmp_path: Path) -> None:
    """The lock is fcntl-based — kernel guarantees mutual exclusion
    even on segfault / SIGKILL of a holder. We exercise the
    non-blocking acquire after a successful release: it MUST succeed
    immediately (no leftover state)."""
    lock_path = tmp_path / "memory-index.db.lock"
    with vault_lock(lock_path, blocking=True, timeout=5.0):
        pass
    # Lock released — fresh acquire works without contention.
    with vault_lock(lock_path, blocking=False):
        pass
