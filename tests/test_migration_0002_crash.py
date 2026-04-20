from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiosqlite
import pytest

from assistant.state.db import apply_schema, connect


async def _user_version(conn: aiosqlite.Connection) -> int:
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_crash_during_migration_rolls_back(tmp_path: Path) -> None:
    """Simulate a crash mid-migration; verify ROLLBACK preserves v=1 state,
    and that re-running converges to v=2 with data intact."""
    db = tmp_path / "crash.db"
    conn = await connect(db)

    # Hand-build a v=1 DB with real data.
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
    for i in range(3):
        await conn.execute(
            "INSERT INTO conversations(chat_id, turn_id, role, content_json) "
            "VALUES (?, ?, 'user', '[]')",
            (i, f"turn-{i}"),
        )
    await conn.commit()

    # Patch aiosqlite.Connection.execute so the ALTER TABLE ... RENAME step
    # inside migration 0002 raises. We raise BEFORE user_version=2 is set.
    real_execute = aiosqlite.Connection.execute

    def exploding_execute(self: aiosqlite.Connection, sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql.startswith("ALTER TABLE conversations_new RENAME"):
            raise RuntimeError("simulated power loss mid-migration")
        return real_execute(self, sql, *args, **kwargs)

    with (
        patch.object(aiosqlite.Connection, "execute", exploding_execute),
        pytest.raises(RuntimeError, match="simulated power loss"),
    ):
        await apply_schema(conn)

    # After rollback: still v=1, conversations table intact, no turns table.
    assert await _user_version(conn) == 1

    async with conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='turns'"
    ) as cur:
        turns_exists = (await cur.fetchone())[0]  # type: ignore[index]
    assert turns_exists == 0

    async with conn.execute("SELECT COUNT(*) FROM conversations") as cur:
        (count,) = await cur.fetchone()  # type: ignore[misc]
    assert count == 3

    # Re-run — should converge to v=2.
    await apply_schema(conn)
    assert await _user_version(conn) == 2

    async with conn.execute("SELECT COUNT(*) FROM turns") as cur:
        (turns_count,) = await cur.fetchone()  # type: ignore[misc]
    assert turns_count == 3

    await conn.close()
