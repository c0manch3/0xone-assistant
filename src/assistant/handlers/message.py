from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import (
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import ClaudeBridge, ClaudeBridgeError, InitMeta
from assistant.config import Settings
from assistant.logger import get_logger
from assistant.state.conversations import ConversationStore
from assistant.state.turns import TurnStore

Emit = Callable[[str], Awaitable[None]]

log = get_logger("handlers.message")


def _result_meta(item: ResultMessage) -> dict[str, Any]:
    return {
        "subtype": item.subtype,
        "stop_reason": item.stop_reason,
        "usage": item.usage,
        "model_usage": item.model_usage,
        "cost_usd": item.total_cost_usd,
        "duration_ms": item.duration_ms,
        "num_turns": item.num_turns,
        "session_id": item.session_id,
    }


def _classify(
    item: Any,
) -> tuple[str | None, dict[str, Any], str | None, str | None]:
    """Map an SDK block to `(role, payload, text_to_emit, block_type)`.

    role ∈ {'user', 'assistant', 'tool', None}. ResultMessage and InitMeta
    are NOT handled here -- they're metadata that flips handler state and
    is dispatched explicitly in `handle()`.
    """
    if isinstance(item, TextBlock):
        return (
            "assistant",
            {"type": "text", "text": item.text},
            item.text,
            "text",
        )
    if isinstance(item, ThinkingBlock):
        return (
            "assistant",
            {
                "type": "thinking",
                "thinking": item.thinking,
                "signature": item.signature,
            },
            None,
            "thinking",
        )
    if isinstance(item, ToolUseBlock):
        return (
            "assistant",
            {
                "type": "tool_use",
                "id": item.id,
                "name": item.name,
                "input": item.input,
            },
            None,
            "tool_use",
        )
    if isinstance(item, ToolResultBlock):
        return (
            "tool",
            {
                "type": "tool_result",
                "tool_use_id": item.tool_use_id,
                "content": item.content,
                "is_error": item.is_error,
            },
            None,
            "tool_result",
        )
    return (None, {}, None, None)


class ClaudeHandler:
    """Bridges an IncomingMessage to ClaudeBridge with full turn lifecycle.

    A per-chat asyncio lock serialises concurrent turns for the same chat
    (e.g. an owner message arriving while a phase-5 scheduler trigger is
    already mid-flight). Different chats run independently up to
    `claude.max_concurrent`.
    """

    def __init__(
        self,
        settings: Settings,
        conv: ConversationStore,
        turns: TurnStore,
        bridge: ClaudeBridge,
    ) -> None:
        self._settings = settings
        self._conv = conv
        self._turns = turns
        self._bridge = bridge
        self._chat_locks: dict[int, asyncio.Lock] = {}

    def _chat_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def handle(self, msg: IncomingMessage, emit: Emit) -> None:
        async with self._chat_lock(msg.chat_id):
            await self._run_turn(msg, emit)

    async def _run_turn(self, msg: IncomingMessage, emit: Emit) -> None:
        turn_id = await self._turns.start(msg.chat_id)
        log.info("turn_started", turn_id=turn_id, chat_id=msg.chat_id, origin=msg.origin)

        await self._conv.append(
            msg.chat_id,
            turn_id,
            "user",
            [{"type": "text", "text": msg.text}],
            block_type="text",
        )

        history = await self._conv.load_recent(msg.chat_id, self._settings.claude.history_limit)
        # Current turn is still 'pending' -> excluded from load_recent's filter.

        completed = False
        meta: dict[str, Any] = {}
        try:
            async for item in self._bridge.ask(msg.chat_id, msg.text, history):
                if isinstance(item, InitMeta):
                    if item.model is not None:
                        meta["model"] = item.model
                    if item.session_id is not None:
                        meta["sdk_session_id"] = item.session_id
                    continue
                if isinstance(item, ResultMessage):
                    meta.update(_result_meta(item))
                    await self._turns.complete(turn_id, meta=meta)
                    completed = True
                    log.info(
                        "turn_complete",
                        turn_id=turn_id,
                        cost_usd=meta.get("cost_usd"),
                        model=meta.get("model"),
                    )
                    continue
                role, payload, text_out, block_type = _classify(item)
                if role is None:
                    continue
                await self._conv.append(
                    msg.chat_id,
                    turn_id,
                    role,
                    [payload],
                    block_type=block_type,
                )
                if text_out:
                    await emit(text_out)
        except ClaudeBridgeError as exc:
            log.warning("bridge_error", turn_id=turn_id, error=repr(exc))
            await emit("\n\n⚠ Внутренняя ошибка, детали в логах.")
        finally:
            if not completed:
                # Shield the interrupt so a CancelledError mid-finally
                # doesn't leave the turn stuck in 'pending'.
                try:
                    await asyncio.shield(self._turns.interrupt(turn_id))
                except asyncio.CancelledError:
                    log.warning(
                        "turn_interrupt_cancelled",
                        turn_id=turn_id,
                    )
                    raise
                log.warning("turn_interrupted", turn_id=turn_id)
