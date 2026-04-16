from __future__ import annotations

from pathlib import Path

import aiosqlite

SCHEMA_VERSION = 2

# Phase 1 initial schema. Kept inline for the v0 → v1 transition.
SCHEMA_V1_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    turn_id      TEXT NOT NULL,
    role         TEXT NOT NULL,
    content_json TEXT NOT NULL,
    meta_json    TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_conversations_chat_time
    ON conversations(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_turn
    ON conversations(chat_id, turn_id);
"""

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


async def connect(path: Path) -> aiosqlite.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


async def _apply_v1(conn: aiosqlite.Connection) -> None:
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.executescript(SCHEMA_V1_SQL)
        await conn.execute("PRAGMA user_version = 1")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def _apply_v2(conn: aiosqlite.Connection) -> None:
    sql = (_MIGRATIONS_DIR / "0002_turns_block_type.sql").read_text(encoding="utf-8")
    # Recreate-table pattern requires FK checks OFF during the transaction.
    await conn.execute("PRAGMA foreign_keys = OFF")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.executescript(sql)
        await conn.execute("PRAGMA user_version = 2")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.execute("PRAGMA foreign_keys = ON")


async def apply_schema(conn: aiosqlite.Connection) -> None:
    """Apply migrations atomically up to SCHEMA_VERSION. Idempotent."""
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    current = row[0] if row else 0

    if current < 1:
        await _apply_v1(conn)
        current = 1
    if current < 2:
        await _apply_v2(conn)
        current = 2
