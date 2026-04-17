"""Spike S-4: fcntl.flock LOCK_EX|LOCK_NB semantics on macOS + Linux.

Question: Plan §1.13 uses `fcntl.flock(fd, LOCK_EX|LOCK_NB)` on
`<data_dir>/run/daemon.pid`. Is the lock released on close/exit?
Released on SIGKILL? Does the second process get a clean BlockingIOError?

Pass criterion:
  * second flock on the same file raises BlockingIOError (never hangs);
  * SIGKILL on the holder releases the lock (next process acquires);
  * no stale-lock hangs.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _holder_script(lock_path: Path, hold_seconds: float) -> str:
    return f"""
import fcntl, time, os, sys
fd = os.open({str(lock_path)!r}, os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.stderr.write('holder: already-locked\\n')
    sys.exit(1)
os.write(fd, f'pid={{os.getpid()}}\\n'.encode())
sys.stdout.write('READY\\n')
sys.stdout.flush()
time.sleep({hold_seconds})
"""


def try_acquire(lock_path: Path) -> tuple[bool, str | None]:
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True, None
    except OSError as exc:
        return False, f"{type(exc).__name__}:{exc.errno}:{errno.errorcode.get(exc.errno, '?')}"
    finally:
        os.close(fd)


def run() -> dict:
    results: dict = {"platform": sys.platform}
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "daemon.pid"
        lock_path.touch()

        # --- Case 1: holder lives 5s; during that, we try LOCK_NB -> must fail.
        proc = subprocess.Popen(
            [sys.executable, "-c", _holder_script(lock_path, 5.0)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        # Wait for the holder to print READY.
        line = proc.stdout.readline().decode().strip()
        results["holder_ready_line"] = line
        time.sleep(0.2)  # ensure flock has settled in the kernel

        acquired, err = try_acquire(lock_path)
        results["case1_second_attempt_acquired"] = acquired
        results["case1_second_attempt_err"] = err

        # --- Case 2: SIGKILL the holder; lock must be released on exit.
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
        time.sleep(0.2)

        acquired, err = try_acquire(lock_path)
        results["case2_post_sigkill_acquired"] = acquired
        results["case2_post_sigkill_err"] = err

        # --- Case 3: holder exits normally; lock released.
        proc2 = subprocess.Popen(
            [sys.executable, "-c", _holder_script(lock_path, 0.2)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc2.stdout is not None
        proc2.stdout.readline()  # READY
        time.sleep(0.3)  # holder has exited by now
        proc2.wait(timeout=5)

        acquired, err = try_acquire(lock_path)
        results["case3_post_clean_exit_acquired"] = acquired
        results["case3_post_clean_exit_err"] = err

        # --- Case 4: same process acquires twice on the same fd -> idempotent.
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Acquire the lock again on the same fd.
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                results["case4_same_fd_reacquire"] = "ok"
            except OSError as exc:
                results["case4_same_fd_reacquire"] = f"err:{exc!r}"
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

        # --- Case 5: two file descriptors in the SAME process, same path.
        # POSIX says the second flock(EX|NB) will block/fail because flock
        # locks are per-file (macOS) or per-open-file-description. This is
        # the "daemon restart leaked fd" scenario.
        fd1 = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd2 = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            results["case5_second_fd_same_proc"] = "acquired"
        except OSError as exc:
            results["case5_second_fd_same_proc"] = f"err:{exc!r}"
        finally:
            fcntl.flock(fd1, fcntl.LOCK_UN)
            os.close(fd1)
            os.close(fd2)

    # Summary pass/fail
    results["pass"] = (
        results.get("case1_second_attempt_acquired") is False
        and "BlockingIOError" in (results.get("case1_second_attempt_err") or "")
        and results.get("case2_post_sigkill_acquired") is True
        and results.get("case3_post_clean_exit_acquired") is True
    )
    return results


def main() -> None:
    r = run()
    print(json.dumps(r, indent=2, default=str))


if __name__ == "__main__":
    main()
