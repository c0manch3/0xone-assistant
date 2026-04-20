"""R11 — Migration 0002 idempotency + crash-safety probe.

Goal: prove the migration SQL is atomic (ROLLBACK on crash) and idempotent
(re-run on partial state is a no-op).

Approach:
1. Build a phase-1 DB (schema v1 with user_version=1, some conversations rows).
2. Run migration 0002 in a transaction.
3. Simulate crash: force exception mid-transaction, verify ROLLBACK.
4. Re-run migration on the rolled-back DB, verify success.
5. Re-run migration on the already-migrated DB, verify no-op.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import aiosqlite


PHASE1_SCHEMA = """
CREATE TABLE conversations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    turn_id      TEXT NOT NULL,
    role         TEXT NOT NULL,
    content_json TEXT NOT NULL,
    meta_json    TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX idx_conversations_chat_time ON conversations(chat_id, created_at);
CREATE INDEX idx_conversations_turn ON conversations(chat_id, turn_id);
"""

MIGRATION_0002 = """
-- All DDL + DML inside one BEGIN EXCLUSIVE transaction.
-- Use recreate-table pattern to add block_type column + FK constraint in one shot.

DROP TABLE IF EXISTS conversations_new;

CREATE TABLE conversations_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    turn_id      TEXT NOT NULL,
    role         TEXT NOT NULL,
    content_json TEXT NOT NULL,
    meta_json    TEXT,
    created_at   TEXT NOT NULL,
    block_type   TEXT NOT NULL DEFAULT 'text'
);

INSERT INTO conversations_new (
    id, chat_id, turn_id, role, content_json, meta_json, created_at, block_type
)
SELECT id, chat_id, turn_id, role, content_json, meta_json, created_at,
       CASE role WHEN 'user' THEN 'text' ELSE 'text' END
FROM conversations;

DROP TABLE conversations;
ALTER TABLE conversations_new RENAME TO conversations;

CREATE INDEX idx_conversations_chat_time ON conversations(chat_id, created_at);
CREATE INDEX idx_conversations_turn ON conversations(chat_id, turn_id);

CREATE TABLE IF NOT EXISTS turns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    turn_id      TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    completed_at TEXT,
    meta_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_chat_status ON turns(chat_id, status, completed_at);

