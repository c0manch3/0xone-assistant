from __future__ import annotations

import asyncio
import re as _re
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

# ---------------------------------------------------------------------------
# URL detector (phase 3)
#
# B9 fix (wave-2): trailing punctuation was captured into the URL, so
# "see https://github.com/foo/bar." would yield the literal string
# "https://github.com/foo/bar." (trailing dot) — which then fails
# GitHub routing and confuses the system-note hint to the model.
# Approach: keep the broad ``\S+`` match, then strip trailing punctuation
# characters that are almost never part of a real URL.
# ---------------------------------------------------------------------------
_URL_RE = _re.compile(r"https?://\S+|git@[^\s:]+:\S+", _re.IGNORECASE)
# S7 wave-3: backtick added — markdown inline code like ``\`https://foo\```
# previously emitted ``https://foo`` ``with trailing backtick intact``,
# which fails downstream URL routing. Backtick is not a valid trailing
# character in any URL form we accept.
_TRAILING_PUNCT = ".,;:!?)\\]\"'`"


def _detect_urls(text: str) -> list[str]:
    urls: list[str] = []
    for m in _URL_RE.finditer(text):
        u = m.group(0).rstrip(_TRAILING_PUNCT)
        if u:
            urls.append(u)
    return urls


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
        # CR-1 (phase 5, NEW): serialise concurrent turns on the same
        # chat_id. Without this, an owner turn and a scheduler-injected
        # turn both targeting ``OWNER_CHAT_ID`` would interleave block
        # writes into ``conversations`` and double-bill Claude for
        # overlapping history reads.
        #
        # RQ0 confirmed absent in phase-4 source. Single-owner deployment
        # bounds the dict to one entry; if multi-chat support is added
        # later, switch to a ``weakref.WeakValueDictionary`` or TTL.
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._locks_mutex = asyncio.Lock()

    async def _lock_for(self, chat_id: int) -> asyncio.Lock:
        """Return the lock for ``chat_id``, allocating lazily on cold path.

        Double-checked lookup so the hot path never acquires
        ``_locks_mutex``.
        """
        lk = self._chat_locks.get(chat_id)
        if lk is not None:
            return lk
        async with self._locks_mutex:
            lk = self._chat_locks.get(chat_id)
            if lk is None:
                lk = asyncio.Lock()
                self._chat_locks[chat_id] = lk
        return lk

    async def handle(self, msg: IncomingMessage, emit: Emit) -> None:
        lock = await self._lock_for(msg.chat_id)
        async with lock:
            await self._handle_locked(msg, emit)

    async def _handle_locked(self, msg: IncomingMessage, emit: Emit) -> None:
        turn_id = await self._conv.start_turn(msg.chat_id)
        log.info(
            "turn_started",
            turn_id=turn_id,
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            origin=msg.origin,
        )
        # User row written with the ORIGINAL text (no system-note leak
        # into persisted history).
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

        # Phase 3: URL detector enriches the envelope sent to the SDK
        # without touching the persisted user row. If the owner pasted a
        # URL we tell the model that the installer @tool is a reasonable
        # first call; otherwise the envelope is unchanged.
        urls = _detect_urls(msg.text)
        url_hint = (
            (
                "the user's message contains URL(s) "
                f"{urls[:3]!r}. If one looks like a GitHub skill bundle, "
                "consider calling `mcp__installer__skill_preview(url=...)` to "
                "fetch a preview before asking the user to confirm install. "
                "Otherwise treat the URL as reference content."
            )
            if urls
            else None
        )

        # Phase 5: scheduler-origin turns get a directive note so the
        # model answers proactively without waiting on the owner.
        scheduler_note: str | None = None
        if msg.origin == "scheduler":
            trig_id = (msg.meta or {}).get("trigger_id")
            scheduler_note = (
                f"autonomous turn from scheduler id={trig_id}; "
                "owner is not active right now; answer proactively and "
                "concisely, do not ask clarifying questions"
            )

        notes: list[str] = []
        if scheduler_note is not None:
            notes.append(scheduler_note)
        if url_hint is not None:
            notes.append(url_hint)
        system_notes: list[str] | None = notes or None
        user_text_for_sdk = msg.text

        completed = False
        last_meta: dict[str, Any] | None = None
        try:
            async for item in self._bridge.ask(
                msg.chat_id,
                user_text_for_sdk,
                history,
                system_notes=system_notes,
            ):
                role, payload, text_out, block_type = _classify_block(item)
                if role == "result":
                    # Fix C (incident S13): accumulate last meta; complete
                    # only after the generator closes cleanly. With Fix A
                    # lifted from bridge.ask, the SDK may emit multiple
                    # ``ResultMessage`` instances per ``query()`` (e.g. when
                    # stream_input carries > 1 pending prompt or the model
                    # iterates via tool_use). Completing on the first one
                    # would race against subsequent block persistence.
                    last_meta = payload
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
            # After async-for exits cleanly, mark complete once.
            if last_meta is not None:
                await self._conv.complete_turn(turn_id, meta=last_meta)
                completed = True
                log.info(
                    "turn_complete",
                    turn_id=turn_id,
                    cost_usd=last_meta.get("cost_usd"),
                )
        except ClaudeBridgeError as exc:
            log.warning("bridge_error", turn_id=turn_id, error=str(exc))
            # Fix 3 / devil C1: on scheduler-origin turns we must
            # propagate the error so the dispatcher can
            # ``revert_to_pending`` and eventually dead-letter.
            # Swallowing the error inline makes attempts=0 forever — the
            # dead-letter threshold is unreachable for every Claude-API
            # failure on the scheduler path. User-origin turns keep the
            # legacy apology-chunk behaviour: the owner expects an
            # inline error reply, not silence or a retry. The ``finally``
            # block below interrupts the pending turn in either branch.
            if msg.origin == "scheduler":
                raise
            await emit(f"\n\n(ошибка: {exc})")
        finally:
            if not completed:
                await self._conv.interrupt_turn(turn_id)
                log.warning("turn_interrupted", turn_id=turn_id)
