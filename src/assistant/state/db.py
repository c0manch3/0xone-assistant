from __future__ import annotations

from pathlib import Path

import aiosqlite

SCHEMA_VERSION = 6

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


async def _apply_v3(conn: aiosqlite.Connection) -> None:
    """Phase 5: schedules + triggers tables. Pure additive — no destructive
    operations on existing tables, so FK-pragma bookkeeping is not required."""
    sql = (_MIGRATIONS_DIR / "0003_scheduler.sql").read_text(encoding="utf-8")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.executescript(sql)
        await conn.execute("PRAGMA user_version = 3")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def _apply_v4(conn: aiosqlite.Connection) -> None:
    """Phase 6: `subagent_jobs` ledger. Pure additive (new table + indexes),
    no destructive operations on existing tables, so FK-pragma bookkeeping
    is not required."""
    sql = (_MIGRATIONS_DIR / "0004_subagent.sql").read_text(encoding="utf-8")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.executescript(sql)
        await conn.execute("PRAGMA user_version = 4")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def _apply_v5(conn: aiosqlite.Connection) -> None:
    """Phase 8: ``schedules.seed_key`` + partial UNIQUE INDEX. Additive.

    v2 SF-F2: do **NOT** use ``executescript`` — on aiosqlite each
    statement auto-commits, which bypasses the ``BEGIN IMMEDIATE`` we
    opened and breaks migration atomicity. Execute each statement
    individually via ``conn.execute`` so the entire migration is one
    transaction that rolls back cleanly on error.

    The sibling ``0005_schedule_seed_key.sql`` file is kept as DDL
    documentation and for downstream tooling (e.g. sqlite schema
    diffing); the runtime applies the statements below, not that file.
    """
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("ALTER TABLE schedules ADD COLUMN seed_key TEXT")
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_schedules_seed_key "
            "ON schedules(seed_key) WHERE seed_key IS NOT NULL"
        )
        await conn.execute("PRAGMA user_version = 5")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def _apply_v6(conn: aiosqlite.Connection) -> None:
    """Phase 8 Q10: ``seed_tombstones`` table. Pure additive (new table).

    v2 SF-F2: same rationale as ``_apply_v5`` — explicit ``conn.execute``
    statements only, no ``executescript``.
    """
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS seed_tombstones ("
            "    seed_key   TEXT PRIMARY KEY,"
            "    deleted_at TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
            ")"
        )
        await conn.execute("PRAGMA user_version = 6")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


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
    if current < 3:
        await _apply_v3(conn)
        current = 3
    if current < 4:
        await _apply_v4(conn)
        current = 4
    if current < 5:
        await _apply_v5(conn)
        current = 5
    if current < 6:
        await _apply_v6(conn)
        current = 6
