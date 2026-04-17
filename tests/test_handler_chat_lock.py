"""Per-chat lock test: two concurrent handle() calls for the same chat
serialise; calls against different chats run in parallel.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
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


async def _wait_until(predicate: Callable[[], bool], *, deadline_s: float) -> None:
    """Poll `predicate` until it returns True. Deadline-bounded.

    Parameter is `deadline_s` (not `timeout`) to sidestep ruff's ASYNC109
    rule -- we use `asyncio.timeout()` internally to enforce the limit.
    """

    async def _poll() -> None:
        event = asyncio.Event()
        while not predicate():
            try:
                await asyncio.wait_for(event.wait(), timeout=0.01)
            except TimeoutError:
                continue

    async with asyncio.timeout(deadline_s):
        await _poll()


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        stop_reason="end_turn",
        total_cost_usd=0.01,
        usage=None,
        result="ok",
        uuid="u",
    )


class _SlowBridge:
    """Bridge whose `ask` blocks until released, recording active concurrency."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.in_flight = 0
        self.max_in_flight = 0

    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
    ) -> AsyncIterator[Any]:
        del chat_id, user_text, history, system_notes
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            yield InitMeta(model="m", skills=[], cwd=None, session_id=None)
            await self.release.wait()
            yield TextBlock(text="ok")
            yield _result()
        finally:
            self.in_flight -= 1


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
    )


async def test_same_chat_serialised(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "lock.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)
    bridge = _SlowBridge()
    handler = ClaudeHandler(_settings(tmp_path), conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(_: str) -> None:
        return None

    msg = IncomingMessage(chat_id=99, text="hi")
    t1 = asyncio.create_task(handler.handle(msg, emit))
    t2 = asyncio.create_task(handler.handle(msg, emit))

    # Wait until at least one task has entered the bridge, then assert no
    # second one ever makes it in while the first is still blocked.
    await _wait_until(lambda: bridge.in_flight >= 1, deadline_s=2.0)
    await asyncio.sleep(0.05)  # give the second task a fair chance to sneak in
    assert bridge.in_flight == 1, "lock did not serialise same-chat calls"

    bridge.release.set()
    await asyncio.gather(t1, t2)
    assert bridge.max_in_flight == 1

    await conn.close()


async def test_different_chats_run_concurrently(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "par.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)
    bridge = _SlowBridge()
    handler = ClaudeHandler(_settings(tmp_path), conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(_: str) -> None:
        return None

    msg_a = IncomingMessage(chat_id=1, text="hi")
    msg_b = IncomingMessage(chat_id=2, text="hi")
    t_a = asyncio.create_task(handler.handle(msg_a, emit))
    t_b = asyncio.create_task(handler.handle(msg_b, emit))

    # Both tasks run sequentially through the store lock (SQLite writer),
    # then park at `release.wait()`. Wait until both are in flight.
    await _wait_until(lambda: bridge.in_flight == 2, deadline_s=2.0)
    assert bridge.in_flight == 2, "different chats should run concurrently"

    bridge.release.set()
    await asyncio.gather(t_a, t_b)
    assert bridge.max_in_flight == 2

    await conn.close()
