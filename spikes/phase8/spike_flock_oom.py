#!/usr/bin/env python3
"""Phase 8 R-4: verify kernel auto-releases `fcntl.flock` on SIGKILL.

Ship a child process that:
  * opens a lock file,
  * acquires `fcntl.flock(LOCK_EX | LOCK_NB)`,
  * signals parent (writes a marker file or prints a line),
  * sleeps 30 s.

Parent:
  1. waits for child's ready marker,
  2. os.kill(pid, SIGKILL),
  3. os.waitpid,
  4. attempts its own `LOCK_EX | LOCK_NB`,
  5. asserts immediate success (no BlockingIOError).

This emulates an OOM-killed subprocess holding the vault-commit-push
flock. Invariant I-8.2 says kernel releases on close(fd) when the
process dies. macOS (Darwin 24) confirmation.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CHILD_SRC = """
import fcntl, os, sys, time
lock_path = sys.argv[1]
ready_path = sys.argv[2]
fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
# Signal parent we have the lock.
with open(ready_path, 'w') as f:
    f.write(str(os.getpid()))
time.sleep(30)
"""


def main() -> int:
    report: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="spike_flock_oom_") as td:
        lock_path = Path(td) / "lock"
        lock_path.touch()
        ready_path = Path(td) / "ready"
        proc = subprocess.Popen(
            [sys.executable, "-c", CHILD_SRC, str(lock_path), str(ready_path)]
        )
        try:
            # Wait up to 5 s for child to signal ready.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not ready_path.exists():
                time.sleep(0.05)
            if not ready_path.exists():
                report["phase"] = "child_never_acquired"
                proc.kill()
                proc.wait(timeout=5)
                return 1
            child_pid = int(ready_path.read_text())

            # Parent tries to acquire — MUST fail (NB).
            parent_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                report["before_kill_acquire"] = "UNEXPECTED_SUCCESS"
                fcntl.flock(parent_fd, fcntl.LOCK_UN)
            except BlockingIOError:
                report["before_kill_acquire"] = "expected_BlockingIOError"

            # Kill child.
            os.kill(child_pid, signal.SIGKILL)
            os.waitpid(child_pid, 0)

            # Immediately retry.
            t0 = time.monotonic()
            try:
                fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elapsed_ms = (time.monotonic() - t0) * 1000
                report["after_kill_acquire"] = {
                    "acquired": True,
                    "elapsed_ms": elapsed_ms,
                }
                fcntl.flock(parent_fd, fcntl.LOCK_UN)
            except BlockingIOError:
                report["after_kill_acquire"] = {"acquired": False}

            os.close(parent_fd)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    out_path = Path(__file__).with_name("spike_flock_oom_report.json")
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
