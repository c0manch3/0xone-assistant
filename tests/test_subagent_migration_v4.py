"""Phase 6 / commit 1 — DB migration v4 (`subagent_jobs` ledger).

Verifies:
  * `PRAGMA user_version = 4` after `apply_schema`.
  * `subagent_jobs` table exists with documented columns.
  * The `partial UNIQUE` on `sdk_agent_id WHERE sdk_agent_id IS NOT NULL`
    allows multiple NULL rows (pre-picker CLI inserts) but rejects a
    duplicate non-NULL value.
  * Idempotent re-application.
  * Status-index + secondary indexes present so list/recover queries
    don't force a sequential scan.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from assistant.state.db import SCHEMA_VERSION, apply_schema, connect


async def _column_names(conn: aiosqlite.Connection, table: str) -> list[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return [r[1] for r in rows]


async def _index_names(conn: aiosqlite.Connection, table: str) -> list[str]:
    async with conn.execute(f"PRAGMA index_list({table})") as cur:
        rows = await cur.fetchall()
    return [r[1] for r in rows]


async def test_v4_bumps_user_version(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "m4.db")
    try:
        await apply_schema(conn)
        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION
    finally:
        await conn.close()


async def test_v4_subagent_jobs_columns(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "m4.db")
    try:
        await apply_schema(conn)
        cols = await _column_names(conn, "subagent_jobs")
    finally:
        await conn.close()

    for col in (
        "id",
        "sdk_agent_id",
        "sdk_session_id",
        "parent_session_id",
        "agent_type",
        "task_text",
        "transcript_path",
        "status",
        "cancel_requested",
        "result_summary",
        "cost_usd",
        "callback_chat_id",
        "spawned_by_kind",
        "spawned_by_ref",
        "depth",
        "created_at",
        "started_at",
        "finished_at",
    ):
        assert col in cols, f"subagent_jobs missing {col!r} (have: {cols})"


async def test_v4_indexes_present(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "m4.db")
    try:
        await apply_schema(conn)
        names = await _index_names(conn, "subagent_jobs")
    finally:
        await conn.close()

    for idx in (
        "idx_subagent_jobs_sdk_agent_id_uq",
        "idx_subagent_jobs_status_started",
        "idx_subagent_jobs_status_created",
    ):
        assert idx in names, f"subagent_jobs missing index {idx!r} (have: {names})"


async def test_v4_partial_unique_allows_multiple_null_sdk_agent_id(tmp_path: Path) -> None:
    """CLI pre-picker rows all carry `sdk_agent_id IS NULL`; the partial
    UNIQUE index (`WHERE sdk_agent_id IS NOT NULL`) must tolerate that."""
    conn = await connect(tmp_path / "m4.db")
    try:
        await apply_schema(conn)
        insert_sql = (
            "INSERT INTO subagent_jobs("
            "agent_type, status, callback_chat_id, spawned_by_kind) "
            "VALUES (?, ?, ?, ?)"
        )
        for _ in range(3):
            await conn.execute(insert_sql, ("general", "requested", 42, "cli"))
        await conn.commit()
        async with conn.execute("SELECT COUNT(*) FROM subagent_jobs") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 3
    finally:
        await conn.close()


async def test_v4_partial_unique_rejects_duplicate_non_null_sdk_agent_id(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "m4.db")
    insert_sql = (
        "INSERT INTO subagent_jobs("
        "sdk_agent_id, agent_type, status, callback_chat_id, spawned_by_kind) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    try:
        await apply_schema(conn)
        await conn.execute(insert_sql, ("agent-X", "general", "started", 42, "user"))
        await conn.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(insert_sql, ("agent-X", "general", "started", 42, "user"))
            await conn.commit()
    finally:
        await conn.close()


async def test_v4_apply_is_idempotent(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "m4.db")
    try:
        await apply_schema(conn)
        await apply_schema(conn)
        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION
    finally:
        await conn.close()
