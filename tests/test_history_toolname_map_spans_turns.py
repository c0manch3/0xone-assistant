"""Phase 4 B2 regression: global tool_use_id -> tool_name map.

SDK pattern: tool_use in assistant turn N, tool_result in USER turn N+1.
Phase 2's handler allocates one turn per user message, so those rows live
under different `turn_id`s. A per-turn tool-name lookup silently dropped
every tool_result whose partner tool_use was in a preceding turn — the
resulting synthetic note rendered "unknown" for every snippet.
"""

from __future__ import annotations

from pathlib import Path

from assistant.bridge.history import history_to_user_envelopes
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from tests._helpers.history_seed import (
    seed_tool_result_row,
    seed_tool_use_row,
    seed_user_text_row,
)


async def test_tool_name_resolved_across_turn_boundary(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "b2.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    chat_id = 77

    # Turn 1: user asks, assistant emits tool_use (NO tool_result yet).
    await seed_user_text_row(conn, chat_id=chat_id, turn_id="t1", text="что в inbox?")
    await seed_tool_use_row(
        conn,
        chat_id=chat_id,
        turn_id="t1",
        tool_use_id="TU1",
        tool_name="memory",
    )
    # Turn 2 (phase 2 handler layout): the tool_result arrives here —
    # different turn_id — plus a fresh user message.
    await seed_tool_result_row(
        conn,
        chat_id=chat_id,
        turn_id="t2",
        tool_use_id="TU1",
        content="snip",
    )
    await seed_user_text_row(conn, chat_id=chat_id, turn_id="t2", text="и дальше")

    rows = await conv.load_recent(chat_id, limit_turns=10)
    envelopes = list(history_to_user_envelopes(rows, chat_id))
    # Turn 2 note must credit "memory", not "unknown".
    t2_content = envelopes[-1]["message"]["content"]
    assert isinstance(t2_content, list)
    note = t2_content[0]["text"]
    assert "результат memory: snip" in note
    assert "unknown" not in note

    await conn.close()


async def test_unknown_fallback_when_tool_use_absent(tmp_path: Path) -> None:
    """If the matching tool_use is missing entirely, we honestly say `unknown`."""
    conn = await connect(tmp_path / "b2-missing.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    chat_id = 78

    await seed_user_text_row(conn, chat_id=chat_id, turn_id="t1", text="q")
    # No tool_use row; only an orphan tool_result.
    await seed_tool_result_row(
        conn,
        chat_id=chat_id,
        turn_id="t1",
        tool_use_id="orphan",
        content="lost",
    )

    rows = await conv.load_recent(chat_id, limit_turns=10)
    envelopes = list(history_to_user_envelopes(rows, chat_id))
    note = envelopes[0]["message"]["content"][0]["text"]
    assert "результат unknown: lost" in note

    await conn.close()
