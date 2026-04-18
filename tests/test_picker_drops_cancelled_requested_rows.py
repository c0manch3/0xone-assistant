"""Phase 6 fix-pack HIGH #1 (CR I-3 / devil H-3).

Before the fix, the picker's `run()` loop logged
`picker_skipping_cancelled` for each `cancel_requested=1 AND
status='requested'` row on every tick. Plan §3.6 promised
`recover_orphans` would sweep them via the 1-h stale bucket, but in
the interim a single cancelled row produces up to
3600 log lines (one per tick, for one hour). This is noise and obscures
real failures in the log stream.

Post-fix the picker calls `SubagentStore.drop_cancelled_request(id)`
on the first observation. The row transitions to `dropped` with
`finished_at` stamped; subsequent `list_pending_requests` calls
don't return it, so no more ticks touch it.

This test is focused on the store method AND the picker's single-
tick behaviour. The broader picker skip test in
`test_subagent_picker.py` covers the run-loop integration.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from assistant.state.db import apply_schema, connect
from assistant.subagent.store import SubagentStore


async def _mkstore(tmp_path: Path) -> SubagentStore:
    db = tmp_path / "d.db"
    conn = await connect(db)
    await apply_schema(conn)
    lock = asyncio.Lock()
    return SubagentStore(conn, lock=lock)


async def test_drop_cancelled_request_transitions_to_dropped(tmp_path: Path) -> None:
    """Happy path: `status='requested' AND cancel_requested=1` →
    `status='dropped'` with `finished_at` stamped."""
    store = await _mkstore(tmp_path)
    try:
        jid = await store.record_pending_request(
            agent_type="general",
            task_text="hi",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )
        # Pre-cancel the row.
        await store.set_cancel_requested(jid)
        dropped = await store.drop_cancelled_request(jid)
        assert dropped is True

        row = await store.get_by_id(jid)
        assert row is not None
        assert row.status == "dropped"
        assert row.finished_at is not None
        # cancel_requested stays as-is — it's audit metadata.
        assert row.cancel_requested is True
    finally:
        await store._conn.close()


async def test_drop_cancelled_request_noop_on_uncancelled_row(tmp_path: Path) -> None:
    """Row without `cancel_requested=1` must NOT be transitioned —
    this is the status-precondition guard."""
    store = await _mkstore(tmp_path)
    try:
        jid = await store.record_pending_request(
            agent_type="general",
            task_text="hi",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )
        dropped = await store.drop_cancelled_request(jid)
        assert dropped is False
        row = await store.get_by_id(jid)
        assert row is not None
        assert row.status == "requested"
    finally:
        await store._conn.close()


async def test_drop_cancelled_request_noop_on_started_row(tmp_path: Path) -> None:
    """Racy case: the picker picked the row up and transitioned it
    to `started` before the drop_cancelled UPDATE could land.
    `rowcount=0` → return False so the caller can log-and-continue."""
    store = await _mkstore(tmp_path)
    try:
        # Insert a started row with cancel_requested=1 directly.
        async with store._lock:
            cur = await store._conn.execute(
                "INSERT INTO subagent_jobs("
                "sdk_agent_id, agent_type, status, cancel_requested, "
                "callback_chat_id, spawned_by_kind) "
                "VALUES (?, 'general', 'started', 1, 42, 'user')",
                ("ag-raced",),
            )
            await store._conn.commit()
        assert cur.lastrowid is not None
        jid = int(cur.lastrowid)

        dropped = await store.drop_cancelled_request(jid)
        assert dropped is False
        row = await store.get_by_id(jid)
        assert row is not None
        assert row.status == "started"
    finally:
        await store._conn.close()
