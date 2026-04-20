from __future__ import annotations

from pathlib import Path

from assistant.state.conversations import ConversationStore
from assistant.state.db import SCHEMA_VERSION, apply_schema, connect


async def test_schema_bootstrap(tmp_path: Path) -> None:
    """Phase-1 schema smoke, adjusted for phase-2 (schema v2).

    Verifies: WAL mode on, user_version == SCHEMA_VERSION (2 in phase 2),
    apply_schema idempotent, append() returns a valid row id.
    """
    db = tmp_path / "test.db"
    conn = await connect(db)
    await apply_schema(conn)

    async with conn.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == "wal"

    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION
        assert SCHEMA_VERSION == 2

    # Idempotency
    await apply_schema(conn)

    store = ConversationStore(conn)
    # Phase-2 requires a turns row before append (FK-less, but load_recent
    # is turn-scoped). For this phase-1-style smoke we still exercise the
    # row insert directly — a naked conversations row is valid; load_recent
    # filtering just skips it.
    turn = ConversationStore.new_turn_id()
    row_id = await store.append(
        42, turn, "user", [{"type": "text", "text": "hi"}], block_type="text"
    )
    assert row_id == 1
    await conn.close()
