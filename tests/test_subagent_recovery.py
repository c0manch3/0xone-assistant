"""Phase 6 / commit 8 — Daemon orphan recovery integration.

Simulates what `Daemon.start` does with `SubagentStore.recover_orphans`
against a DB pre-seeded with a mix of `started`/`requested` rows at
different ages. Verifies the split-notify accounting that the owner-
facing Telegram message consumes.

We don't spin up the full Daemon here (that requires OAuth + real
TelegramAdapter); the invariant under test is the store-level
transition matrix that the Daemon wires to the notify text.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from assistant.state.db import apply_schema, connect
from assistant.subagent.store import SubagentStore


async def _mkstore(tmp_path: Path) -> tuple[SubagentStore, Path]:
    db = tmp_path / "r.db"
    conn = await connect(db)
    await apply_schema(conn)
    lock = asyncio.Lock()
    store = SubagentStore(conn, lock=lock)
    return store, db


def _iso_minus(h: int) -> str:
    return (datetime.now(UTC) - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_recovery_interrupted_started_unfinished(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        # Seed a started row via the store API — this is the canonical path.
        await store.record_started(
            sdk_agent_id="ag-started",
            agent_type="general",
            parent_session_id=None,
            callback_chat_id=42,
            spawned_by_kind="user",
            spawned_by_ref=None,
        )
        counts = await store.recover_orphans()
        assert counts == {"interrupted": 1, "dropped": 0}

        job = await store.get_by_agent_id("ag-started")
        assert job is not None
        assert job.status == "interrupted"
        assert job.finished_at is not None
    finally:
        await store._conn.close()


async def test_recovery_dropped_requested_stale(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        # Stale requested row (>1h old).
        async with store._lock:
            await store._conn.execute(
                "INSERT INTO subagent_jobs("
                "agent_type, task_text, status, callback_chat_id, "
                "spawned_by_kind, created_at) "
                "VALUES (?, ?, 'requested', ?, 'cli', ?)",
                ("general", "old", 42, _iso_minus(3)),
            )
            await store._conn.commit()
        counts = await store.recover_orphans(stale_requested_after_s=3600)
        assert counts == {"interrupted": 0, "dropped": 1}
    finally:
        await store._conn.close()


async def test_recovery_leaves_fresh_requested_alone(tmp_path: Path) -> None:
    store, _ = await _mkstore(tmp_path)
    try:
        jid = await store.record_pending_request(
            agent_type="general",
            task_text="fresh",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )
        counts = await store.recover_orphans(stale_requested_after_s=3600)
        assert counts == {"interrupted": 0, "dropped": 0}
        row = await store.get_by_id(jid)
        assert row is not None
        assert row.status == "requested"
    finally:
        await store._conn.close()


async def test_recovery_is_idempotent(tmp_path: Path) -> None:
    """Running recover_orphans twice MUST NOT double-count or re-
    transition already-terminal rows."""
    store, _ = await _mkstore(tmp_path)
    try:
        await store.record_started(
            sdk_agent_id="ag-1",
            agent_type="general",
            parent_session_id=None,
            callback_chat_id=42,
            spawned_by_kind="user",
            spawned_by_ref=None,
        )
        first = await store.recover_orphans()
        second = await store.recover_orphans()
        assert first == {"interrupted": 1, "dropped": 0}
        assert second == {"interrupted": 0, "dropped": 0}
    finally:
        await store._conn.close()


async def test_recovery_runs_before_fresh_writes_can_race(tmp_path: Path) -> None:
    """Invariant: recover_orphans runs AT start, BEFORE the picker or
    bridge accepts new turns. We simulate the invariant here by calling
    recover_orphans first; any row inserted afterwards is untouched."""
    store, _ = await _mkstore(tmp_path)
    try:
        # Seed a stale row.
        async with store._lock:
            await store._conn.execute(
                "INSERT INTO subagent_jobs("
                "agent_type, task_text, status, callback_chat_id, "
                "spawned_by_kind, created_at) "
                "VALUES (?, ?, 'requested', ?, 'cli', ?)",
                ("general", "stale", 42, _iso_minus(3)),
            )
            await store._conn.commit()
        counts = await store.recover_orphans()
        assert counts["dropped"] == 1

        # Fresh insert after recovery — must be untouched by a subsequent
        # recovery call.
        fresh_id = await store.record_pending_request(
            agent_type="general",
            task_text="new",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )
        again = await store.recover_orphans()
        assert again == {"interrupted": 0, "dropped": 0}
        fresh = await store.get_by_id(fresh_id)
        assert fresh is not None
        assert fresh.status == "requested"
    finally:
        await store._conn.close()


async def test_recovery_defensive_drop_started_null_agent_id(tmp_path: Path) -> None:
    """Defensive: a 'started' row with NULL sdk_agent_id is a schema-
    invariant violation but recover_orphans must transition it out of
    running-state so the picker/hooks don't crash on it."""
    store, _ = await _mkstore(tmp_path)
    try:
        async with store._lock:
            await store._conn.execute(
                "INSERT INTO subagent_jobs("
                "agent_type, status, callback_chat_id, spawned_by_kind) "
                "VALUES (?, 'started', ?, ?)",
                ("general", 42, "user"),
            )
            await store._conn.commit()
        counts = await store.recover_orphans()
        # Defensive branch → dropped (distinguishable from interrupted).
        assert counts["dropped"] == 1
        assert counts["interrupted"] == 0
    finally:
        await store._conn.close()
