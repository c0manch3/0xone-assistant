from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import ClaudeBridge, ClaudeBridgeError
from assistant.config import ClaudeSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect


# ---------------------------------------------------------------------------
# Lightweight stand-ins. The handler only calls ``bridge.ask(...)``; we
# don't need a real SDK connection.
# ---------------------------------------------------------------------------
@dataclass
class _FakeTextBlock:
    """Duck-types ``claude_agent_sdk.TextBlock`` for ``isinstance`` in
    ``_classify_block`` — we use the real imported class instead, see
    below; this alias is only for readability in the stream factory."""

    text: str


class _FakeBridge(ClaudeBridge):
    """Substitute ``ask`` with a scripted generator but preserve everything
    else (mostly ``_sem`` etc.) from the real bridge. The handler doesn't
    touch any other method.
    """

    def __init__(self, settings: Settings, script: list[Any] | Exception) -> None:
        super().__init__(settings)
        self._script = script

    async def ask(  # type: ignore[override]
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        if isinstance(self._script, Exception):
            raise self._script
        for item in self._script:
            yield item


def _settings(project_root: Path) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=project_root,
        data_dir=project_root / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
    )


async def _make_store(tmp_path: Path) -> ConversationStore:
    db = tmp_path / "handler.db"
    conn = await connect(db)
    await apply_schema(conn)
    return ConversationStore(conn)


