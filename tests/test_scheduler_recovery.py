"""Phase 5 / commit 7b — daemon-level recovery regressions.

Four blocks:
  1. `_acquire_pid_lock_or_exit` — second daemon on same data_dir exits 0.
  2. `clean_slate_sent` — called at boot before dispatcher starts; affects
     only `status='sent'` rows.
  3. `revert_stuck_sent` runtime sweep — honours `exclude_ids` AND timeout
     (re-exercises the B-W2-1 contract at the store level from the daemon
     angle).
  4. Pidfile mode — `0o600` per wave-2 N-W2-5.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import stat
from pathlib import Path
from typing import Any

import pytest

import assistant.main as main_mod
from assistant.config import ClaudeSettings, SchedulerSettings, Settings
from assistant.main import Daemon
from assistant.scheduler.store import SchedulerStore
from assistant.state.db import apply_schema, connect


class _DummyAdapter:
    def __init__(self, settings: Any, *, dedup_ledger: Any = None) -> None:
        # Phase 7 fix-pack C1: daemon threads the shared ledger.
        del settings, dedup_ledger
        self._handler: Any = None
        self.sends: list[tuple[int, str]] = []

    def set_handler(self, handler: Any) -> None:
        self._handler = handler

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sends.append((chat_id, text))


async def _noop_preflight(log: Any) -> None:
    del log


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        scheduler=SchedulerSettings(),
    )


# ---------------------------------------------------------------- pidfile


def test_pid_lock_second_daemon_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wave-2 B-W2-2 regression guard: the existing flock file blocks a
    second `_acquire_pid_lock_or_exit`. Simulated by pre-locking the file
    from the test process itself (spike S-4 case 5 confirmed macOS blocks
    same-process-different-fd)."""
    (tmp_path / "data" / "run").mkdir(parents=True, exist_ok=True)
    pid_path = tmp_path / "data" / "run" / "daemon.pid"
    holder_fd = os.open(str(pid_path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        monkeypatch.setattr(main_mod, "_preflight_claude_cli", _noop_preflight)
        monkeypatch.setattr(main_mod, "ensure_skills_symlink", lambda root: None)
        monkeypatch.setattr(main_mod, "TelegramAdapter", _DummyAdapter)

        daemon = Daemon(_settings(tmp_path))
        with pytest.raises(SystemExit) as excinfo:
            daemon._acquire_pid_lock_or_exit()
        assert excinfo.value.code == 0
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_pid_lock_writes_pid_and_0o600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "_preflight_claude_cli", _noop_preflight)
    daemon = Daemon(_settings(tmp_path))
    daemon._acquire_pid_lock_or_exit()
    try:
        pid_path = tmp_path / "data" / "run" / "daemon.pid"
        content = pid_path.read_text().strip()
        assert int(content) == os.getpid()
        mode = stat.S_IMODE(pid_path.stat().st_mode)
        # Wave-2 N-W2-5: 0o600 exact.
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
    finally:
        assert daemon._pid_fd is not None
        os.close(daemon._pid_fd)


# ---------------------------------------------------------------- clean-slate


async def test_clean_slate_sent_reverts_only_sent(tmp_path: Path) -> None:
    """Daemon.start() runs clean_slate_sent BEFORE the dispatcher starts.
    Seed a mix of statuses and verify the SchedulerStore method behaves as
    documented."""
    db = tmp_path / "recovery.db"
    conn = await connect(db)
    try:
        await apply_schema(conn)
        lock = asyncio.Lock()
        store = SchedulerStore(conn, lock)

        # Seed via direct SQL to control statuses exactly.
        await store.insert_schedule(cron="0 9 * * *", prompt="x", tz="UTC")
        for idx, status in enumerate(("pending", "sent", "acked", "dead", "sent")):
            await conn.execute(
                "INSERT INTO triggers(schedule_id, prompt, scheduled_for, status, "
                "sent_at) VALUES (?, ?, ?, ?, ?)",
                (1, "x", f"2026-04-15T09:{idx:02d}:00Z", status, "2020-01-01T00:00:00Z"),
            )
        await conn.commit()

        reverted = await store.clean_slate_sent()
        assert reverted == 2  # the two 'sent' rows

        async with conn.execute("SELECT status FROM triggers ORDER BY id ASC") as cur:
            rows = [r[0] for r in await cur.fetchall()]
        assert rows == ["pending", "pending", "acked", "dead", "pending"]
    finally:
        await conn.close()


# ---------------------------------------------------------------- revert sweep


async def test_revert_stuck_sent_respects_exclude_ids(tmp_path: Path) -> None:
    """Re-exercises the B-W2-1 contract from the store / recovery angle:
    `exclude_ids={inflight_id}` leaves the row alone even past timeout."""
    db = tmp_path / "recovery.db"
    conn = await connect(db)
    try:
        await apply_schema(conn)
        lock = asyncio.Lock()
        store = SchedulerStore(conn, lock)
        sid = await store.insert_schedule(cron="0 9 * * *", prompt="x", tz="UTC")
        # Two triggers, both stuck in 'sent'.
        for minute in ("2026-04-15T09:00:00Z", "2026-04-15T09:05:00Z"):
            await conn.execute(
                "INSERT INTO triggers(schedule_id, prompt, scheduled_for, status, "
                "sent_at) VALUES (?, ?, ?, 'sent', '2020-01-01T00:00:00Z')",
                (sid, "x", minute),
            )
        await conn.commit()

        # Exclude trigger #1 → only #2 reverted.
        reverted = await store.revert_stuck_sent(timeout_s=360, exclude_ids={1})
        assert reverted == 1

        async with conn.execute("SELECT id, status FROM triggers ORDER BY id ASC") as cur:
            rows = await cur.fetchall()
        assert rows[0] == (1, "sent")
        assert rows[1] == (2, "pending")
    finally:
        await conn.close()
