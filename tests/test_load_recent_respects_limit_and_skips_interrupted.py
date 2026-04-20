from __future__ import annotations

from pathlib import Path

from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect


async def test_load_recent_is_turn_limited(tmp_path: Path) -> None:
    """B6: load_recent limits by TURN count, not row count.

    Build 4 complete turns, each with 3 rows (12 rows total). limit=2
    must return all rows of the most recent 2 turns = 6 rows.
    """
    db = tmp_path / "turn_limit.db"
    conn = await connect(db)
    await apply_schema(conn)
    store = ConversationStore(conn)

    turn_ids: list[str] = []
    for _ in range(4):
        t = await store.start_turn(1)
        for _ in range(3):
            await store.append(
                1,
                t,
                "user",
                [{"type": "text", "text": "x"}],
                block_type="text",
            )
        await store.complete_turn(t, meta={"stop_reason": "end_turn"})
        turn_ids.append(t)

    rows = await store.load_recent(1, 2)
    seen = {r["turn_id"] for r in rows}
    assert seen == {turn_ids[-1], turn_ids[-2]}
    assert len(rows) == 6
    await conn.close()


async def test_load_recent_skips_interrupted_and_pending(tmp_path: Path) -> None:
    """Turns with status='pending' or 'interrupted' must not appear in
    load_recent output — the rows attached to them would form a broken
    tool_use/tool_result pair otherwise."""
    db = tmp_path / "skip.db"
    conn = await connect(db)
    await apply_schema(conn)
    store = ConversationStore(conn)

    # Completed turn
    t_ok = await store.start_turn(1)
    await store.append(1, t_ok, "user", [{"type": "text", "text": "hi"}], block_type="text")
    await store.complete_turn(t_ok, meta={})

    # Pending turn (never completed)
    t_pending = await store.start_turn(1)
    await store.append(
        1,
        t_pending,
        "assistant",
        [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}],
        block_type="tool_use",
    )

    # Interrupted turn
    t_bad = await store.start_turn(1)
    await store.append(1, t_bad, "user", [{"type": "text", "text": "bye"}], block_type="text")
    await store.interrupt_turn(t_bad)

    rows = await store.load_recent(1, 20)
    seen = {r["turn_id"] for r in rows}
    assert seen == {t_ok}
    await conn.close()


async def test_load_recent_chronological_within_turn_set(tmp_path: Path) -> None:
    """Rows come back in ASC-by-id order, i.e. the temporal insertion order."""
    db = tmp_path / "order.db"
    conn = await connect(db)
    await apply_schema(conn)
    store = ConversationStore(conn)

    t = await store.start_turn(1)
    ids = []
    for i in range(5):
        row_id = await store.append(
            1,
            t,
            "user",
            [{"type": "text", "text": str(i)}],
            block_type="text",
        )
        ids.append(row_id)
    await store.complete_turn(t, meta={})

    rows = await store.load_recent(1, 5)
    assert [r["id"] for r in rows] == ids
    await conn.close()