def _make_emit() -> tuple[list[str], Any]:
    emitted: list[str] = []

    async def emit(text: str) -> None:
        emitted.append(text)

    return emitted, emit


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
async def test_handler_happy_path(tmp_path: Path) -> None:
    """SW5: handler orchestration — start_turn → bridge.ask →
    classify → persist every block → complete_turn on ResultMessage.

    The user row and the assistant text row must both land in
    ``conversations``; ``turns`` row must transition pending → complete.
    ``emit`` receives the assistant text.
    """
    from claude_agent_sdk import ResultMessage, TextBlock

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)

    text_block = TextBlock(text="Hi there")
    result = ResultMessage(
        subtype="success",
        duration_ms=123,
        duration_api_ms=100,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
        total_cost_usd=0.001,
        usage={"input_tokens": 10, "output_tokens": 5},
        stop_reason="end_turn",
    )
    bridge = _FakeBridge(settings, [text_block, result])
    handler = ClaudeHandler(settings, store, bridge)

    emitted, emit = _make_emit()
    msg = IncomingMessage(chat_id=42, message_id=1, text="Hello")
    await handler.handle(msg, emit)

    assert emitted == ["Hi there"]

    # ``turns`` row exists, status='complete'.
    async with store._conn.execute(
        "SELECT status FROM turns WHERE chat_id=42"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "complete"

    # ``conversations`` carries the user row + the assistant text block.
    async with store._conn.execute(
        "SELECT role, block_type FROM conversations "
        "WHERE chat_id=42 ORDER BY id"
    ) as cur:
        conv_rows = await cur.fetchall()
    assert [(r[0], r[1]) for r in conv_rows] == [
        ("user", "text"),
        ("assistant", "text"),
    ]

    await store._conn.close()


# ---------------------------------------------------------------------------
# Bridge error — timeout / SDK failure → error suffix + interrupt status
# ---------------------------------------------------------------------------
async def test_handler_bridge_error_marks_turn_interrupted(tmp_path: Path) -> None:
    """SW5: ``ClaudeBridgeError`` from the bridge (e.g. timeout) is
    caught; the handler emits a user-visible error suffix and the turn
    row is marked ``interrupted`` in the ``finally`` clause."""
    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)

    bridge = _FakeBridge(settings, ClaudeBridgeError("timeout"))
    handler = ClaudeHandler(settings, store, bridge)

    emitted, emit = _make_emit()
    msg = IncomingMessage(chat_id=7, message_id=2, text="stall please")
    await handler.handle(msg, emit)

    # Error suffix surfaced to the user.
    assert len(emitted) == 1
    assert "ошибка" in emitted[0]
    assert "timeout" in emitted[0]

    # Turn row marked interrupted.
    async with store._conn.execute(
        "SELECT status FROM turns WHERE chat_id=7"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "interrupted"

    # User row was still persisted before the bridge was called.
    async with store._conn.execute(
        "SELECT role FROM conversations WHERE chat_id=7"
    ) as cur:
        conv_rows = await cur.fetchall()
    assert len(conv_rows) == 1
    assert conv_rows[0][0] == "user"

    await store._conn.close()


# ---------------------------------------------------------------------------
# Interrupt path — CancelledError mid-stream
# ---------------------------------------------------------------------------
async def test_handler_interrupt_during_stream(tmp_path: Path) -> None:
    """SW5: if the stream is cancelled mid-flight (Telegram shutdown, user
    navigates away), the handler's ``finally`` must still mark the turn
    ``interrupted`` and the partial text block must be persisted."""
    from claude_agent_sdk import TextBlock

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)

    partial = TextBlock(text="partial answer")

    class _CancellingBridge(ClaudeBridge):
        async def ask(  # type: ignore[override]
            self,
            chat_id: int,
            user_text: str,
            history: list[dict[str, Any]],
        ) -> AsyncIterator[Any]:
            yield partial
            raise BaseException("simulated cancellation")

    bridge = _CancellingBridge(settings)
    handler = ClaudeHandler(settings, store, bridge)

    emitted, emit = _make_emit()
    msg = IncomingMessage(chat_id=11, message_id=3, text="will be cut")
    with pytest.raises(BaseException, match="simulated cancellation"):
        await handler.handle(msg, emit)

    # Partial text emitted before cancellation.
    assert emitted == ["partial answer"]

    # Turn marked interrupted in finally.
    async with store._conn.execute(
        "SELECT status FROM turns WHERE chat_id=11"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "interrupted"

    # Both user row AND the partial assistant block persisted.
    async with store._conn.execute(
        "SELECT role, block_type FROM conversations "
        "WHERE chat_id=11 ORDER BY id"
    ) as cur:
        conv_rows = await cur.fetchall()
    assert [(r[0], r[1]) for r in conv_rows] == [
        ("user", "text"),
        ("assistant", "text"),
    ]

    await store._conn.close()


# ---------------------------------------------------------------------------
# Tool-use + tool-result round trip persists correctly
# ---------------------------------------------------------------------------
async def test_handler_persists_tool_use_and_result(tmp_path: Path) -> None:
    """SW5: classify_block maps ToolResultBlock to role='user' and
    ToolUseBlock to role='assistant' with proper block_type. Verify the
    rows land in ``conversations`` with the right labels — a regression
    here would silently poison replay on the next turn."""
    from claude_agent_sdk import ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)

    tool_use = ToolUseBlock(id="tu_1", name="Read", input={"file_path": "README.md"})
    tool_res = ToolResultBlock(tool_use_id="tu_1", content="hello", is_error=False)
    final_text = TextBlock(text="done")
    result = ResultMessage(
        subtype="success",
        duration_ms=200,
        duration_api_ms=150,
        is_error=False,
        num_turns=2,
        session_id="sess-2",
        total_cost_usd=0.002,
        usage={"input_tokens": 20, "output_tokens": 8},
        stop_reason="end_turn",
    )
    bridge = _FakeBridge(settings, [tool_use, tool_res, final_text, result])
    handler = ClaudeHandler(settings, store, bridge)

    emitted, emit = _make_emit()
    msg = IncomingMessage(chat_id=21, message_id=4, text="read README")
    await handler.handle(msg, emit)

    assert emitted == ["done"]

    async with store._conn.execute(
        "SELECT role, block_type FROM conversations "
        "WHERE chat_id=21 ORDER BY id"
    ) as cur:
        rows = await cur.fetchall()
    assert [(r[0], r[1]) for r in rows] == [
        ("user", "text"),  # initial user prompt
        ("assistant", "tool_use"),  # model decided to call Read
        ("user", "tool_result"),  # tool result — role='user' per Anthropic API
        ("assistant", "text"),  # final text
    ]

    async with store._conn.execute(
        "SELECT status FROM turns WHERE chat_id=21"
    ) as cur:
        trow = await cur.fetchone()
    assert trow is not None and trow[0] == "complete"

    await store._conn.close()
