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

    await apply_schema(conn)

    store = ConversationStore(conn)
    turn = ConversationStore.new_turn_id()
    row_id = await store.append(42, turn, "user", [{"type": "text", "text": "hi"}])
    assert row_id == 1
    await conn.close()


async def test_append_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "roundtrip.db"
    conn = await connect(db)
    await apply_schema(conn)
    store = ConversationStore(conn)

    turn = ConversationStore.new_turn_id()
    blocks = [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "name": "x", "input": {"p": Path("/tmp")}},
    ]
    await store.append(1234, turn, "assistant", blocks, meta={"src": Path("/var/log")})

    rows = await store.load_recent(1234, 10)
    assert len(rows) == 1
    row = rows[0]
    assert row["chat_id"] == 1234
    assert row["turn_id"] == turn
    assert row["role"] == "assistant"
    assert isinstance(row["content"], list)
    assert len(row["content"]) == 2
    assert row["content"][0] == {"type": "text", "text": "hi"}
    assert row["content"][1]["type"] == "tool_use"
    assert row["content"][1]["input"]["p"] == "/tmp"
    assert row["meta"] == {"src": "/var/log"}
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
