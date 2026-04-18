"""Phase 6 / commit 2 — SubagentStore CRUD, state machine, recovery.

Covers:
  * `record_pending_request` returns an auto-increment id + row has
    `status='requested'` and `sdk_agent_id IS NULL`.
  * `record_started` (native-Task path) inserts with `status='started'`;
    duplicate `sdk_agent_id` hits partial-UNIQUE and returns the
    existing id (no raise — pitfall #9).
  * `update_sdk_agent_id_for_claimed_request` transitions
    `requested → started` ONLY when status precondition matches; racing
    path (already started) returns False with skew log.
  * `record_finished` transitions `started → terminal` ONLY from
    `started`; duplicate or out-of-order Stop hooks are no-ops.
  * `set_cancel_requested`: new flag flips for non-terminal rows;
    terminal returns `{"already_terminal": ...}`.
  * `is_cancel_requested`: returns False for missing row.
  * `recover_orphans` splits `interrupted` (started+unfinished) from
    `dropped` (requested+>1h stale).
  * `list_pending_requests` returns only `status='requested' AND
    sdk_agent_id IS NULL`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from assistant.state.db import apply_schema, connect
from assistant.subagent.store import SubagentStore


async def _mkstore(tmp_path: Path) -> tuple[SubagentStore, aiosqlite.Connection]:
    conn = await connect(tmp_path / "sub.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    return SubagentStore(conn, lock=lock), conn


# ---------------------------------------------------------------- pending


async def test_record_pending_request_returns_id_and_inserts_null_agent(
    tmp_path: Path,
) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        jid = await store.record_pending_request(
            agent_type="general",
            task_text="write a poem",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )
        assert jid >= 1
        job = await store.get_by_id(jid)
        assert job is not None
        assert job.agent_type == "general"
        assert job.task_text == "write a poem"
        assert job.callback_chat_id == 42
        assert job.spawned_by_kind == "cli"
        assert job.status == "requested"
        assert job.sdk_agent_id is None
        assert job.cancel_requested is False
    finally:
        await conn.close()


async def test_list_pending_returns_requested_null_agent_only(tmp_path: Path) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        j1 = await store.record_pending_request(
            agent_type="general",
            task_text="a",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )
        await store.record_pending_request(
            agent_type="worker",
            task_text="b",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )
        # A started row (native-Task) must NOT appear in pending list.
        await store.record_started(
            sdk_agent_id="agent-started-1",
            agent_type="researcher",
            parent_session_id="sess-main",
            callback_chat_id=42,
            spawned_by_kind="user",
            spawned_by_ref=None,
        )
        pending = await store.list_pending_requests()
        assert len(pending) == 2
        # Oldest first.
        assert pending[0].id == j1
        for p in pending:
            assert p.status == "requested"
            assert p.sdk_agent_id is None
    finally:
        await conn.close()


# ---------------------------------------------------------------- started


async def test_record_started_native_task_path_inserts_row(tmp_path: Path) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        jid = await store.record_started(
            sdk_agent_id="agent-A",
            agent_type="general",
            parent_session_id="sess-parent",
            callback_chat_id=42,
            spawned_by_kind="user",
            spawned_by_ref=None,
        )
        job = await store.get_by_id(jid)
        assert job is not None
        assert job.sdk_agent_id == "agent-A"
        assert job.status == "started"
        assert job.started_at is not None
        assert job.finished_at is None
        assert job.parent_session_id == "sess-parent"
    finally:
        await conn.close()


async def test_record_started_duplicate_sdk_agent_id_returns_existing(tmp_path: Path) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        first = await store.record_started(
            sdk_agent_id="agent-dup",
            agent_type="general",
            parent_session_id=None,
            callback_chat_id=42,
            spawned_by_kind="user",
            spawned_by_ref=None,
        )
        # Second call with the SAME sdk_agent_id must not raise.
        second = await store.record_started(
            sdk_agent_id="agent-dup",
            agent_type="worker",  # intentionally different — we expect first to win
            parent_session_id=None,
            callback_chat_id=42,
            spawned_by_kind="user",
            spawned_by_ref=None,
        )
        assert first == second
        job = await store.get_by_id(first)
        assert job is not None
        # Original row unchanged.
        assert job.agent_type == "general"
    finally:
        await conn.close()


# ---------------------------------------------------------------- update_sdk_agent_id


async def test_update_sdk_agent_id_for_claimed_request_flips_requested_to_started(
    tmp_path: Path,
) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        jid = await store.record_pending_request(
            agent_type="researcher",
            task_text="research x",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )
        ok = await store.update_sdk_agent_id_for_claimed_request(
            job_id=jid,
            sdk_agent_id="agent-B",
            parent_session_id="sess-P",
        )
        assert ok is True
        job = await store.get_by_id(jid)
        assert job is not None
        assert job.sdk_agent_id == "agent-B"
        assert job.parent_session_id == "sess-P"
        assert job.status == "started"
        assert job.started_at is not None
    finally:
        await conn.close()


async def test_update_sdk_agent_id_noop_when_row_already_started(tmp_path: Path) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        jid = await store.record_pending_request(
            agent_type="general",
            task_text="x",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )
        await store.update_sdk_agent_id_for_claimed_request(
            job_id=jid,
            sdk_agent_id="agent-first",
            parent_session_id=None,
        )
        # Second patch attempt (simulating duplicate Start) returns False.
        ok = await store.update_sdk_agent_id_for_claimed_request(
            job_id=jid,
            sdk_agent_id="agent-second",
            parent_session_id=None,
        )
        assert ok is False
        # Row retains the first agent_id.
        job = await store.get_by_id(jid)
        assert job is not None
        assert job.sdk_agent_id == "agent-first"
    finally:
        await conn.close()


# ---------------------------------------------------------------- finished


async def test_record_finished_transitions_only_from_started(tmp_path: Path) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        await store.record_started(
            sdk_agent_id="agent-X",
            agent_type="general",
            parent_session_id=None,
            callback_chat_id=42,
            spawned_by_kind="user",
            spawned_by_ref=None,
        )
        await store.record_finished(
            sdk_agent_id="agent-X",
            status="completed",
            result_summary="done",
            transcript_path="/tmp/t",
            sdk_session_id="sess-child",
        )
        job = await store.get_by_agent_id("agent-X")
        assert job is not None
        assert job.status == "completed"
        assert job.finished_at is not None
        assert job.result_summary == "done"
        assert job.sdk_session_id == "sess-child"
    finally:
        await conn.close()


async def test_record_finished_noop_when_not_started(tmp_path: Path) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        await store.record_started(
            sdk_agent_id="agent-Y",
            agent_type="general",
            parent_session_id=None,
            callback_chat_id=42,
            spawned_by_kind="user",
            spawned_by_ref=None,
        )
        await store.record_finished(
            sdk_agent_id="agent-Y",
            status="completed",
            result_summary="first",
            transcript_path=None,
            sdk_session_id=None,
        )
        # Duplicate Stop hook — must be a no-op, not raise.
        await store.record_finished(
            sdk_agent_id="agent-Y",
            status="failed",
            result_summary="second should not land",
            transcript_path=None,
            sdk_session_id=None,
        )
        job = await store.get_by_agent_id("agent-Y")
        assert job is not None
        # First write wins.
        assert job.status == "completed"
        assert job.result_summary == "first"
    finally:
        await conn.close()


async def test_record_finished_rejects_non_terminal_status(tmp_path: Path) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        await store.record_started(
            sdk_agent_id="agent-Z",
            agent_type="general",
            parent_session_id=None,
            callback_chat_id=42,
            spawned_by_kind="user",
            spawned_by_ref=None,
        )
        with pytest.raises(ValueError):
            await store.record_finished(
                sdk_agent_id="agent-Z",
                status="started",  # not a terminal status
                result_summary=None,
                transcript_path=None,
                sdk_session_id=None,
            )
    finally:
        await conn.close()


# ---------------------------------------------------------------- cancel


async def test_set_cancel_requested_on_requested_row(tmp_path: Path) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        jid = await store.record_pending_request(
            agent_type="general",
            task_text="x",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )
        out = await store.set_cancel_requested(jid)
        assert out.get("cancel_requested") is True
        assert out.get("previous_status") == "requested"
        # Flag-read by sdk_agent_id only works once the row has one. For
        # requested rows the CLI path uses the job_id; we verify the
        # column flipped directly.
        job = await store.get_by_id(jid)
        assert job is not None
        assert job.cancel_requested is True
    finally:
        await conn.close()


async def test_set_cancel_requested_on_started_row(tmp_path: Path) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        jid = await store.record_started(
            sdk_agent_id="agent-c1",
            agent_type="general",
            parent_session_id=None,
            callback_chat_id=42,
            spawned_by_kind="user",
            spawned_by_ref=None,
        )
        out = await store.set_cancel_requested(jid)
        assert out.get("cancel_requested") is True
        assert out.get("previous_status") == "started"
        assert await store.is_cancel_requested("agent-c1") is True
    finally:
        await conn.close()


async def test_set_cancel_requested_already_terminal_returns_marker(tmp_path: Path) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        jid = await store.record_started(
            sdk_agent_id="agent-term",
            agent_type="general",
            parent_session_id=None,
            callback_chat_id=42,
            spawned_by_kind="user",
            spawned_by_ref=None,
        )
        await store.record_finished(
            sdk_agent_id="agent-term",
            status="completed",
            result_summary="ok",
            transcript_path=None,
            sdk_session_id=None,
        )
        out = await store.set_cancel_requested(jid)
        assert out.get("already_terminal") == "completed"
        assert "cancel_requested" not in out
    finally:
        await conn.close()


async def test_is_cancel_requested_missing_row_returns_false(tmp_path: Path) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        assert await store.is_cancel_requested("nonexistent-agent-id") is False
    finally:
        await conn.close()


# ---------------------------------------------------------------- recover_orphans


async def test_recover_orphans_transitions_started_unfinished_to_interrupted(
    tmp_path: Path,
) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        await store.record_started(
            sdk_agent_id="agent-orphan",
            agent_type="general",
            parent_session_id=None,
            callback_chat_id=42,
            spawned_by_kind="user",
            spawned_by_ref=None,
        )
        counts = await store.recover_orphans()
        assert counts["interrupted"] == 1
        assert counts["dropped"] == 0
        job = await store.get_by_agent_id("agent-orphan")
        assert job is not None
        assert job.status == "interrupted"
        assert job.finished_at is not None
    finally:
        await conn.close()


async def test_recover_orphans_transitions_stale_requested_to_dropped(
    tmp_path: Path,
) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        # Seed a row with backdated `created_at` (>1h old).
        old_ts = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with store._lock:
            await conn.execute(
                "INSERT INTO subagent_jobs("
                "agent_type, task_text, status, callback_chat_id, spawned_by_kind, created_at) "
                "VALUES (?, ?, 'requested', ?, 'cli', ?)",
                ("general", "old task", 42, old_ts),
            )
            await conn.commit()

        # Also a fresh `requested` row — must NOT be touched.
        fresh_id = await store.record_pending_request(
            agent_type="worker",
            task_text="fresh",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )

        counts = await store.recover_orphans(stale_requested_after_s=3600)
        assert counts["interrupted"] == 0
        assert counts["dropped"] == 1

        fresh = await store.get_by_id(fresh_id)
        assert fresh is not None
        assert fresh.status == "requested"
    finally:
        await conn.close()


async def test_recover_orphans_combined_branches(tmp_path: Path) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        await store.record_started(
            sdk_agent_id="agent-i",
            agent_type="general",
            parent_session_id=None,
            callback_chat_id=42,
            spawned_by_kind="user",
            spawned_by_ref=None,
        )
        old_ts = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with store._lock:
            await conn.execute(
                "INSERT INTO subagent_jobs("
                "agent_type, task_text, status, callback_chat_id, spawned_by_kind, created_at) "
                "VALUES (?, ?, 'requested', ?, 'cli', ?)",
                ("general", "old1", 42, old_ts),
            )
            await conn.execute(
                "INSERT INTO subagent_jobs("
                "agent_type, task_text, status, callback_chat_id, spawned_by_kind, created_at) "
                "VALUES (?, ?, 'requested', ?, 'cli', ?)",
                ("general", "old2", 42, old_ts),
            )
            await conn.commit()

        counts = await store.recover_orphans(stale_requested_after_s=3600)
        assert counts["interrupted"] == 1
        assert counts["dropped"] == 2
    finally:
        await conn.close()


# ---------------------------------------------------------------- list_jobs


async def test_list_jobs_filters_by_status_and_kind(tmp_path: Path) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        await store.record_pending_request(
            agent_type="general",
            task_text="g",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )
        await store.record_pending_request(
            agent_type="worker",
            task_text="w",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )
        all_req = await store.list_jobs(status="requested")
        assert len(all_req) == 2
        only_general = await store.list_jobs(status="requested", kind="general")
        assert len(only_general) == 1
        assert only_general[0].agent_type == "general"
    finally:
        await conn.close()


async def test_claim_pending_request_is_idempotent_noop(tmp_path: Path) -> None:
    store, conn = await _mkstore(tmp_path)
    try:
        jid = await store.record_pending_request(
            agent_type="general",
            task_text="x",
            callback_chat_id=42,
            spawned_by_kind="cli",
        )
        assert await store.claim_pending_request(jid) is True
        assert await store.claim_pending_request(jid) is True
    finally:
        await conn.close()
