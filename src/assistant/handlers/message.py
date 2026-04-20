from __future__ import annotations

from typing import Any

from claude_agent_sdk import (
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from assistant.adapters.base import Emit, IncomingMessage
from assistant.bridge.claude import ClaudeBridge, ClaudeBridgeError
from assistant.config import Settings
from assistant.logger import get_logger
from assistant.state.conversations import ConversationStore

log = get_logger("handlers.message")


def _classify_block(
    item: Any,
) -> tuple[str | None, dict[str, Any], str | None, str | None]:
    """Classify an SDK message/block into ``(role, payload, text_out, block_type)``.

    Contract (B5 — Anthropic tools API):
      TextBlock      → role='assistant', block_type='text', text_out=item.text
      ThinkingBlock  → role='assistant', block_type='thinking'
      ToolUseBlock   → role='assistant', block_type='tool_use'
      ToolResultBlock→ role='user',      block_type='tool_result'
        ^^^ SDK streaming-input mode requires tool_result on USER envelope,
            not 'tool'. Storing with role='tool' silently drops on replay.
      ResultMessage  → role='result' (caller uses the payload to mark the
        turn complete; no DB row is written for it).

    Unknown block types map to ``(None, ...)`` and the caller skips them.
    """
    if isinstance(item, ResultMessage):
        usage = item.usage or {}
        meta = {
            "stop_reason": getattr(item, "stop_reason", None),
            "usage": usage,
            "cost_usd": item.total_cost_usd,
            "duration_ms": getattr(item, "duration_ms", None),
            "num_turns": getattr(item, "num_turns", None),
            "sdk_session_id": getattr(item, "session_id", None),
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
            # B5: role='user' (NOT 'tool') — SDK requires ToolResultBlock on
            # a user envelope per the Anthropic tools API contract.
            "user",
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
    """Orchestrates a single turn:
    start_turn → store user row → bridge.ask → persist every block →
    on ResultMessage mark turn complete; otherwise interrupt on ``finally``.
    """

    def __init__(
        self,
        settings: Settings,
        conv: ConversationStore,
        bridge: ClaudeBridge,
    ) -> None:
        self._settings = settings
        self._conv = conv
        self._bridge = bridge

    async def handle(self, msg: IncomingMessage, emit: Emit) -> None:
        turn_id = await self._conv.start_turn(msg.chat_id)
        log.info(
            "turn_started",
            turn_id=turn_id,
            chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
        await self._conv.append(
            msg.chat_id,
            turn_id,
            "user",
            [{"type": "text", "text": msg.text}],
            block_type="text",
        )
        history = await self._conv.load_recent(msg.chat_id, self._settings.claude.history_limit)
        # The current turn is still 'pending' so load_recent's 'complete'
        # filter excludes it — we won't replay our own user row to the model.

        completed = False
        try:
            async for item in self._bridge.ask(msg.chat_id, msg.text, history):
                role, payload, text_out, block_type = _classify_block(item)
                if role == "result":
                    await self._conv.complete_turn(turn_id, meta=payload)
                    completed = True
                    log.info(
                        "turn_complete",
                        turn_id=turn_id,
                        cost_usd=payload.get("cost_usd"),
                    )
                    continue
                if role is None:
                    continue
                assert block_type is not None
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
            log.warning("bridge_error", turn_id=turn_id, error=str(exc))
            await emit(f"\n\n(ошибка: {exc})")
        finally:
            if not completed:
                await self._conv.interrupt_turn(turn_id)
                log.warning("turn_interrupted", turn_id=turn_id)
