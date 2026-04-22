"""Fix 6 / H6-W3 — only one ``Daemon`` process per ``data_dir`` can hold
the advisory lock on ``<data_dir>/.daemon.pid``.

Two concurrent daemons against the same vault + audit log + sqlite
``assistant.db`` race on the between-write windows of the vault flock
and double-write audit entries. ``fcntl.flock(LOCK_EX | LOCK_NB)`` on
a pidfile is the idiomatic POSIX guard.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from assistant.main import _acquire_singleton_lock


def test_singleton_lock_held_blocks_second_call(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fd = _acquire_singleton_lock(data_dir)
    try:
        # Pidfile exists and stores our pid.
        assert (data_dir / ".daemon.pid").is_file()
        with (data_dir / ".daemon.pid").open(encoding="utf-8") as fh:
            pid_in_file = fh.read().strip()
        assert pid_in_file == str(os.getpid())

        # Second attempt exits cleanly (sys.exit(3)).
        with pytest.raises(SystemExit) as excinfo:
            _acquire_singleton_lock(data_dir)
        assert excinfo.value.code == 3
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_singleton_lock_released_allows_reacquire(tmp_path: Path) -> None:
    """After the first holder releases the lock, the next caller must
    succeed without hitting the ``sys.exit(3)`` path.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    first = _acquire_singleton_lock(data_dir)
    fcntl.flock(first, fcntl.LOCK_UN)
    os.close(first)

    second = _acquire_singleton_lock(data_dir)
    try:
        # Pidfile now stores our pid (replacing the prior one).
        assert (data_dir / ".daemon.pid").is_file()
        with (data_dir / ".daemon.pid").open(encoding="utf-8") as fh:
            pid_in_file = fh.read().strip()
        assert pid_in_file == str(os.getpid())
    finally:
        fcntl.flock(second, fcntl.LOCK_UN)
        os.close(second)


def test_singleton_lock_pidfile_permissions_owner_only(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fd = _acquire_singleton_lock(data_dir)
    try:
        mode = (data_dir / ".daemon.pid").stat().st_mode & 0o777
        assert (mode & 0o077) == 0, f"pidfile mode {oct(mode)} must be 0o600"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
