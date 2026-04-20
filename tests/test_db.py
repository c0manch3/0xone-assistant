from __future__ import annotations

from pathlib import Path

from assistant.state.conversations import ConversationStore
from assistant.state.db import SCHEMA_VERSION, apply_schema, connect


async def test_schema_bootstrap(tmp_path: Path) -> None:
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

    # Idempotency
    await apply_schema(conn)

    store = ConversationStore(conn)
    turn = ConversationStore.new_turn_id()
    row_id = await store.append(42, turn, "user", [{"type": "text", "text": "hi"}])
    assert row_id == 1
    await conn.close()
