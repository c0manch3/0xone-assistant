from __future__ import annotations

from pathlib import Path

from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect


async def test_cleanup_orphan_pending_turns(tmp_path: Path) -> None:
    """S10: pending turns (left by a prior crash) become 'interrupted'."""
    db = tmp_path / "orphan.db"
    conn = await connect(db)
    await apply_schema(conn)
    store = ConversationStore(conn)

    # Simulate two pending turns (as if the daemon crashed mid-turn twice).
    t_a = await store.start_turn(1)
    t_b = await store.start_turn(2)

    # Baseline: both in 'pending'.
    async with conn.execute("SELECT turn_id, status FROM turns ORDER BY id") as cur:
        rows = await cur.fetchall()
    assert {(r[0], r[1]) for r in rows} == {(t_a, "pending"), (t_b, "pending")}

    # Cleanup.
    count = await store.cleanup_orphan_pending_turns()
    assert count == 2

    async with conn.execute("SELECT turn_id, status FROM turns ORDER BY id") as cur:
        rows = await cur.fetchall()
    statuses = {r[0]: r[1] for r in rows}
    assert statuses[t_a] == "interrupted"
    assert statuses[t_b] == "interrupted"

    # Idempotent: running again affects zero rows.
    count2 = await store.cleanup_orphan_pending_turns()
    assert count2 == 0

    await conn.close()


async def test_cleanup_leaves_complete_turns_alone(tmp_path: Path) -> None:
    db = tmp_path / "mixed.db"
    conn = await connect(db)
    await apply_schema(conn)
    store = ConversationStore(conn)

    t_good = await store.start_turn(1)
    await store.complete_turn(t_good, meta={})
    t_pending = await store.start_turn(1)

    count = await store.cleanup_orphan_pending_turns()
    assert count == 1

    async with conn.execute("SELECT status FROM turns WHERE turn_id=?", (t_good,)) as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == "complete"

    async with conn.execute("SELECT status FROM turns WHERE turn_id=?", (t_pending,)) as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == "interrupted"
    await conn.close()