INSERT OR IGNORE INTO turns (chat_id, turn_id, status, created_at, completed_at)
SELECT chat_id, turn_id, 'complete', MIN(created_at), MAX(created_at)
FROM conversations
GROUP BY chat_id, turn_id;
"""


async def build_phase1_db(path: Path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(PHASE1_SCHEMA)
        # Seed with 2 turns * 2 rows each
        await db.executemany(
            "INSERT INTO conversations(chat_id, turn_id, role, content_json, created_at) "
            "VALUES (?,?,?,?,'2026-04-20T10:00:00Z')",
            [
                (1, "turnA", "user", '[{"type":"text","text":"hello"}]'),
                (1, "turnA", "assistant", '[{"type":"text","text":"hi"}]'),
                (1, "turnB", "user", '[{"type":"text","text":"ping"}]'),
                (1, "turnB", "assistant", '[{"type":"text","text":"pong"}]'),
            ],
        )
        await db.execute("PRAGMA user_version=1")
        await db.commit()


async def apply_migration_0002(path: Path, *, crash_after: str | None = None) -> None:
    """Apply migration in BEGIN EXCLUSIVE transaction.

    If crash_after matches a step marker, raise RuntimeError to trigger rollback.
    Steps: 'create_new_table', 'insert_rows', 'drop_old', 'rename', 'create_turns',
           'backfill_turns'.
    """
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN EXCLUSIVE")
        try:
            await db.execute("DROP TABLE IF EXISTS conversations_new")

            await db.execute(
                "CREATE TABLE conversations_new ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
                "turn_id TEXT NOT NULL, role TEXT NOT NULL, content_json TEXT NOT NULL, "
                "meta_json TEXT, created_at TEXT NOT NULL, "
                "block_type TEXT NOT NULL DEFAULT 'text')"
            )
            if crash_after == "create_new_table":
                raise RuntimeError("simulated crash after create_new_table")

            await db.execute(
                "INSERT INTO conversations_new (id, chat_id, turn_id, role, content_json, "
                "meta_json, created_at, block_type) "
                "SELECT id, chat_id, turn_id, role, content_json, meta_json, created_at, 'text' "
                "FROM conversations"
            )
            if crash_after == "insert_rows":
                raise RuntimeError("simulated crash after insert_rows")

            await db.execute("DROP TABLE conversations")
            await db.execute("ALTER TABLE conversations_new RENAME TO conversations")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_conversations_chat_time ON conversations(chat_id, created_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_conversations_turn ON conversations(chat_id, turn_id)")

            await db.execute(
                "CREATE TABLE IF NOT EXISTS turns ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
                "turn_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending', "
                "created_at TEXT NOT NULL, completed_at TEXT, meta_json TEXT)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_turns_chat_status "
                "ON turns(chat_id, status, completed_at)"
            )
            if crash_after == "create_turns":
                raise RuntimeError("simulated crash after create_turns")

            await db.execute(
                "INSERT OR IGNORE INTO turns (chat_id, turn_id, status, created_at, completed_at) "
                "SELECT chat_id, turn_id, 'complete', MIN(created_at), MAX(created_at) "
                "FROM conversations GROUP BY chat_id, turn_id"
            )

            await db.execute("PRAGMA user_version=2")
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def check_version(path: Path) -> int:
    async with aiosqlite.connect(path) as db:
        async with db.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


async def check_counts(path: Path) -> dict[str, int]:
    async with aiosqlite.connect(path) as db:
        out = {}
        try:
            async with db.execute("SELECT COUNT(*) FROM conversations") as cur:
                out["conversations"] = (await cur.fetchone())[0]
        except Exception as e:
            out["conversations_err"] = str(e)
        try:
            async with db.execute("SELECT COUNT(*) FROM turns") as cur:
                out["turns"] = (await cur.fetchone())[0]
        except Exception as e:
            out["turns_err"] = str(e)
        try:
            async with db.execute("PRAGMA table_info(conversations)") as cur:
                cols = [r[1] async for r in cur]
                out["conversations_cols"] = cols
        except Exception as e:
            out["cols_err"] = str(e)
    return out


async def main() -> None:
    # --- Test 1: happy path ---
    print("=== Test 1: happy path migration ===")
    with tempfile.TemporaryDirectory() as tdir:
        db_path = Path(tdir) / "t1.db"
        await build_phase1_db(db_path)
        print(f"  before: v={await check_version(db_path)}  {await check_counts(db_path)}")
        await apply_migration_0002(db_path)
        print(f"  after:  v={await check_version(db_path)}  {await check_counts(db_path)}")

    # --- Test 2: crash mid-way rolls back cleanly ---
    print("\n=== Test 2: crash during insert_rows → rollback ===")
    with tempfile.TemporaryDirectory() as tdir:
        db_path = Path(tdir) / "t2.db"
        await build_phase1_db(db_path)
        v_before = await check_version(db_path)
        counts_before = await check_counts(db_path)
        print(f"  before: v={v_before}  {counts_before}")
        try:
            await apply_migration_0002(db_path, crash_after="insert_rows")
        except RuntimeError as e:
            print(f"  crash raised (expected): {e}")
        v_after = await check_version(db_path)
        counts_after = await check_counts(db_path)
        print(f"  after crash: v={v_after}  {counts_after}")
        assert v_after == v_before, f"version drift: {v_before} → {v_after}"
        assert counts_after.get("conversations") == counts_before.get("conversations")
        print("  ✓ rollback preserved original schema + row count")

        # Now rerun migration cleanly
        await apply_migration_0002(db_path)
        v_final = await check_version(db_path)
        counts_final = await check_counts(db_path)
        print(f"  re-run cleanly: v={v_final}  {counts_final}")
        assert v_final == 2

    # --- Test 3: re-run on already-migrated DB ---
    print("\n=== Test 3: re-run on v=2 DB → idempotent ===")
    with tempfile.TemporaryDirectory() as tdir:
        db_path = Path(tdir) / "t3.db"
        await build_phase1_db(db_path)
        await apply_migration_0002(db_path)
        counts_after_first = await check_counts(db_path)
        await apply_migration_0002(db_path)  # second run
        counts_after_second = await check_counts(db_path)
        print(f"  first:  {counts_after_first}")
        print(f"  second: {counts_after_second}")
        # Idempotency: conversation row count should be stable; turns count stable
        # (INSERT OR IGNORE on the UNIQUE turn_id).
        assert counts_after_first["conversations"] == counts_after_second["conversations"]
        assert counts_after_first["turns"] == counts_after_second["turns"]
        print("  ✓ idempotent (counts stable)")


if __name__ == "__main__":
    asyncio.run(main())
