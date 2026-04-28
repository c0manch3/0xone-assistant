"""Phase 6: SubagentStore CRUD + state-machine + recover_orphans."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from assistant.state.db import apply_schema, connect
from assistant.subagent.store import (
    OrphanRecovery,
    SubagentStore,
)


async def _store(tmp_path: Path) -> tuple[SubagentStore, aiosqlite.Connection]:
    db = tmp_path / "sub.db"
    conn = await connect(db)
    await apply_schema(conn)
    return SubagentStore(conn), conn


async def test_record_pending_request_inserts_requested_row(tmp_path: Path) -> None:
    st, _ = await _store(tmp_path)
    job_id = await st.record_pending_request(
        agent_type="general",
        task_text="write a long thing",
        callback_chat_id=42,
        spawned_by_kind="tool",
    )
    assert job_id > 0
    job = await st.get_by_id(job_id)
    assert job is not None
    assert job.status == "requested"
    assert job.sdk_agent_id is None
    assert job.task_text == "write a long thing"
    assert job.callback_chat_id == 42
    assert job.spawned_by_kind == "tool"


async def test_record_started_inserts_started_row(tmp_path: Path) -> None:
    st, _ = await _store(tmp_path)
    job_id = await st.record_started(
        sdk_agent_id="agent-abc",
        agent_type="researcher",
        parent_session_id="parent-sess",
        callback_chat_id=10,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    job = await st.get_by_id(job_id)
    assert job is not None
    assert job.status == "started"
    assert job.sdk_agent_id == "agent-abc"
    assert job.parent_session_id == "parent-sess"
    assert job.started_at is not None


async def test_record_started_duplicate_returns_existing(tmp_path: Path) -> None:
    """Partial UNIQUE on sdk_agent_id rejects duplicates; the helper logs
    skew and returns the existing id rather than raising."""
    st, _ = await _store(tmp_path)
    first = await st.record_started(
        sdk_agent_id="dup-agent",
        agent_type="general",
        parent_session_id=None,
        callback_chat_id=10,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    second = await st.record_started(
        sdk_agent_id="dup-agent",
        agent_type="general",
        parent_session_id=None,
        callback_chat_id=10,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    assert second == first


async def test_partial_unique_allows_multiple_null_pending(tmp_path: Path) -> None:
    """Two CLI/@tool spawns produce two rows with NULL sdk_agent_id."""
    st, _ = await _store(tmp_path)
    a = await st.record_pending_request(
        agent_type="general",
        task_text="a",
        callback_chat_id=1,
        spawned_by_kind="tool",
    )
    b = await st.record_pending_request(
        agent_type="general",
        task_text="b",
        callback_chat_id=1,
        spawned_by_kind="tool",
    )
    assert a != b
    rows = await st.list_pending_requests(limit=10)
    assert len(rows) == 2


async def test_update_sdk_agent_id_for_claimed_request(tmp_path: Path) -> None:
    st, _ = await _store(tmp_path)
    job_id = await st.record_pending_request(
        agent_type="general",
        task_text="task",
        callback_chat_id=10,
        spawned_by_kind="tool",
    )
    patched = await st.update_sdk_agent_id_for_claimed_request(
        job_id=job_id,
        sdk_agent_id="real-agent",
        parent_session_id="parent-1",
    )
    assert patched is True
    job = await st.get_by_id(job_id)
    assert job is not None
    assert job.status == "started"
    assert job.sdk_agent_id == "real-agent"
    assert job.parent_session_id == "parent-1"
    assert job.started_at is not None


async def test_update_claim_skew_on_already_started(tmp_path: Path) -> None:
    """Patch returns False if the row was already moved out of 'requested'."""
    st, _ = await _store(tmp_path)
    job_id = await st.record_pending_request(
        agent_type="worker",
        task_text="t",
        callback_chat_id=1,
        spawned_by_kind="tool",
    )
    # Mutate it manually to simulate a prior claim.
    await st._conn.execute(
        "UPDATE subagent_jobs SET status='started', sdk_agent_id='x' "
        "WHERE id=?",
        (job_id,),
    )
    await st._conn.commit()
    patched = await st.update_sdk_agent_id_for_claimed_request(
        job_id=job_id,
        sdk_agent_id="other",
        parent_session_id=None,
    )
    assert patched is False


async def test_record_finished_updates_started(tmp_path: Path) -> None:
    st, _ = await _store(tmp_path)
    await st.record_started(
        sdk_agent_id="ag",
        agent_type="worker",
        parent_session_id=None,
        callback_chat_id=1,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    ok = await st.record_finished(
        sdk_agent_id="ag",
        status="completed",
        result_summary="hi",
        transcript_path="/tmp/t.jsonl",
        sdk_session_id="sess-2",
    )
    assert ok is True
    job = await st.get_by_agent_id("ag")
    assert job is not None
    assert job.status == "completed"
    assert job.result_summary == "hi"
    assert job.transcript_path == "/tmp/t.jsonl"
    assert job.sdk_session_id == "sess-2"
    assert job.finished_at is not None


async def test_record_finished_skew_returns_false(tmp_path: Path) -> None:
    """If status is not 'started', the precondition fails — no raise,
    returns False."""
    st, _ = await _store(tmp_path)
    await st.record_started(
        sdk_agent_id="ag",
        agent_type="worker",
        parent_session_id=None,
        callback_chat_id=1,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    await st.record_finished(
        sdk_agent_id="ag",
        status="completed",
        result_summary="x",
        transcript_path=None,
        sdk_session_id=None,
    )
    # Second call: row is already 'completed' — predicate fails.
    again = await st.record_finished(
        sdk_agent_id="ag",
        status="failed",
        result_summary="y",
        transcript_path=None,
        sdk_session_id=None,
    )
    assert again is False


async def test_record_finished_rejects_non_terminal_status(tmp_path: Path) -> None:
    st, _ = await _store(tmp_path)
    with pytest.raises(ValueError, match="non-terminal"):
        await st.record_finished(
            sdk_agent_id="x",
            status="started",
            result_summary=None,
            transcript_path=None,
            sdk_session_id=None,
        )


async def test_set_cancel_requested_on_pending(tmp_path: Path) -> None:
    st, _ = await _store(tmp_path)
    job_id = await st.record_pending_request(
        agent_type="general",
        task_text="t",
        callback_chat_id=1,
        spawned_by_kind="tool",
    )
    res = await st.set_cancel_requested(job_id)
    assert res["cancel_requested"] is True
    assert res["previous_status"] == "requested"
    job = await st.get_by_id(job_id)
    assert job is not None
    assert job.cancel_requested is True


async def test_set_cancel_requested_already_terminal(tmp_path: Path) -> None:
    st, _ = await _store(tmp_path)
    await st.record_started(
        sdk_agent_id="ag",
        agent_type="worker",
        parent_session_id=None,
        callback_chat_id=1,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    await st.record_finished(
        sdk_agent_id="ag",
        status="completed",
        result_summary=None,
        transcript_path=None,
        sdk_session_id=None,
    )
    job = await st.get_by_agent_id("ag")
    assert job is not None
    res = await st.set_cancel_requested(job.id)
    assert res == {"already_terminal": "completed"}


async def test_set_cancel_requested_unknown_id(tmp_path: Path) -> None:
    st, _ = await _store(tmp_path)
    res = await st.set_cancel_requested(999)
    assert res == {"error": "job not found"}


async def test_is_cancel_requested(tmp_path: Path) -> None:
    st, _ = await _store(tmp_path)
    await st.record_started(
        sdk_agent_id="ag",
        agent_type="worker",
        parent_session_id=None,
        callback_chat_id=1,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    assert await st.is_cancel_requested("ag") is False
    job = await st.get_by_agent_id("ag")
    assert job is not None
    await st.set_cancel_requested(job.id)
    assert await st.is_cancel_requested("ag") is True
    assert await st.is_cancel_requested("does-not-exist") is False


async def test_recover_orphans_branch_1_dropped_no_sdk(tmp_path: Path) -> None:
    """status='started' AND sdk_agent_id IS NULL → 'dropped'."""
    st, _ = await _store(tmp_path)
    # Manually craft a Branch-1 orphan
    await st._conn.execute(
        "INSERT INTO subagent_jobs(agent_type, status, callback_chat_id, "
        "spawned_by_kind, sdk_agent_id) VALUES('general','started',1,"
        "'tool',NULL)"
    )
    await st._conn.commit()
    rec = await st.recover_orphans()
    assert rec.dropped_no_sdk == 1
    assert rec.interrupted == 0
    assert rec.dropped_stale == 0


async def test_recover_orphans_branch_2_interrupted(tmp_path: Path) -> None:
    """status='started' AND sdk_agent_id IS NOT NULL → 'interrupted'."""
    st, _ = await _store(tmp_path)
    await st.record_started(
        sdk_agent_id="alive",
        agent_type="researcher",
        parent_session_id=None,
        callback_chat_id=10,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    rec = await st.recover_orphans()
    assert rec.interrupted == 1
    assert rec.dropped_no_sdk == 0
    job = await st.get_by_agent_id("alive")
    assert job is not None
    assert job.status == "interrupted"


async def test_recover_orphans_branch_3_stale_requested(tmp_path: Path) -> None:
    """status='requested' older than threshold → 'dropped'.

    We seed created_at directly so the test is deterministic.
    """
    st, _ = await _store(tmp_path)
    await st._conn.execute(
        "INSERT INTO subagent_jobs(agent_type, status, callback_chat_id, "
        "spawned_by_kind, sdk_agent_id, created_at) VALUES("
        "'general','requested',1,'tool',NULL,'2020-01-01T00:00:00Z')"
    )
    await st._conn.commit()
    rec = await st.recover_orphans(stale_requested_after_s=3600)
    assert rec.dropped_stale == 1


async def test_recover_orphans_leaves_fresh_requested(tmp_path: Path) -> None:
    """A 'requested' row with recent created_at is left untouched."""
    st, _ = await _store(tmp_path)
    job_id = await st.record_pending_request(
        agent_type="general",
        task_text="fresh",
        callback_chat_id=1,
        spawned_by_kind="tool",
    )
    rec = await st.recover_orphans()
    assert rec.dropped_stale == 0
    job = await st.get_by_id(job_id)
    assert job is not None
    assert job.status == "requested"


async def test_recover_orphans_branch_ordering(tmp_path: Path) -> None:
    """Branch 1 must run BEFORE Branch 2 — otherwise Branch 2's
    predicate (status='started' AND sdk_agent_id IS NOT NULL AND
    finished_at IS NULL) would not catch the rows that Branch 1
    actually drops; but a regression that swapped the order would
    silently move sdk_agent_id IS NULL rows to 'interrupted' (wrong).
    Test seeds one of each shape and asserts terminal status on each.
    """
    st, _ = await _store(tmp_path)
    # Branch-1 candidate: started + NULL sdk_agent_id.
    await st._conn.execute(
        "INSERT INTO subagent_jobs(agent_type, status, callback_chat_id, "
        "spawned_by_kind, sdk_agent_id) "
        "VALUES('general','started',1,'tool',NULL)"
    )
    # Branch-2 candidate: started + non-NULL sdk_agent_id.
    await st.record_started(
        sdk_agent_id="real",
        agent_type="researcher",
        parent_session_id=None,
        callback_chat_id=1,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    # Branch-3 candidate: stale requested.
    await st._conn.execute(
        "INSERT INTO subagent_jobs(agent_type, status, callback_chat_id, "
        "spawned_by_kind, sdk_agent_id, created_at) "
        "VALUES('general','requested',1,'tool',NULL,'2020-01-01T00:00:00Z')"
    )
    # Terminal control row.
    await st._conn.execute(
        "INSERT INTO subagent_jobs(agent_type, status, callback_chat_id, "
        "spawned_by_kind, sdk_agent_id) "
        "VALUES('worker','completed',1,'user','done-agent')"
    )
    await st._conn.commit()
    rec = await st.recover_orphans()
    assert rec.dropped_no_sdk == 1
    assert rec.interrupted == 1
    assert rec.dropped_stale == 1
    # Verify exact terminal statuses.
    cur = await st._conn.execute(
        "SELECT status FROM subagent_jobs WHERE sdk_agent_id='real'"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "interrupted"
    cur = await st._conn.execute(
        "SELECT status FROM subagent_jobs WHERE sdk_agent_id='done-agent'"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "completed"  # untouched


async def test_recover_orphans_empty(tmp_path: Path) -> None:
    st, _ = await _store(tmp_path)
    rec = await st.recover_orphans()
    assert rec == OrphanRecovery(0, 0, 0)
    assert rec.total == 0


async def test_list_jobs_filters(tmp_path: Path) -> None:
    st, _ = await _store(tmp_path)
    await st.record_pending_request(
        agent_type="general", task_text="a", callback_chat_id=1,
        spawned_by_kind="tool",
    )
    await st.record_pending_request(
        agent_type="researcher", task_text="b", callback_chat_id=1,
        spawned_by_kind="tool",
    )
    rows = await st.list_jobs(kind="general")
    assert len(rows) == 1
    assert rows[0].agent_type == "general"
    rows = await st.list_jobs(status="requested", limit=5)
    assert len(rows) == 2


async def test_list_pending_requests_oldest_first(tmp_path: Path) -> None:
    """Oldest created_at first."""
    st, _ = await _store(tmp_path)
    await st._conn.execute(
        "INSERT INTO subagent_jobs(agent_type, status, callback_chat_id, "
        "spawned_by_kind, sdk_agent_id, created_at) VALUES("
        "'general','requested',1,'tool',NULL,'2020-01-01T00:00:00Z')"
    )
    await st._conn.execute(
        "INSERT INTO subagent_jobs(agent_type, status, callback_chat_id, "
        "spawned_by_kind, sdk_agent_id, created_at) VALUES("
        "'researcher','requested',1,'tool',NULL,'2020-01-02T00:00:00Z')"
    )
    await st._conn.commit()
    rows = await st.list_pending_requests(limit=10)
    assert len(rows) == 2
    assert rows[0].agent_type == "general"
    assert rows[1].agent_type == "researcher"


async def test_get_by_agent_id_missing(tmp_path: Path) -> None:
    st, _ = await _store(tmp_path)
    assert await st.get_by_agent_id("does-not-exist") is None


async def test_get_by_id_missing(tmp_path: Path) -> None:
    st, _ = await _store(tmp_path)
    assert await st.get_by_id(123) is None


async def test_record_finished_recovers_from_start_stop_race(
    tmp_path: Path,
) -> None:
    """Fix-pack F5 (devil H-W2-4): if Stop hook fires BEFORE the picker's
    ``update_sdk_agent_id_for_claimed_request`` commits, the row is still
    'requested' but already carries the agent_id. ``record_finished``
    must finalize THAT row instead of dropping the notify.
    """
    st, _ = await _store(tmp_path)
    job_id = await st.record_pending_request(
        agent_type="general",
        task_text="t",
        callback_chat_id=42,
        spawned_by_kind="tool",
    )
    # Simulate the partial commit: row stays 'requested' but the
    # sdk_agent_id has been written by the picker's UPDATE attempt.
    await st._conn.execute(
        "UPDATE subagent_jobs SET sdk_agent_id='race-ag' WHERE id=?",
        (job_id,),
    )
    await st._conn.commit()
    ok = await st.record_finished(
        sdk_agent_id="race-ag",
        status="completed",
        result_summary="done",
        transcript_path=None,
        sdk_session_id=None,
    )
    assert ok is True
    job = await st.get_by_id(job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.result_summary == "done"
    assert job.finished_at is not None
    # ``started_at`` was NULL pre-race; recovery path back-fills it so
    # downstream duration computations don't blow up.
    assert job.started_at is not None


async def test_record_finished_persists_last_error(tmp_path: Path) -> None:
    """Fix-pack F3: ``last_error`` parameter is stored on terminal
    transition (used by Stop hook on status='failed')."""
    st, _ = await _store(tmp_path)
    await st.record_started(
        sdk_agent_id="ag-err",
        agent_type="worker",
        parent_session_id=None,
        callback_chat_id=1,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    ok = await st.record_finished(
        sdk_agent_id="ag-err",
        status="failed",
        result_summary=None,
        transcript_path=None,
        sdk_session_id=None,
        last_error="max turns exceeded",
    )
    assert ok is True
    job = await st.get_by_agent_id("ag-err")
    assert job is not None
    assert job.status == "failed"
    assert job.last_error == "max turns exceeded"


async def test_set_cancel_requested_pre_pickup_transitions_to_stopped(
    tmp_path: Path,
) -> None:
    """Fix-pack F1: cancel-before-pickup directly transitions a 'requested'
    row to 'stopped' AND sets finished_at, so the picker's
    list_pending_requests filter excludes it forever (no looping skips)."""
    st, _ = await _store(tmp_path)
    job_id = await st.record_pending_request(
        agent_type="general",
        task_text="t",
        callback_chat_id=42,
        spawned_by_kind="tool",
    )
    res = await st.set_cancel_requested(job_id)
    assert res["cancel_requested"] is True
    assert res["previous_status"] == "requested"
    job = await st.get_by_id(job_id)
    assert job is not None
    assert job.status == "stopped"
    assert job.finished_at is not None
    assert job.cancel_requested is True


async def test_set_cancel_requested_started_keeps_status(
    tmp_path: Path,
) -> None:
    """When the row is already 'started', cancel only flips the flag —
    Stop hook still has to run to drive the terminal transition."""
    st, _ = await _store(tmp_path)
    await st.record_started(
        sdk_agent_id="started-ag",
        agent_type="worker",
        parent_session_id=None,
        callback_chat_id=1,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    job = await st.get_by_agent_id("started-ag")
    assert job is not None
    res = await st.set_cancel_requested(job.id)
    assert res["cancel_requested"] is True
    refreshed = await st.get_by_id(job.id)
    assert refreshed is not None
    # Status untouched — Stop hook drives the terminal transition.
    assert refreshed.status == "started"
    assert refreshed.cancel_requested is True
    assert refreshed.finished_at is None


async def test_mark_dispatch_failed_increments_attempts(tmp_path: Path) -> None:
    """Fix-pack F1: each call increments ``attempts``; row stays 'requested'
    until the threshold is reached."""
    st, _ = await _store(tmp_path)
    job_id = await st.record_pending_request(
        agent_type="general",
        task_text="t",
        callback_chat_id=1,
        spawned_by_kind="tool",
    )
    s1 = await st.mark_dispatch_failed(job_id=job_id, reason="boom 1")
    assert s1 == "requested"
    job = await st.get_by_id(job_id)
    assert job is not None
    assert job.attempts == 1
    assert job.last_error == "boom 1"
    s2 = await st.mark_dispatch_failed(job_id=job_id, reason="boom 2")
    assert s2 == "requested"
    s3 = await st.mark_dispatch_failed(job_id=job_id, reason="boom 3")
    assert s3 == "error"
    job = await st.get_by_id(job_id)
    assert job is not None
    assert job.status == "error"
    assert job.attempts == 3
    assert job.last_error == "boom 3"
    assert job.finished_at is not None


async def test_mark_dispatch_failed_truncates_long_reason(
    tmp_path: Path,
) -> None:
    """``reason`` is trimmed to 500 chars so a runaway exception repr
    cannot bloat the row."""
    st, _ = await _store(tmp_path)
    job_id = await st.record_pending_request(
        agent_type="general",
        task_text="t",
        callback_chat_id=1,
        spawned_by_kind="tool",
    )
    long_reason = "x" * 5000
    await st.mark_dispatch_failed(job_id=job_id, reason=long_reason)
    job = await st.get_by_id(job_id)
    assert job is not None
    assert job.last_error is not None
    assert len(job.last_error) <= 500


async def test_lock_serialises_writes(tmp_path: Path) -> None:
    """Two concurrent record_pending_request calls do not interleave —
    both succeed and both rows exist."""
    st, _ = await _store(tmp_path)

    async def insert(t: str) -> int:
        return await st.record_pending_request(
            agent_type="general",
            task_text=t,
            callback_chat_id=1,
            spawned_by_kind="tool",
        )

    ids = await asyncio.gather(insert("a"), insert("b"), insert("c"))
    assert len(set(ids)) == 3
