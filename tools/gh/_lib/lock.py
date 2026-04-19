"""Cross-process single-flight lock for ``vault-commit-push`` (I-8.2).

Design: one file per data directory (``<data_dir>/run/gh-vault-commit.lock``)
acquired via :func:`fcntl.flock` with ``LOCK_EX | LOCK_NB``. Non-blocking
semantics (Q1 closed): contending callers immediately raise
:class:`LockBusyError` and the CLI maps that to exit 9 ``LOCK_BUSY``.
No rebase/fetch/stall loop; the owner retries from the scheduler on the
next tick.

Why flock (not asyncio.Lock, not a PID file)

- Process-level not task-level. Two separate `python tools/gh/main.py`
  invocations (owner shell AND scheduler cron firing simultaneously) must
  serialise, and they're in different processes so an in-process
  :class:`asyncio.Lock` is useless.
- Kernel-managed ownership. On process death (normal exit, SIGKILL, OOM),
  the kernel releases the lock within sub-millisecond latency (spike R-4
  measured 0.003 ms on macOS Darwin 24). No stale-PID-file cleanup logic
  is needed; the NEXT invocation just re-acquires.
- File permissions (``0o600``) match the rest of the daemon's run
  directory, so a shared multi-user host still has vault-lock files that
  are invisible to other uids.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path


class LockBusyError(Exception):
    """Raised by :func:`flock_exclusive_nb` when another process holds the lock.

    The exception message is the lock-file path so the operator can
    ``ls -la`` / ``lsof`` to find the holder if debugging is needed.
    Carrying a structured payload would be over-engineering — the CLI
    immediately emits ``{"ok": false, "error": "lock_busy"}`` and exits 9.
    """


@contextlib.contextmanager
def flock_exclusive_nb(lock_path: Path) -> Iterator[int]:
    """Yield an exclusive, non-blocking flock file-descriptor.

    On successful acquisition, yields the raw fd (for tests that want to
    assert the descriptor is still open). On release (``__exit__`` or
    exception), the fd is ``UN``-flocked then ``close()``-d. If the lock
    is already held by another process, raises :class:`LockBusyError`
    IMMEDIATELY (no retry, no wait — Q1 locked to fail-fast).

    Parent directory is created with default mode (caller may have set
    ``<data_dir>/run`` mode 0o700 already); the lock file itself is
    created with mode 0o600 so other local users can't even stat it.

    The inner try/finally pattern tolerates the lock-release syscall
    failing (e.g. EBADF if the fd was double-closed) without masking the
    original exception — :func:`contextlib.suppress` swallows OSError
    only, which is the correct no-op for the double-close edge.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            # Convert to our named exception so callers don't have to
            # import the low-level `BlockingIOError` from the stdlib to
            # recognise the condition.
            raise LockBusyError(str(lock_path)) from exc
        try:
            yield fd
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


__all__ = ["LockBusyError", "flock_exclusive_nb"]
