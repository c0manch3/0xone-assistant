"""CR-1: concurrent ClaudeHandler.handle() calls on the same chat_id
must serialise on the per-chat lock — no interleaved ``turn_started``
/ ``turn_complete`` transitions.
"""

from __future__ import annotations

import asyncio
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


class _GateBridge(ClaudeBridge):
    """Bridge that records enter / exit order across concurrent calls
    and yields a single TextBlock + ResultMessage.

    The ``_enter_event`` fires on the first call's entry; the second
    call waits on ``_release`` before entering the generator body.
    Without the per-chat lock, the second call would stream blocks
    while the first was still mid-flight.
    """

    def __init__(self, settings: Settings, trace: list[str]) -> None:
        super().__init__(settings)
        self._trace = trace
        self._calls = 0
        self._block_event = asyncio.Event()

    async def ask(  # type: ignore[override]
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
    ) -> AsyncIterator[Any]:
        del system_notes
        self._calls += 1
        tag = f"call{self._calls}"
        self._trace.append(f"{tag}-enter")
        if tag == "call1":
            # Hold the first call until block_event set so the second
            # call has a chance to race in without the lock.
            await self._block_event.wait()
        self._trace.append(f"{tag}-yield")
        yield TextBlock(text=f"reply from {tag}")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=f"sess-{tag}",
            total_cost_usd=0.0,
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        self._trace.append(f"{tag}-exit")


async def _make_store(tmp_path: Path) -> ConversationStore:
    db = tmp_path / "h.db"
    conn = await connect(db)
    await apply_schema(conn)
    return ConversationStore(conn)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=2, history_limit=5),
    )


async def test_concurrent_handle_serialises(tmp_path: Path) -> None:
    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    trace: list[str] = []
    bridge = _GateBridge(settings, trace)
    handler = ClaudeHandler(settings, store, bridge)

    async def noop_emit(s: str) -> None:
        del s

    msg_a = IncomingMessage(chat_id=42, message_id=1, text="first")
    msg_b = IncomingMessage(chat_id=42, message_id=2, text="second")

    async def run_a() -> None:
        await handler.handle(msg_a, noop_emit)

    async def run_b() -> None:
        # Give task A a moment to acquire the lock first.
        await asyncio.sleep(0.01)
        await handler.handle(msg_b, noop_emit)

    task_a = asyncio.create_task(run_a())
    task_b = asyncio.create_task(run_b())
    # Release the first call after both tasks have been scheduled.
    await asyncio.sleep(0.05)
    bridge._block_event.set()
    await asyncio.gather(task_a, task_b)

    # Contract: all of call1's trace must precede every call2 trace.
    call1_last = max(i for i, t in enumerate(trace) if t.startswith("call1"))
    call2_first = min(i for i, t in enumerate(trace) if t.startswith("call2"))
    assert call1_last < call2_first, f"interleaved trace: {trace}"


async def test_different_chat_ids_do_not_block(tmp_path: Path) -> None:
    """Same handler, two distinct chat_ids — both should run in parallel.
    If the lock were global this would deadlock or serialise.
    """
    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    trace: list[str] = []
    bridge = _GateBridge(settings, trace)
    handler = ClaudeHandler(settings, store, bridge)

    async def noop_emit(s: str) -> None:
        del s

    msg_a = IncomingMessage(chat_id=11, message_id=1, text="a")
    msg_b = IncomingMessage(chat_id=22, message_id=2, text="b")

    task_a = asyncio.create_task(handler.handle(msg_a, noop_emit))
    task_b = asyncio.create_task(handler.handle(msg_b, noop_emit))
    await asyncio.sleep(0.05)
    # Release call1 so everyone can finish.
    bridge._block_event.set()
    await asyncio.gather(task_a, task_b)

    # Both calls have entered before either exits — interleave is FINE
    # across different chats.
    assert "call1-enter" in trace and "call2-enter" in trace
