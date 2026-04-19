"""S1 / R-4 — kernel auto-releases flock on process SIGKILL.

Spike R-4 measured sub-millisecond kernel release on Darwin 24. This
test encodes that behaviour as a regression gate: if a future kernel /
libc regression ever delays flock release on SIGKILL, our I-8.2
invariant would silently deadlock for the scheduler.

Design: spawn a subprocess via :class:`Popen` that holds the flock
indefinitely; wait for the readiness marker; send ``SIGKILL``; measure
how quickly the parent can re-acquire. Must complete in well under
1 second (the spike measured 0.003 ms; we allow 500 ms margin for CI
noise / signal-delivery latency).

The subprocess is launched via :mod:`subprocess` (not
:mod:`multiprocessing`) because we specifically need a real fork-exec
so the fd's kernel owner is a distinct process that SIGKILL can
cleanly terminate without our Python interpreter's cleanup handlers
interfering. ``close_fds=True`` is the default on Python 3.12 — we
rely on it so the spawned interpreter doesn't accidentally inherit any
unrelated test fds that would confuse the fcntl state.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tools.gh._lib.lock import LockBusyError, flock_exclusive_nb


# Child program: acquire the flock, print "READY" + flush, sleep forever.
# Structured as a one-liner so the subprocess doesn't depend on the test
# file layout / sys.path being set up in the child.
_CHILD_PROGRAM = textwrap.dedent(
    """
    import sys, os, fcntl, time
    lock_path = sys.argv[1]
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    sys.stdout.write("READY\\n")
    sys.stdout.flush()
    while True:
        time.sleep(60)
    """
).strip()


def test_lock_released_on_sigkill(tmp_path: Path) -> None:
    """SIGKILL the lock holder → parent reacquires within 500 ms."""
    lock_path = tmp_path / "run" / "gh-vault-commit.lock"

    proc = subprocess.Popen(  # noqa: S603 — launching our own test child
        [sys.executable, "-u", "-c", _CHILD_PROGRAM, str(lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for READY on the child's stdout so we know the lock is held.
        deadline = time.monotonic() + 5.0
        ready = False
        assert proc.stdout is not None  # for type narrowing
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if line.strip() == "READY":
                ready = True
                break
        if not ready:
            pytest.fail(f"child never printed READY; rc={proc.poll()!r}")

        # Confirm the lock is actually busy right now.
        with pytest.raises(LockBusyError):
            with flock_exclusive_nb(lock_path):
                pytest.fail("parent should not acquire while child holds")

        # SIGKILL the holder. Kernel must release the flock sub-millisecond.
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5.0)

        # Measure re-acquire latency. Generous budget (500ms) for CI
        # noise; the actual measurement should be sub-millisecond.
        start = time.monotonic()
        with flock_exclusive_nb(lock_path):
            elapsed_ms = (time.monotonic() - start) * 1000
            assert elapsed_ms < 500, (
                f"flock re-acquire after SIGKILL took {elapsed_ms:.1f}ms "
                "(spike R-4 measured sub-millisecond; S1 regression)"
            )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)
