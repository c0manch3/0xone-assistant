"""S13 regression: after the A+B+C+D fix bundle, the full multi-iteration
SDK stream is persisted and the final user-visible marker actually reaches
the emit callback.

Symptom pre-fix: owner sent "use the ping skill", expected marker
"PONG-FROM-SKILL-OK", but kept seeing the stale greet "Йо. Чё делаем?"
instead. Forensics pointed at ``bridge.ask`` ``return``-ing after the
first ``ResultMessage`` and ``history_to_sdk_envelopes`` emitting one
envelope per prior row — together these caused every iteration beyond
the first to disappear into the journal without being persisted.

This test uses a fake bridge that yields the exact sequence the bug
dropped, and asserts:
  * every block type lands in ``conversations``;
  * the final text chunk (the marker) reaches the emit callback;
  * the turn row transitions to ``complete``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect


def _settings(project_root: Path) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=project_root,
        data_dir=project_root / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
    )


class _FakeBridge(ClaudeBridge):
    """Scripted bridge mimicking a multi-iteration Skill invocation.

    Stream layout:
      TextBlock  (pre-tool narration)
      ToolUseBlock  (model invokes the ping Skill)
      ToolResultBlock  (skill output — role 'user' at handler layer)
      TextBlock  (the marker the owner looks for)
      ResultMessage  (terminal)
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)

    async def ask(  # type: ignore[override]
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
    ) -> AsyncIterator[Any]:
        del system_notes  # phase-5 signature; test ignores
        yield TextBlock(text="let me check the ping")
        yield ToolUseBlock(id="tu_1", name="Skill", input={"skill": "ping"})
        yield ToolResultBlock(
            tool_use_id="tu_1",
            content="Launching skill: ping",
            is_error=False,
        )
        yield TextBlock(text="PONG-FROM-SKILL-OK\n\nPhase 2 skill plumbing жив.")
        yield ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=80,
            is_error=False,
            num_turns=2,
            session_id="fake-session-id",
            total_cost_usd=0.05,
            usage={"input_tokens": 100, "output_tokens": 50},
            stop_reason="end_turn",
        )


async def test_full_sequence_persisted_and_marker_emitted(tmp_path: Path) -> None:
    """All blocks persisted, final emit chunks contain the marker, turn
    row marked ``complete``."""
    db_path = tmp_path / "test.db"
    conn = await connect(db_path)
    await apply_schema(conn)
    store = ConversationStore(conn)
    settings = _settings(tmp_path)
    bridge = _FakeBridge(settings)
    handler = ClaudeHandler(settings, store, bridge)

    emitted: list[str] = []

    async def _emit(text: str) -> None:
        emitted.append(text)

    msg = IncomingMessage(chat_id=1, message_id=1, text="use the ping skill")
    await handler.handle(msg, _emit)

    # Every block type (the full multi-iteration sequence) is persisted.
    async with conn.execute(
        "SELECT role, block_type FROM conversations "
        "WHERE chat_id=? ORDER BY id",
        (1,),
    ) as cur:
        rows = await cur.fetchall()
    block_types = [(r[0], r[1]) for r in rows]
    assert ("user", "text") in block_types, (
        "initial user prompt not persisted; block_types=" + repr(block_types)
    )
    assert any(bt == "tool_use" for _, bt in block_types), (
        "tool_use not persisted — classic S13 symptom; block_types="
        + repr(block_types)
    )
    assert any(bt == "tool_result" for _, bt in block_types), (
        "tool_result not persisted — classic S13 symptom; block_types="
        + repr(block_types)
    )
    text_rows = [r for r in block_types if r == ("assistant", "text")]
    # Two assistant text blocks expected: narration + marker.
    assert len(text_rows) >= 2, (
        "expected >= 2 assistant text blocks (narration + marker); "
        "block_types=" + repr(block_types)
    )

    # Final emit chunks contain the marker — this is what the owner
    # looks for in Telegram.
    joined = "\n".join(emitted)
    assert "PONG-FROM-SKILL-OK" in joined, (
        "marker missing from emit chunks: " + repr(emitted)
    )

    # Turn transitioned pending → complete exactly once.
    async with conn.execute(
        "SELECT status FROM turns WHERE chat_id=? ORDER BY id DESC LIMIT 1",
        (1,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == "complete"

    await conn.close()
