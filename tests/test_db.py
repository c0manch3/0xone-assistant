from __future__ import annotations

from pathlib import Path

from assistant.state.conversations import ConversationStore
from assistant.state.db import SCHEMA_VERSION, apply_schema, connect
from assistant.state.turns import TurnStore


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

    # Idempotent re-apply.
    await apply_schema(conn)

    # `turns` table exists.
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='turns'"
    ) as cur:
        assert await cur.fetchone() is not None

    # `conversations.block_type` column exists.
    async with conn.execute("PRAGMA table_info(conversations)") as cur:
        cols = [r[1] for r in await cur.fetchall()]
    assert "block_type" in cols
    assert "meta_json" not in cols  # migrated to turns

    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)
    turn = await turns.start(42)
    row_id = await conv.append(
        42, turn, "user", [{"type": "text", "text": "hi"}], block_type="text"
    )
    assert row_id == 1
    await conn.close()


async def test_append_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "roundtrip.db"
    conn = await connect(db)
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    turn = await turns.start(1234)
    blocks = [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "name": "x", "input": {"p": Path("/tmp")}},
    ]
    await conv.append(1234, turn, "assistant", blocks, block_type="tool_use")
    await turns.complete(turn, meta={"model": "opus", "cost": 0.01})

    rows = await conv.load_recent(1234, 10)
    assert len(rows) == 1
    row = rows[0]
    assert row["chat_id"] == 1234
    assert row["turn_id"] == turn
    assert row["role"] == "assistant"
    assert row["block_type"] == "tool_use"
    assert isinstance(row["content"], list)
    assert len(row["content"]) == 2
    assert row["content"][0] == {"type": "text", "text": "hi"}
    assert row["content"][1]["type"] == "tool_use"
    assert row["content"][1]["input"]["p"] == "/tmp"
    assert isinstance(row["created_at"], str)

    await conn.close()


async def test_apply_schema_reopen(tmp_path: Path) -> None:
    db = tmp_path / "reopen.db"

    conn1 = await connect(db)
    await apply_schema(conn1)
    await conn1.close()

    conn2 = await connect(db)
    await apply_schema(conn2)
    async with conn2.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION
    await conn2.close()
