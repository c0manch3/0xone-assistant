"""S3: `_probe_lock_semantics` + `_ensure_lock_semantics_once` behaviour."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from _memlib import fts as fts_mod


@pytest.fixture(autouse=True)
def _reset_probe_cache() -> None:
    fts_mod._reset_lock_probe_cache()


def test_probe_passes_on_local_fs(tmp_path: Path) -> None:
    lock = tmp_path / "test.lock"
    assert fts_mod._probe_lock_semantics(lock) is True


def test_probe_busy_fd1_returns_true(tmp_path: Path) -> None:
    """B-CRIT-2: probe handles a concurrent lock holder gracefully.

    If another memory CLI is mid-write when the probe runs, the probe's
    first acquire raises BlockingIOError. Treat that as "flock works"
    (the block itself is evidence) rather than propagating the exception.
    """
    lock_path = tmp_path / "busy.lock"
    holder_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert fts_mod._probe_lock_semantics(lock_path) is True
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_ensure_does_not_explode_when_probe_busy(tmp_path: Path) -> None:
    """Top-level `_ensure_lock_semantics_once` must not raise when busy."""
    lock_path = tmp_path / "idx.db.lock"
    holder_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Must return cleanly (no SystemExit, no uncaught BlockingIOError).
        fts_mod._ensure_lock_semantics_once(tmp_path / "idx.db")
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_ensure_exits_5_on_noop_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fts_mod, "_probe_lock_semantics", lambda _p: False)
    idx = tmp_path / "idx.db"
    with pytest.raises(SystemExit) as exc:
        fts_mod._ensure_lock_semantics_once(idx)
    assert exc.value.code == 5


def test_ensure_skip_env_bypass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Probe would fail, but the env override keeps us going.
    monkeypatch.setattr(fts_mod, "_probe_lock_semantics", lambda _p: False)
    monkeypatch.setenv("ASSISTANT_SKIP_LOCK_PROBE", "1")
    idx = tmp_path / "idx.db"
    # Must not raise.
    fts_mod._ensure_lock_semantics_once(idx)


def test_ensure_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []

    def spy(p: Path) -> bool:
        calls.append(p)
        return True

    monkeypatch.setattr(fts_mod, "_probe_lock_semantics", spy)
    idx = tmp_path / "idx.db"
    fts_mod._ensure_lock_semantics_once(idx)
    fts_mod._ensure_lock_semantics_once(idx)
    assert len(calls) == 1, "second call should have been served from cache"
