"""Handler test: model from InitMeta propagates into turns.meta_json."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ResultMessage, TextBlock

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import InitMeta
from assistant.config import ClaudeSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore


class _FakeBridge:
    """Stand-in bridge that yields a scripted item sequence."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
        image_blocks: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Any]:
        del chat_id, user_text, history, system_notes, image_blocks
        for item in self._items:
            yield item


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
    )


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=1,
        session_id="sdk-sid",
        stop_reason="end_turn",
        total_cost_usd=0.02,
        usage={"input_tokens": 5, "output_tokens": 3},
        result="ok",
        uuid="u",
    )


async def test_handler_records_model_into_turn_meta(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "h.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    bridge = _FakeBridge(
        [
            InitMeta(
                model="claude-opus-4-6",
                skills=["ping"],
                cwd=str(tmp_path),
                session_id="abc",
            ),
            TextBlock(text="hi"),
            _result(),
        ]
    )
    handler = ClaudeHandler(_settings(tmp_path), conv, turns, bridge)  # type: ignore[arg-type]

    sent: list[str] = []

    async def emit(text: str) -> None:
        sent.append(text)

    msg = IncomingMessage(chat_id=42, text="hello", message_id=1, origin="telegram")
    await handler.handle(msg, emit)

    assert sent == ["hi"]

    async with conn.execute("SELECT status, meta_json FROM turns WHERE chat_id = ?", (42,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    status, meta_json = row
    assert status == "complete"
    meta = json.loads(meta_json)
    assert meta["model"] == "claude-opus-4-6"
    assert meta["sdk_session_id"] == "abc"
    assert meta["cost_usd"] == 0.02
    assert meta["stop_reason"] == "end_turn"

    await conn.close()
