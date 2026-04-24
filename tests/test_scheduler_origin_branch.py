"""Scheduler-origin branch of ClaudeHandler: ``system_notes`` carries
the trigger directive only on origin=scheduler.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ResultMessage, TextBlock

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect


class _RecordingBridge(ClaudeBridge):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.last_notes: list[str] | None = None

    async def ask(  # type: ignore[override]
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
    ) -> AsyncIterator[Any]:
        del chat_id, user_text, history
        self.last_notes = list(system_notes) if system_notes else None
        yield TextBlock(text="ok")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s1",
            total_cost_usd=0.0,
            usage={"input_tokens": 1, "output_tokens": 1},
        )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
    )


async def _make_store(tmp_path: Path) -> ConversationStore:
    db = tmp_path / "h.db"
    conn = await connect(db)
    await apply_schema(conn)
    return ConversationStore(conn)


async def test_scheduler_origin_adds_directive_note(tmp_path: Path) -> None:
    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _RecordingBridge(settings)
    handler = ClaudeHandler(settings, store, bridge)

    emitted: list[str] = []

    async def emit(s: str) -> None:
        emitted.append(s)

    msg = IncomingMessage(
        chat_id=1,
        message_id=0,
        text="do the thing",
        origin="scheduler",
        meta={"trigger_id": 42, "schedule_id": 7},
    )
    await handler.handle(msg, emit)
    assert bridge.last_notes is not None
    joined = " ".join(bridge.last_notes)
    assert "scheduler id=42" in joined
    assert "proactively" in joined


async def test_telegram_origin_emits_no_scheduler_note(tmp_path: Path) -> None:
    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _RecordingBridge(settings)
    handler = ClaudeHandler(settings, store, bridge)

    async def emit(s: str) -> None:
        del s

    msg = IncomingMessage(chat_id=1, message_id=1, text="hi")
    await handler.handle(msg, emit)
    # No URL in text + non-scheduler origin → no notes at all.
    assert bridge.last_notes is None
