from __future__ import annotations

from pathlib import Path

from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore


async def test_interrupted_turn_excluded_from_history(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "interrupt.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)
    chat_id = 11

    # Two complete turns.
    good_ids: list[str] = []
    for i in range(2):
        tid = await turns.start(chat_id)
        good_ids.append(tid)
        await conv.append(
            chat_id, tid, "user", [{"type": "text", "text": f"q{i}"}], block_type="text"
        )
        await conv.append(
            chat_id,
            tid,
            "assistant",
            [{"type": "text", "text": f"a{i}"}],
            block_type="text",
        )
        await turns.complete(tid)

    # Newest turn interrupted (orphan tool_use → must not be replayed).
    bad = await turns.start(chat_id)
    await conv.append(chat_id, bad, "user", [{"type": "text", "text": "q2"}], block_type="text")
    await conv.append(
        chat_id,
        bad,
        "assistant",
        [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}],
        block_type="tool_use",
    )
    await turns.interrupt(bad)

    rows = await conv.load_recent(chat_id, limit_turns=10)
    turn_ids = {row["turn_id"] for row in rows}
    assert bad not in turn_ids
    assert turn_ids == set(good_ids)

    # Interrupted rows are still physically in the DB (traceability).
    async with conn.execute("SELECT COUNT(*) FROM conversations WHERE turn_id = ?", (bad,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 2

    async with conn.execute("SELECT status FROM turns WHERE turn_id = ?", (bad,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "interrupted"

    await conn.close()
