"""Phase 6: schema v4 migration — column shape, partial UNIQUE, idempotency."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from assistant.state.db import (
    SCHEMA_VERSION,
    _apply_0004,
    apply_schema,
    connect,
)


async def test_v4_applies_to_user_version(tmp_path: Path) -> None:
    db = tmp_path / "v4.db"
    conn = await connect(db)
    await apply_schema(conn)
    cur = await conn.execute("PRAGMA user_version")
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == 4
    assert SCHEMA_VERSION == 4


async def test_v4_creates_subagent_jobs_table(tmp_path: Path) -> None:
    db = tmp_path / "v4.db"
    conn = await connect(db)
    await apply_schema(conn)
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='subagent_jobs'"
    )
    row = await cur.fetchone()
    assert row is not None
    cur = await conn.execute("PRAGMA table_info(subagent_jobs)")
    cols = [r[1] for r in await cur.fetchall()]
    # Fix-pack F6: ``depth`` column DROPPED (never written non-zero in
    # phase 6; SQLite ALTER TABLE DROP COLUMN is painful post-deploy so
    # we removed it before the column reached production).
    # Fix-pack F1: ``attempts`` + ``last_error`` columns ADDED to back
    # the ``mark_dispatch_failed`` retry counter.
    expected = {
        "id", "sdk_agent_id", "sdk_session_id", "parent_session_id",
        "agent_type", "task_text", "transcript_path", "status",
        "cancel_requested", "result_summary", "cost_usd",
        "callback_chat_id", "spawned_by_kind", "spawned_by_ref",
        "attempts", "last_error",
        "created_at", "started_at", "finished_at",
    }
    assert set(cols) == expected
    assert "depth" not in set(cols)


async def test_v4_creates_partial_unique_index(tmp_path: Path) -> None:
    db = tmp_path / "v4.db"
    conn = await connect(db)
    await apply_schema(conn)
    cur = await conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='idx_subagent_jobs_sdk_agent_id_uq'"
    )
    row = await cur.fetchone()
    assert row is not None
    sql = row[0]
    assert "WHERE sdk_agent_id IS NOT NULL" in sql


async def test_v4_partial_unique_allows_null_duplicates(tmp_path: Path) -> None:
    db = tmp_path / "v4.db"
    conn = await connect(db)
    await apply_schema(conn)
    await conn.execute(
        "INSERT INTO subagent_jobs(agent_type, status, callback_chat_id, "
        "spawned_by_kind, sdk_agent_id) VALUES('general','requested',1,"
        "'tool',NULL)"
    )
    await conn.execute(
        "INSERT INTO subagent_jobs(agent_type, status, callback_chat_id, "
        "spawned_by_kind, sdk_agent_id) VALUES('general','requested',1,"
        "'tool',NULL)"
    )
    await conn.commit()
    cur = await conn.execute(
        "SELECT COUNT(*) FROM subagent_jobs WHERE sdk_agent_id IS NULL"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == 2


async def test_v4_partial_unique_rejects_non_null_duplicates(tmp_path: Path) -> None:
    db = tmp_path / "v4.db"
    conn = await connect(db)
    await apply_schema(conn)
    await conn.execute(
        "INSERT INTO subagent_jobs(agent_type, status, callback_chat_id, "
        "spawned_by_kind, sdk_agent_id) VALUES('general','started',1,"
        "'user','dup-id')"
    )
    await conn.commit()
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(
            "INSERT INTO subagent_jobs(agent_type, status, callback_chat_id, "
            "spawned_by_kind, sdk_agent_id) VALUES('general','started',1,"
            "'user','dup-id')"
        )
        await conn.commit()


async def test_v4_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "v4.db"
    conn = await connect(db)
    await apply_schema(conn)
    # Run again — must not raise.
    await apply_schema(conn)
    await _apply_0004(conn)


async def test_v4_default_values(tmp_path: Path) -> None:
    """Defaults: status='started', cancel_requested=0, attempts=0,
    last_error NULL, created_at populated."""
    db = tmp_path / "v4.db"
    conn = await connect(db)
    await apply_schema(conn)
    await conn.execute(
        "INSERT INTO subagent_jobs(agent_type, callback_chat_id, "
        "spawned_by_kind) VALUES('worker',1,'tool')"
    )
    await conn.commit()
    cur = await conn.execute(
        "SELECT status, cancel_requested, attempts, last_error, "
        "created_at FROM subagent_jobs"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "started"
    assert row[1] == 0
    assert row[2] == 0  # attempts default
    assert row[3] is None  # last_error default
    assert row[4] is not None and len(row[4]) == 20  # ISO Z form


async def test_v4_indexes_exist(tmp_path: Path) -> None:
    db = tmp_path / "v4.db"
    conn = await connect(db)
    await apply_schema(conn)
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='subagent_jobs' ORDER BY name"
    )
    names = {r[0] for r in await cur.fetchall()}
    assert "idx_subagent_jobs_sdk_agent_id_uq" in names
    assert "idx_subagent_jobs_status_started" in names
    assert "idx_subagent_jobs_status_created" in names
