"""Fix 3 / devil C1: ``ClaudeHandler`` re-raises ``ClaudeBridgeError``
on scheduler-origin turns so the dispatcher can revert + dead-letter.

User-origin turns keep the legacy apology-chunk-then-return behaviour.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import ClaudeBridge, ClaudeBridgeError
from assistant.config import ClaudeSettings, SchedulerSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect


class _BridgeRaisingBridgeError(ClaudeBridge):
    async def ask(  # type: ignore[override]
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
    ) -> AsyncIterator[Any]:
        del chat_id, user_text, history, system_notes
        raise ClaudeBridgeError("simulated CLI timeout at 300s")
        yield  # pragma: no cover — makes this an async generator


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
        scheduler=SchedulerSettings(),
    )


async def test_handler_reraises_bridge_error_on_scheduler_origin(
    tmp_path: Path,
) -> None:
    db = tmp_path / "x.db"
    conn = await connect(db)
    await apply_schema(conn)
    conv = ConversationStore(conn)
    settings = _settings(tmp_path)
    bridge = _BridgeRaisingBridgeError(settings)
    handler = ClaudeHandler(settings, conv, bridge)

    emitted: list[str] = []

    async def emit(chunk: str) -> None:
        emitted.append(chunk)

    msg = IncomingMessage(
        chat_id=settings.owner_chat_id,
        message_id=0,
        text="wrapped scheduler body",
        origin="scheduler",
        meta={"trigger_id": 42, "schedule_id": 7},
    )
    with pytest.raises(ClaudeBridgeError, match="simulated CLI timeout"):
        await handler.handle(msg, emit)
    # Nothing was emitted on the scheduler path: the dispatcher's outer
    # try/except is where the failure surfaces.
    assert emitted == []


async def test_handler_apologises_inline_on_user_origin(
    tmp_path: Path,
) -> None:
    """Control: user-origin turns keep the apology-chunk behaviour so the
    owner sees an inline error reply instead of a silent drop.
    """
    db = tmp_path / "x.db"
    conn = await connect(db)
    await apply_schema(conn)
    conv = ConversationStore(conn)
    settings = _settings(tmp_path)
    bridge = _BridgeRaisingBridgeError(settings)
    handler = ClaudeHandler(settings, conv, bridge)

    emitted: list[str] = []

    async def emit(chunk: str) -> None:
        emitted.append(chunk)

    msg = IncomingMessage(
        chat_id=settings.owner_chat_id,
        message_id=1,
        text="hello",
        origin="telegram",
    )
    # Must NOT raise — user-origin branch swallows the error.
    await handler.handle(msg, emit)
    full = "".join(emitted)
    assert "ошибка" in full
    assert "simulated CLI timeout" in full
