from __future__ import annotations

from pathlib import Path

import aiosqlite

from assistant.state.db import SCHEMA_VERSION, apply_schema, connect


async def _user_version(conn: aiosqlite.Connection) -> int:
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _table_exists(conn: aiosqlite.Connection, name: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ) as cur:
        return await cur.fetchone() is not None


async def _columns(conn: aiosqlite.Connection, table: str) -> list[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return [r[1] for r in await cur.fetchall()]


async def test_happy_path_fresh_db(tmp_path: Path) -> None:
    """Fresh DB → apply_schema bumps user_version straight to SCHEMA_VERSION.

    Phase 5 (2026-04-21): SCHEMA_VERSION is now 3 after the scheduler
    migration (0003). The original phase-2 test asserted ``== 2`` to
    guard the B1 "no stomp" invariant; we keep that intent alive by
    asserting a monotonically-increasing version + that the phase-2
    invariants still hold.
    """
    db = tmp_path / "happy.db"
    conn = await connect(db)
    assert await _user_version(conn) == 0
    await apply_schema(conn)
    assert await _user_version(conn) == SCHEMA_VERSION >= 2
    assert await _table_exists(conn, "conversations")
    assert await _table_exists(conn, "turns")
    cols = await _columns(conn, "conversations")
    assert "block_type" in cols
    await conn.close()


async def test_re_run_is_noop(tmp_path: Path) -> None:
    """Second apply_schema call on a fully-migrated DB is a no-op."""
    db = tmp_path / "rerun.db"
    conn = await connect(db)
    await apply_schema(conn)
    # Insert a production-style row with block_type='tool_use' — re-run
    # must NOT stomp it back to 'text' (B1 guard).
    await conn.execute(
        "INSERT INTO conversations(chat_id, turn_id, role, content_json, "
        "meta_json, block_type) VALUES (1, 't1', 'assistant', '[]', NULL, 'tool_use')"
    )
    await conn.commit()

    await apply_schema(conn)
    assert await _user_version(conn) == SCHEMA_VERSION

    async with conn.execute("SELECT block_type FROM conversations WHERE turn_id='t1'") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "tool_use"
    await conn.close()


async def test_v1_to_v2_backfills_block_type_and_turns(tmp_path: Path) -> None:
    """A DB at v=1 with rows receives block_type='text' backfill and a
    corresponding turns row."""
    db = tmp_path / "v1.db"
    conn = await connect(db)

    # Hand-build a v=1 DB (old schema, no block_type, no turns table).
    await conn.executescript(
        """
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            turn_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content_json TEXT NOT NULL,
            meta_json TEXT,
            created_at TEXT NOT NULL DEFAULT
              (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        """
    )
    await conn.execute("PRAGMA user_version=1")
    await conn.execute(
        "INSERT INTO conversations(chat_id, turn_id, role, content_json) "
        "VALUES (7, 'legacy', 'user', '[]')"
    )
    await conn.commit()

    await apply_schema(conn)
    assert await _user_version(conn) == SCHEMA_VERSION

    cols = await _columns(conn, "conversations")
    assert "block_type" in cols

    async with conn.execute("SELECT block_type FROM conversations WHERE turn_id='legacy'") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "text"

    async with conn.execute("SELECT status FROM turns WHERE turn_id='legacy'") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "complete"
    await conn.close()
