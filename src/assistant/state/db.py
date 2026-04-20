from __future__ import annotations

from pathlib import Path

import aiosqlite

SCHEMA_VERSION = 2

SCHEMA_0001 = """
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


async def connect(path: Path) -> aiosqlite.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


async def _current_version(conn: aiosqlite.Connection) -> int:
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _apply_0001(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA_0001)
    await conn.execute("PRAGMA user_version=1")
    await conn.commit()


async def _apply_0002(conn: aiosqlite.Connection) -> None:
    """Run migration 0002 statement-by-statement.

    Atomicity & idempotency:
    - Early-exit if ``PRAGMA user_version >= 2`` — protects direct test calls
      from stomping production data (B1). ``apply_schema`` already gates on
      the version; this second check inside the runner closes the door on any
      stray direct caller (there are tests that do exactly that).
    - Statement-by-statement: ``executescript()`` would implicit-COMMIT our
      ``BEGIN EXCLUSIVE``, defeating atomicity (B2 / sqlite3 docs: "If there
      is a pending transaction, an implicit COMMIT statement is executed
      first.").
    - ROLLBACK on exception preserves v=1 state; rerun converges to v=2.
    - FK toggled OFF during recreate-table; back ON in ``finally``.
    """
    if await _current_version(conn) >= 2:
        return

    await conn.execute("PRAGMA foreign_keys=OFF")
    try:
        await conn.execute("BEGIN EXCLUSIVE")

        # 1. Drop any leftover from a previous partial run.
        await conn.execute("DROP TABLE IF EXISTS conversations_new")

        # 2. New conversations schema with block_type column.
        await conn.execute(
            "CREATE TABLE conversations_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "chat_id INTEGER NOT NULL, "
            "turn_id TEXT NOT NULL, "
            "role TEXT NOT NULL, "
            "content_json TEXT NOT NULL, "
            "meta_json TEXT, "
            "block_type TEXT NOT NULL DEFAULT 'text', "
            "created_at TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%dT%H:%M:%SZ','now')))"
        )

        # 3. Backfill rows from old table; block_type defaults to 'text' for
        # legacy rows (pre-phase-2 conversations only stored plain text).
        await conn.execute(
            "INSERT INTO conversations_new "
            "(id, chat_id, turn_id, role, content_json, meta_json, created_at, block_type) "
            "SELECT id, chat_id, turn_id, role, content_json, meta_json, created_at, 'text' "
            "FROM conversations"
        )

        # 4. Drop the old table.
        await conn.execute("DROP TABLE conversations")

        # 5. Rename the new table to conversations.
        await conn.execute("ALTER TABLE conversations_new RENAME TO conversations")

        # 6-7. Recreate indexes.
        await conn.execute(
            "CREATE INDEX idx_conversations_chat_time ON conversations(chat_id, created_at)"
        )
        await conn.execute("CREATE INDEX idx_conversations_turn ON conversations(chat_id, turn_id)")

        # 8. Turns table — source of truth for turn-level status & completion.
        await conn.execute(
            "CREATE TABLE turns ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "chat_id INTEGER NOT NULL, "
            "turn_id TEXT NOT NULL UNIQUE, "
            "status TEXT NOT NULL DEFAULT 'pending', "
            "created_at TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%dT%H:%M:%SZ','now')), "
            "completed_at TEXT, "
            "meta_json TEXT)"
        )

        # 9. Backfill turns from existing conversations grouped by turn_id.
        await conn.execute(
            "INSERT OR IGNORE INTO turns "
            "(chat_id, turn_id, status, created_at, completed_at) "
            "SELECT chat_id, turn_id, 'complete', MIN(created_at), MAX(created_at) "
            "FROM conversations GROUP BY chat_id, turn_id"
        )

        # 10. Turns index for load_recent ORDER BY completed_at.
        await conn.execute("CREATE INDEX idx_turns_chat_completed ON turns(chat_id, completed_at)")

        # 11. Bump version inside the transaction — commit makes everything visible.
        await conn.execute("PRAGMA user_version=2")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.execute("PRAGMA foreign_keys=ON")


async def apply_schema(conn: aiosqlite.Connection) -> None:
    current = await _current_version(conn)
    if current < 1:
        await _apply_0001(conn)
    if current < 2:
        await _apply_0002(conn)
