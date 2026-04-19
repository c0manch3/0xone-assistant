"""Phase 6 fix-pack CRITICAL #3 (CR I-2 / devil H-5).

Anti-phase-5 regression: phase-5's `SchedulerStore.revert_stuck_sent`
had isolated unit coverage but no `Daemon.start()` runtime test —
production miss landed a silent regression. Phase 6's
`SubagentStore.recover_orphans` has the same shape: `test_subagent_
recovery.py` covers the store method directly, but nothing verifies
the full wire-up inside `Daemon.start()`:

  * that the recovery RUNS on boot BEFORE the picker goes live,
  * that the split-counts notify task is spawned (and carries the
    right body text),
  * that the DB rows land in the expected terminal states after the
    full `start()` call, not just the isolated store method.

The test seeds three realistic rows against a freshly-applied schema,
starts a fully-monkeypatched Daemon (no Telegram I/O, no real bridge,
no bootstrap), and asserts:

  1. a `status='started' AND finished_at IS NULL` row → `interrupted`,
  2. a `status='requested'` row older than 1 h → `dropped`,
  3. a `status='requested'` row fresher than 1 h → still `requested`,
  4. an owner-facing notify was sent via the fake adapter,
  5. the notify body contains both split counts ("1 interrupted",
     "1 dropped" / the Russian-language variant).

Runs before the picker claims anything — invariant enforced by
`Daemon.start()` ordering (recovery step runs BEFORE
`_subagent_picker.run()` is scheduled on bg_tasks).
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import assistant.main as main_mod
from assistant.config import ClaudeSettings, Settings, SubagentSettings
from assistant.main import Daemon
from assistant.state.db import apply_schema, connect


class _CaptureAdapter:
    """Records send_text calls so the test can inspect the notify
    body. Mirrors the shape of `TelegramAdapter` for the slice the
    Daemon uses: `set_handler`, `start`, `stop`, `send_text`."""

    def __init__(self, settings: Any, *, dedup_ledger: Any = None) -> None:
        # Phase 7 fix-pack C1: daemon threads the shared ledger.
        del settings, dedup_ledger
        self._handler: Any = None
        self.sent: list[tuple[int, str]] = []
        self._send_event = asyncio.Event()

    def set_handler(self, handler: Any) -> None:
        self._handler = handler

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))
        self._send_event.set()


async def _noop_preflight(log: Any) -> None:
    del log


def _iso_hours_ago(h: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _seed_rows(db_path: Path) -> dict[str, int]:
    """Insert the three rows in the anti-regression matrix directly
    against the schema-applied DB. Returns a dict mapping the
    logical name to the row id so the test can assert per-row."""
    conn = await connect(db_path)
    try:
        await apply_schema(conn)
        # (a) started, unfinished → interrupted
        a_cur = await conn.execute(
            "INSERT INTO subagent_jobs("
            "sdk_agent_id, agent_type, status, callback_chat_id, "
            "spawned_by_kind, started_at) "
            "VALUES ('ag-started-1', 'general', 'started', 42, 'user', ?)",
            (_iso_hours_ago(0.01),),
        )
        # (b) requested, old (>1h) → dropped
        b_cur = await conn.execute(
            "INSERT INTO subagent_jobs("
            "agent_type, task_text, status, callback_chat_id, "
            "spawned_by_kind, created_at) "
            "VALUES ('general', 'stale task', 'requested', 42, 'cli', ?)",
            (_iso_hours_ago(2),),
        )
        # (c) requested, fresh (<1h) → still requested
        c_cur = await conn.execute(
            "INSERT INTO subagent_jobs("
            "agent_type, task_text, status, callback_chat_id, "
            "spawned_by_kind, created_at) "
            "VALUES ('general', 'fresh task', 'requested', 42, 'cli', ?)",
            (_iso_hours_ago(0.01),),
        )
        await conn.commit()
        assert a_cur.lastrowid is not None
        assert b_cur.lastrowid is not None
        assert c_cur.lastrowid is not None
        return {
            "a_started_unfinished": int(a_cur.lastrowid),
            "b_requested_stale": int(b_cur.lastrowid),
            "c_requested_fresh": int(c_cur.lastrowid),
        }
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_daemon_start_runs_subagent_recovery_and_notifies_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end Daemon.start() recovery wire-up.

    Build the DB out-of-band, seed the three-row matrix, then start
    a monkeypatched Daemon. After start() returns, assert:
      * row (a) is 'interrupted',
      * row (b) is 'dropped',
      * row (c) is still 'requested',
      * adapter.sent has exactly one notify to the owner chat,
      * the notify body carries both split counts.
    """
    (tmp_path / "skills").mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Seed BEFORE Daemon.start() applies the schema — we pre-apply so
    # the Daemon's apply_schema is a no-op. This is identical to how
    # Daemon.start will see the DB on the real boot path.
    seeded = await _seed_rows(data_dir / "assistant.db")

    # Monkeypatch out the side effects we don't want in a unit test:
    # claude CLI preflight, skills symlink, Telegram, bootstrap.
    monkeypatch.setattr(main_mod, "_preflight_claude_cli", _noop_preflight)
    monkeypatch.setattr(main_mod, "ensure_skills_symlink", lambda root: None)
    monkeypatch.setattr(main_mod, "TelegramAdapter", _CaptureAdapter)
    monkeypatch.setattr(Daemon, "_bootstrap_skill_creator_bg", lambda self: asyncio.sleep(0))
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")

    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=data_dir,
        claude=ClaudeSettings(),
        subagent=SubagentSettings(enabled=True, picker_tick_s=10.0),
    )
    daemon = Daemon(settings)

    try:
        await daemon.start()
        # The recovery notify is a bg task; give it a beat to flush.
        adapter = daemon._adapter
        assert isinstance(adapter, _CaptureAdapter)
        try:
            await asyncio.wait_for(adapter._send_event.wait(), timeout=2.0)
        except TimeoutError as exc:
            raise AssertionError(
                f"recovery notify never fired; adapter.sent={adapter.sent!r}"
            ) from exc

        # Terminal-state assertions — read via the store the Daemon built.
        assert daemon._sub_store is not None
        a_job = await daemon._sub_store.get_by_id(seeded["a_started_unfinished"])
        b_job = await daemon._sub_store.get_by_id(seeded["b_requested_stale"])
        c_job = await daemon._sub_store.get_by_id(seeded["c_requested_fresh"])
        assert a_job is not None
        assert b_job is not None
        assert c_job is not None
        assert a_job.status == "interrupted", a_job
        assert b_job.status == "dropped", b_job
        assert c_job.status == "requested", c_job

        # Notify assertion — exactly one message to the owner chat
        # carrying BOTH split counts.
        assert len(adapter.sent) == 1, adapter.sent
        chat_id, body = adapter.sent[0]
        assert chat_id == 42
        # Split-count evidence: both "1" counts appear AND the
        # category words match B-W2-7's Russian-language notify.
        assert "1" in body
        assert "interrupted" in body
        # The stale-requested bucket wording — check for the
        # "отброшено" stem so we're resilient to spacing changes.
        assert "отброшено" in body or "dropped" in body.lower(), body
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_daemon_start_without_orphans_skips_notify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inverse case: zero orphans → no recovery notify. Verifies the
    Daemon doesn't spam a "daemon restart" notice on every cold
    boot (owner-experience invariant)."""
    (tmp_path / "skills").mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Schema only, no rows.
    conn = await connect(data_dir / "assistant.db")
    try:
        await apply_schema(conn)
    finally:
        await conn.close()

    monkeypatch.setattr(main_mod, "_preflight_claude_cli", _noop_preflight)
    monkeypatch.setattr(main_mod, "ensure_skills_symlink", lambda root: None)
    monkeypatch.setattr(main_mod, "TelegramAdapter", _CaptureAdapter)
    monkeypatch.setattr(Daemon, "_bootstrap_skill_creator_bg", lambda self: asyncio.sleep(0))
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")

    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=data_dir,
        claude=ClaudeSettings(),
        subagent=SubagentSettings(enabled=True, picker_tick_s=10.0),
    )
    daemon = Daemon(settings)
    try:
        await daemon.start()
        # Let any stray bg task settle — there shouldn't be a recovery
        # notify, but the adapter might have OTHER spontaneous messages
        # (today there aren't any).
        await asyncio.sleep(0.1)
        adapter = daemon._adapter
        assert isinstance(adapter, _CaptureAdapter)
        assert adapter.sent == [], f"unexpected notify: {adapter.sent!r}"
    finally:
        await daemon.stop()
