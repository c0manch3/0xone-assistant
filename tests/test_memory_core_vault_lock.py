"""vault_lock tests — fcntl.flock blocking/non-blocking + timeout."""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

import pytest

from assistant.tools_sdk._memory_core import vault_lock


def test_vault_lock_nonblocking_contention_raises(tmp_path: Path) -> None:
    """Non-blocking acquisition on a held lock raises BlockingIOError.

    On Darwin ``fcntl.flock`` is per-fd — a fresh ``os.open`` + flock in
    the same process DOES contend against a held flock on another fd of
    the same path. Our helper uses a fresh fd under the hood, so the
    contention surfaces.
    """
    lock = tmp_path / "mem.lock"
    fd = os.open(lock, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError), vault_lock(
            lock, blocking=False
        ):
            pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_vault_lock_acquires_when_free(tmp_path: Path) -> None:
    """Happy path: freshly created lock path is acquirable."""
    lock = tmp_path / "mem.lock"
    with vault_lock(lock, blocking=False):
        pass
    # File is left behind by the helper for subsequent calls.
    assert lock.exists()


def test_vault_lock_blocking_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocking-with-timeout raises TimeoutError when the deadline elapses.

    Forces an always-contended lock by mocking flock to always raise
    BlockingIOError.
    """
    lock = tmp_path / "mem.lock"

    def always_blocked(fd: int, op: int) -> None:
        # Re-raise on every attempted non-blocking exclusive acquire.
        if op & fcntl.LOCK_NB:
            raise BlockingIOError("simulated contention")

    monkeypatch.setattr(fcntl, "flock", always_blocked)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError), vault_lock(
        lock, blocking=True, timeout=0.2
    ):
        pass
    elapsed = time.monotonic() - t0
    assert 0.15 <= elapsed <= 1.0, f"elapsed={elapsed}"


def test_memory_lock_released_after_kill(tmp_path: Path) -> None:
    """Advisory flock is released when the owning fd is closed.

    Simulates a crashed daemon by opening+flock+close; the next
    acquisition MUST succeed.
    """
    lock = tmp_path / "mem.lock"
    # Acquire + close (simulated crash).
    fd = os.open(lock, os.O_CREAT | os.O_WRONLY, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(fd)  # advisory lock released at close.
    # Next acquisition via our helper succeeds.
    with vault_lock(lock, blocking=False):
        pass
