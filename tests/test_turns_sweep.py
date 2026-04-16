"""Startup sweeper test: pending turns from a previous run get interrupted."""

from __future__ import annotations

from pathlib import Path

from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore


async def test_sweep_pending_marks_stale_turns_interrupted(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "s.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    # 1 complete + 2 pending (simulating a crashed previous process).
    good = await turns.start(1)
    await turns.complete(good)
    leftover_a = await turns.start(1)
    leftover_b = await turns.start(2)

    swept = await turns.sweep_pending()
    assert swept == 2

    async with conn.execute("SELECT turn_id, status FROM turns ORDER BY rowid") as cur:
        rows = await cur.fetchall()
    by_id = {r[0]: r[1] for r in rows}
    assert by_id[good] == "complete"
    assert by_id[leftover_a] == "interrupted"
    assert by_id[leftover_b] == "interrupted"

    # Idempotent: a second sweep should sweep zero rows.
    again = await turns.sweep_pending()
    assert again == 0

    await conn.close()
