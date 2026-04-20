"""Phase-8 production fix-pack regression.

Before this fix, `history_to_user_envelopes` emitted ONLY `type=user`
envelopes and dropped every assistant `TextBlock`. The replayed SDK
session saw N user questions with zero assistant answers and treated
each message as first contact, responding with an identical canned
greeting every turn.

These tests pin the new invariant: per turn, the stream yields a user
envelope and, when the assistant produced any text, a following
assistant envelope carrying the concatenated TextBlock content.
"""

from __future__ import annotations

from pathlib import Path

from assistant.bridge.history import history_to_user_envelopes
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from tests._helpers.history_seed import (
    seed_assistant_text_row,
    seed_user_text_row,
)


async def test_assistant_reply_emitted_after_user_envelope(tmp_path: Path) -> None:
    """Single turn with one user question and one assistant reply -> two envelopes."""
    conn = await connect(tmp_path / "assistant-single.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    chat_id = 91

    await seed_user_text_row(conn, chat_id=chat_id, turn_id="t1", text="привет")
    await seed_assistant_text_row(conn, chat_id=chat_id, turn_id="t1", text="Ответ")

    rows = await conv.load_recent(chat_id, limit_turns=10)
    envelopes = list(history_to_user_envelopes(rows, chat_id))

    assert len(envelopes) == 2
    assert envelopes[0]["type"] == "user"
    assert envelopes[0]["message"]["role"] == "user"
    # Single-user-text turn collapses content to a bare string.
    assert envelopes[0]["message"]["content"] == "привет"
    assert envelopes[0]["session_id"] == f"chat-{chat_id}"

    assert envelopes[1]["type"] == "assistant"
    assert envelopes[1]["message"]["role"] == "assistant"
    assistant_content = envelopes[1]["message"]["content"]
    assert isinstance(assistant_content, list)
    assert assistant_content[0]["type"] == "text"
    assert "Ответ" in assistant_content[0]["text"]
    assert envelopes[1]["session_id"] == f"chat-{chat_id}"
    assert envelopes[1]["parent_tool_use_id"] is None

    await conn.close()


async def test_multiple_turns_interleaved_user_assistant(tmp_path: Path) -> None:
    """Three turns each with user+assistant text -> six alternating envelopes."""
    conn = await connect(tmp_path / "assistant-multi.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    chat_id = 92

    pairs = [("q1", "a1"), ("q2", "a2"), ("q3", "a3")]
    for i, (q, a) in enumerate(pairs):
        turn_id = f"t{i}"
        await seed_user_text_row(conn, chat_id=chat_id, turn_id=turn_id, text=q)
        await seed_assistant_text_row(conn, chat_id=chat_id, turn_id=turn_id, text=a)

    rows = await conv.load_recent(chat_id, limit_turns=10)
    envelopes = list(history_to_user_envelopes(rows, chat_id))

    assert len(envelopes) == 6
    roles = [e["message"]["role"] for e in envelopes]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
    types = [e["type"] for e in envelopes]
    assert types == ["user", "assistant", "user", "assistant", "user", "assistant"]

    # Verify pairing by content — each (user, assistant) pair matches its seed.
    for idx, (q, a) in enumerate(pairs):
        user_env = envelopes[idx * 2]
        assistant_env = envelopes[idx * 2 + 1]
        assert user_env["message"]["content"] == q
        assistant_content = assistant_env["message"]["content"]
        assert isinstance(assistant_content, list)
        assert assistant_content[0]["text"] == a

    await conn.close()


async def test_empty_assistant_turn_skipped(tmp_path: Path) -> None:
    """Turn with user text but no assistant reply -> only the user envelope."""
    conn = await connect(tmp_path / "assistant-empty.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    chat_id = 93

    await seed_user_text_row(conn, chat_id=chat_id, turn_id="t1", text="q")

    rows = await conv.load_recent(chat_id, limit_turns=10)
    envelopes = list(history_to_user_envelopes(rows, chat_id))

    assert len(envelopes) == 1
    assert envelopes[0]["type"] == "user"
    assert envelopes[0]["message"]["role"] == "user"
    assert envelopes[0]["message"]["content"] == "q"

    await conn.close()
