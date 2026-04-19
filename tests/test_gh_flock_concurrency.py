"""I-8.2 / R-4 — single-flight flock on ``<data_dir>/run/gh-vault-commit.lock``.

Two parallel ``vault-commit-push`` invocations must serialise via
``fcntl.flock(LOCK_EX | LOCK_NB)``. The second one — unable to acquire
the lock — must exit 9 (``LOCK_BUSY``) IMMEDIATELY (Q1 decision: no
wait, no retry; the scheduler retries on its own).

We drive this at the low-level level (via :func:`flock_exclusive_nb`
directly) because the high-level CLI path would require two separate
Python interpreter processes with precisely-coordinated timing, which
is brittle. The low-level test is equally conclusive for I-8.2 because
the CLI's ONLY single-flight barrier is the `with flock_exclusive_nb(...)`
block in ``_cmd_vault_commit_push``.

Also covers the multiprocess integration via a real ``Popen`` helper
invocation to prove the fd semantics work across process boundaries
(not just threads / within one interpreter).
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import pytest

from tools.gh._lib.lock import LockBusyError, flock_exclusive_nb


def test_second_acquire_raises_lockbusy(tmp_path: Path) -> None:
    """Holding the flock → second `flock_exclusive_nb` immediately raises."""
    lock_path = tmp_path / "run" / "gh-vault-commit.lock"

    with flock_exclusive_nb(lock_path):
        # First context active. Second attempt MUST raise before blocking.
        start = time.monotonic()
        with pytest.raises(LockBusyError):
            with flock_exclusive_nb(lock_path):
                pytest.fail("second flock_exclusive_nb should not succeed")
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 100, (
            f"LOCK_NB should fail-fast in < 100ms, took {elapsed_ms:.1f}ms"
        )


def test_lock_released_after_context_exit(tmp_path: Path) -> None:
    """Normal context-manager exit releases the lock; re-acquire works."""
    lock_path = tmp_path / "run" / "gh-vault-commit.lock"

    with flock_exclusive_nb(lock_path):
        pass  # quick acquire-release cycle

    # Second acquisition must succeed — no stale lock state.
    with flock_exclusive_nb(lock_path):
        pass  # ok


def _child_holder(lock_path: str, ready_file: str, release_file: str) -> None:
    """Subprocess helper: acquire the lock, signal readiness, wait for release signal.

    Communicates with the parent via two marker files:
    - ``ready_file``: created after the flock is acquired, parent polls.
    - ``release_file``: parent touches it to tell the child to exit.

    We avoid :class:`multiprocessing.Event` because on some platforms
    (especially with `spawn` start method) pickling state across the
    fork boundary interacts badly with the fd inheritance we want to
    test.
    """
    with flock_exclusive_nb(Path(lock_path)):
        Path(ready_file).write_text("1")
        while not Path(release_file).exists():
            time.sleep(0.01)


def test_second_process_cannot_acquire(tmp_path: Path) -> None:
    """Two SEPARATE processes — second raises :class:`LockBusyError`."""
    lock_path = tmp_path / "run" / "gh-vault-commit.lock"
    ready = tmp_path / "ready"
    release = tmp_path / "release"

    ctx = mp.get_context("spawn")  # explicit — stable across platforms
    proc = ctx.Process(target=_child_holder, args=(str(lock_path), str(ready), str(release)))
    proc.start()
    try:
        # Wait for the child to acquire the lock.
        deadline = time.monotonic() + 5.0
        while not ready.exists():
            if time.monotonic() > deadline:
                pytest.fail("child did not acquire lock within 5s")
            time.sleep(0.01)

        # Parent tries to acquire — must raise immediately.
        start = time.monotonic()
        with pytest.raises(LockBusyError):
            with flock_exclusive_nb(lock_path):
                pytest.fail("parent should not have acquired while child holds")
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 200, (
            f"LOCK_NB should fail fast even across processes, took {elapsed_ms:.1f}ms"
        )
    finally:
        # Tell the child to release the lock and exit.
        release.write_text("1")
        proc.join(timeout=5.0)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2.0)
