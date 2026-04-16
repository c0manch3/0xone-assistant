from __future__ import annotations

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
from assistant.bridge.claude import ClaudeBridge, ClaudeBridgeError
from assistant.config import Settings
from assistant.logger import get_logger
from assistant.state.conversations import ConversationStore
from assistant.state.turns import TurnStore

Emit = Callable[[str], Awaitable[None]]

log = get_logger("handlers.message")


def _classify(
    item: Any,
) -> tuple[str | None, dict[str, Any], str | None, str | None]:
    """Map an SDK block/message to `(role, payload, text_to_emit, block_type)`.

    role ∈ {'user', 'assistant', 'tool', 'result', None}. 'result' is meta-only
    — it's NOT written to `conversations`; it flips the handler's completed
    flag and feeds `turns.complete(meta=...)`.
    """
    if isinstance(item, ResultMessage):
        meta: dict[str, Any] = {
            "subtype": item.subtype,
            "stop_reason": item.stop_reason,
            "usage": item.usage,
            "model_usage": item.model_usage,
            "cost_usd": item.total_cost_usd,
            "duration_ms": item.duration_ms,
            "num_turns": item.num_turns,
            "session_id": item.session_id,
        }
        return ("result", meta, None, None)
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
    """Bridges an IncomingMessage to ClaudeBridge with full turn lifecycle."""

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

    async def handle(self, msg: IncomingMessage, emit: Emit) -> None:
        turn_id = await self._turns.start(msg.chat_id)
        log.info("turn_started", turn_id=turn_id, chat_id=msg.chat_id)

        await self._conv.append(
            msg.chat_id,
            turn_id,
            "user",
            [{"type": "text", "text": msg.text}],
            block_type="text",
        )

        history = await self._conv.load_recent(msg.chat_id, self._settings.claude.history_limit)
        # Current turn is still 'pending' → excluded from load_recent's filter.

        completed = False
        try:
            async for item in self._bridge.ask(msg.chat_id, msg.text, history):
                role, payload, text_out, block_type = _classify(item)
                if role == "result":
                    await self._turns.complete(turn_id, meta=payload)
                    completed = True
                    log.info(
                        "turn_complete",
                        turn_id=turn_id,
                        cost_usd=payload.get("cost_usd"),
                    )
                    continue
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
            await emit(f"\n\n⚠ {exc}")
        finally:
            if not completed:
                await self._turns.interrupt(turn_id)
                log.warning("turn_interrupted", turn_id=turn_id)
