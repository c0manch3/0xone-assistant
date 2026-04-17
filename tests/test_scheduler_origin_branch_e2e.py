"""Phase 5 / commit 7b — `origin="scheduler"` handler branch.

When the handler receives an IncomingMessage with `origin="scheduler"`,
it must prepend a scheduler-note to `system_notes` so the model sees
"autonomous turn" before anything else. URL-detector notes still run
but come AFTER (plan §1.6 / spike S-7 order).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ResultMessage, TextBlock

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import InitMeta
from assistant.config import ClaudeSettings, SchedulerSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore


class _SpyBridge:
    """Fake ClaudeBridge that captures the last `system_notes` arg."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.last_system_notes: list[str] | None = None

    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
    ) -> AsyncIterator[Any]:
        del chat_id, user_text, history
        self.last_system_notes = list(system_notes) if system_notes else None
        for item in self._items:
            yield item


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        scheduler=SchedulerSettings(),
    )


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=1,
        session_id="sid",
        stop_reason="end_turn",
        total_cost_usd=0.0,
        usage={"input_tokens": 1, "output_tokens": 1},
        result="ok",
        uuid="u",
    )


async def test_scheduler_origin_prepends_scheduler_note(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "h.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    bridge = _SpyBridge(
        [
            InitMeta(model="claude-opus-4-7", skills=[], cwd=str(tmp_path), session_id="s"),
            TextBlock(text="ok"),
            _result(),
        ]
    )
    handler = ClaudeHandler(_settings(tmp_path), conv, turns, bridge)  # type: ignore[arg-type]

    sent: list[str] = []

    async def emit(text: str) -> None:
        sent.append(text)

    msg = IncomingMessage(
        chat_id=42,
        text="summarise vault",
        origin="scheduler",
        meta={"trigger_id": 777, "schedule_id": 5},
    )
    await handler.handle(msg, emit)

    assert sent == ["ok"]
    assert bridge.last_system_notes is not None
    assert len(bridge.last_system_notes) == 1
    first = bridge.last_system_notes[0]
    assert "id=777" in first
    assert "scheduler" in first.lower()
    assert "owner is not active" in first.lower()

    await conn.close()


async def test_scheduler_origin_with_url_preserves_order(tmp_path: Path) -> None:
    """Plan §1.6: when both conditions apply (scheduler AND URL), the
    notes go `[scheduler_note, url_note]`."""
    conn = await connect(tmp_path / "h.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    bridge = _SpyBridge(
        [
            InitMeta(model="claude-opus-4-7", skills=[], cwd=str(tmp_path), session_id="s"),
            TextBlock(text="ok"),
            _result(),
        ]
    )
    handler = ClaudeHandler(_settings(tmp_path), conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(text: str) -> None:
        del text

    msg = IncomingMessage(
        chat_id=42,
        text="summarise https://example.com/doc",
        origin="scheduler",
        meta={"trigger_id": 42},
    )
    await handler.handle(msg, emit)

    assert bridge.last_system_notes is not None
    assert len(bridge.last_system_notes) == 2
    # Order: scheduler first, URL second.
    assert "id=42" in bridge.last_system_notes[0]
    assert "URL" in bridge.last_system_notes[1]

    await conn.close()


async def test_telegram_origin_has_no_scheduler_note(tmp_path: Path) -> None:
    """Regression guard: existing telegram flow is untouched."""
    conn = await connect(tmp_path / "h.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    bridge = _SpyBridge(
        [
            InitMeta(model="claude-opus-4-7", skills=[], cwd=str(tmp_path), session_id="s"),
            TextBlock(text="ok"),
            _result(),
        ]
    )
    handler = ClaudeHandler(_settings(tmp_path), conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(text: str) -> None:
        del text

    msg = IncomingMessage(chat_id=42, text="hi", origin="telegram")
    await handler.handle(msg, emit)

    # No scheduler note; URL note would have been attached if the text had
    # a URL (it doesn't).
    assert bridge.last_system_notes is None

    await conn.close()


async def test_scheduler_origin_missing_meta_still_safe(tmp_path: Path) -> None:
    """Defensive: a scheduler-origin IncomingMessage with `meta=None` still
    produces a valid note (trigger_id=None in the string)."""
    conn = await connect(tmp_path / "h.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    bridge = _SpyBridge(
        [
            InitMeta(model="claude-opus-4-7", skills=[], cwd=str(tmp_path), session_id="s"),
            TextBlock(text="ok"),
            _result(),
        ]
    )
    handler = ClaudeHandler(_settings(tmp_path), conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(text: str) -> None:
        del text

    msg = IncomingMessage(chat_id=42, text="summary", origin="scheduler", meta=None)
    await handler.handle(msg, emit)

    assert bridge.last_system_notes is not None
    assert "id=None" in bridge.last_system_notes[0]

    await conn.close()
