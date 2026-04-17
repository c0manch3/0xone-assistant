"""Phase 4 Q1: synthetic history summary carries tool_result snippets."""

from __future__ import annotations

from pathlib import Path

from assistant.bridge.history import (
    _render_tool_summary,
    _stringify_tool_result_content,
    history_to_user_envelopes,
)
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from tests._helpers.history_seed import (
    seed_tool_result_row,
    seed_tool_use_row,
    seed_user_text_row,
)


async def test_short_snippet_passes_through(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "h1.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    chat_id = 1

    await seed_user_text_row(conn, chat_id=chat_id, turn_id="t1", text="q")
    await seed_tool_use_row(
        conn,
        chat_id=chat_id,
        turn_id="t1",
        tool_use_id="tu1",
        tool_name="memory",
    )
    await seed_tool_result_row(
        conn,
        chat_id=chat_id,
        turn_id="t1",
        tool_use_id="tu1",
        content='{"hits":[{"path":"inbox/a.md"}]}',
    )
    await seed_user_text_row(conn, chat_id=chat_id, turn_id="t2", text="next")

    rows = await conv.load_recent(chat_id, limit_turns=10)
    envelopes = list(history_to_user_envelopes(rows, chat_id))

    assert len(envelopes) == 2
    t1_content = envelopes[0]["message"]["content"]
    assert isinstance(t1_content, list)
    note = t1_content[0]["text"]
    assert "[system-note:" in note
    assert "результат memory:" in note
    assert '{"hits":[{"path":"inbox/a.md"}]}' in note

    await conn.close()


async def test_long_snippet_truncated_with_marker(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "h2.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    chat_id = 2
    body = "x" * 5000

    await seed_user_text_row(conn, chat_id=chat_id, turn_id="t1", text="q")
    await seed_tool_use_row(
        conn,
        chat_id=chat_id,
        turn_id="t1",
        tool_use_id="tu1",
        tool_name="Bash",
    )
    await seed_tool_result_row(
        conn,
        chat_id=chat_id,
        turn_id="t1",
        tool_use_id="tu1",
        content=body,
    )

    rows = await conv.load_recent(chat_id, limit_turns=10)
    envelopes = list(history_to_user_envelopes(rows, chat_id, tool_result_truncate=2000))
    note = envelopes[0]["message"]["content"][0]["text"]
    assert "...(truncated)" in note
    # Strict: the whole 5000-char body MUST NOT end up in the note.
    assert body not in note
    # At least 2000 chars of 'x' should still be present.
    assert "x" * 2000 in note

    await conn.close()


async def test_is_error_renders_error_prefix(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "h3.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    chat_id = 3

    await seed_user_text_row(conn, chat_id=chat_id, turn_id="t1", text="q")
    await seed_tool_use_row(
        conn,
        chat_id=chat_id,
        turn_id="t1",
        tool_use_id="tu1",
        tool_name="Bash",
    )
    await seed_tool_result_row(
        conn,
        chat_id=chat_id,
        turn_id="t1",
        tool_use_id="tu1",
        content="Exit code 1\nboom",
        is_error=True,
    )

    rows = await conv.load_recent(chat_id, limit_turns=10)
    envelopes = list(history_to_user_envelopes(rows, chat_id))
    note = envelopes[0]["message"]["content"][0]["text"]
    assert "ошибка Bash:" in note
    assert "Exit code 1" in note
    assert "результат Bash:" not in note

    await conn.close()


async def test_cyrillic_preserved(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "h4.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    chat_id = 4

    await seed_user_text_row(conn, chat_id=chat_id, turn_id="t1", text="q")
    await seed_tool_use_row(
        conn,
        chat_id=chat_id,
        turn_id="t1",
        tool_use_id="tu1",
        tool_name="memory",
    )
    await seed_tool_result_row(
        conn,
        chat_id=chat_id,
        turn_id="t1",
        tool_use_id="tu1",
        content="найдено 3 заметки: жена жене женой",
    )

    rows = await conv.load_recent(chat_id, limit_turns=10)
    envelopes = list(history_to_user_envelopes(rows, chat_id))
    note = envelopes[0]["message"]["content"][0]["text"]
    assert "жена жене женой" in note

    await conn.close()


def test_stringify_defensive_list_path() -> None:
    result = _stringify_tool_result_content(
        [
            {"type": "text", "text": "A"},
            {"type": "image", "url": "x"},
            {"type": "text", "text": "B"},
        ]
    )
    assert result == "A[image block]B"


def test_stringify_none() -> None:
    assert _stringify_tool_result_content(None) == ""


def test_stringify_bytes() -> None:
    assert _stringify_tool_result_content(b"\x00\x01") == "[binary content: 2B]"


def test_render_tool_summary_shape() -> None:
    out = _render_tool_summary(
        tool_names=["memory", "Bash"],
        tool_results_by_name={
            "memory": [{"content": "hit", "is_error": False}],
            "Bash": [{"content": "boom", "is_error": True}],
        },
        truncate=2000,
    )
    assert out.startswith("[system-note:")
    assert out.endswith("]")
    assert "в прошлом ходе вызваны инструменты: memory, Bash." in out
    assert "результат memory: hit" in out
    assert "ошибка Bash: boom" in out
    assert "Для полного вывода" in out
