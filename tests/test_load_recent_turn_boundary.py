from __future__ import annotations

from pathlib import Path

from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore


async def test_load_recent_returns_whole_turns(tmp_path: Path) -> None:
    """3 turns x 5 rows; limit=2 -> exactly 10 rows of the last two turns."""
    conn = await connect(tmp_path / "boundary.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    chat_id = 7
    turn_ids: list[str] = []
    for t in range(3):
        tid = await turns.start(chat_id)
        turn_ids.append(tid)
        for b in range(5):
            await conv.append(
                chat_id,
                tid,
                "user" if b == 0 else "assistant",
                [{"type": "text", "text": f"turn{t}-block{b}"}],
                block_type="text",
            )
        await turns.complete(tid, meta={"i": t})

    rows = await conv.load_recent(chat_id, limit_turns=2)
    assert len(rows) == 10
    seen_turns = {row["turn_id"] for row in rows}
    # The newest two turns.
    assert seen_turns == {turn_ids[1], turn_ids[2]}

    # Ordering: rows are chronological (oldest-first).
    ids = [row["id"] for row in rows]
    assert ids == sorted(ids)

    await conn.close()


async def test_load_recent_full_limit(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "full.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    chat_id = 9
    for _ in range(3):
        tid = await turns.start(chat_id)
        await conv.append(chat_id, tid, "user", [{"type": "text", "text": "hi"}], block_type="text")
        await turns.complete(tid)

    rows = await conv.load_recent(chat_id, limit_turns=20)
    assert len(rows) == 3

    await conn.close()
