"""SubagentStart + SubagentStop + cancel-flag PreToolUse hooks (phase 6).

Factory shape mirrors `bridge/hooks.py::make_pretool_hooks`. Returns a
dict keyed by hook-event name; the Daemon hands this off to both the
user-chat `ClaudeBridge` and the picker `ClaudeBridge` (B-W2-6 — two
bridges share ONE hook factory so Q6's cross-bridge guarantee holds).

Spike-backed decisions:
  * **S-6-0 Q5 / B-W2-2** — `last_assistant_message` is NOT in the SDK's
    SubagentStopHookInput TypedDict. On SDK 0.1.59 + CLI 2.1.114 it IS
    present at runtime with the full final text. Primary path reads
    it; fallback walks the JSONL at `agent_transcript_path` after a
    250 ms sleep (v1 analyser saw 0 assistant blocks at hook-fire time).
    `Daemon.start` logs a warning if the SDK version drifts.
  * **S-6-0 Q6** — hook factory shared across multiple bridges works.
  * **S-6-0 Q7 + S-2** — cancel propagates via PreToolUse flag-poll
    ONLY. Tool-free subagents run to completion. `agent_id` is on
    the PreToolUse input_data when the hook fires inside a subagent
    (5/5 observed; SDK types.py `_SubagentContextMixin`).
  * **S-1 / B-W2-4** — ContextVar propagates across the SDK boundary.
    The picker (`subagent/picker.py`) sets `CURRENT_REQUEST_ID` before
    `bridge.ask(...)`; `on_subagent_start` reads it to patch the
    pending `requested` row with the SDK-assigned `agent_id`.

GAP #12 fix: hook callbacks return `{}` immediately. Delivery tasks
are registered in `pending_updates: set` and the Daemon.stop sequence
drains them with a timeout — awaiting `asyncio.shield(task)` inside
the hook body would block the SDK iterator for a full Telegram round
trip (the original phase-1 anti-pattern).

GAP #15: the per-chat throttle dict is LRU-bounded at 64 entries. In
phase 6 all notifies flow to OWNER_CHAT_ID so the dict has one entry;
the bound is defensive against phase-8 multi-chat.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk.types import (
    AsyncHookJSONOutput,
    HookContext,
    HookInput,
    SyncHookJSONOutput,
)

from assistant.adapters.base import MessengerAdapter
from assistant.config import Settings
from assistant.logger import get_logger
from assistant.subagent.context import CURRENT_REQUEST_ID
from assistant.subagent.format import format_notification
from assistant.subagent.store import SubagentStore

log = get_logger("subagent.hooks")

_THROTTLE_MAX = 64
# Fallback sleep window before re-reading the JSONL transcript if the
# hook's `last_assistant_message` was empty/missing. S-6-0 Q5 analyser
# observed 0 assistant blocks at hook-fire time; 250 ms was enough in
# every observed run.
_TRANSCRIPT_RETRY_S = 0.25


def make_subagent_hooks(
    *,
    store: SubagentStore,
    adapter: MessengerAdapter,
    settings: Settings,
    pending_updates: set[asyncio.Task[Any]],
) -> dict[str, list[Any]]:
    """Build SubagentStart + SubagentStop + PreToolUse-cancel-gate hooks.

    Pattern: import HookMatcher lazily (pitfall #11 — keeps this module
    pure for unit tests that mock `input_data`). Close over `store`,
    `adapter`, `settings`, `pending_updates`.

    Returns a dict keyed by SDK hook-event name. The PreToolUse entry
    carries ONLY the cancel-flag-gate matcher; ClaudeBridge merges it
    with the existing phase-3 PreToolUse list (§3.10 — list-concat, not
    replacement).
    """
    from claude_agent_sdk import HookMatcher

    # Per-chat throttle, bounded LRU (GAP #15). Phase 6 always has one
    # entry since `callback_chat_id == OWNER_CHAT_ID`; the bound is
    # forward-compat defence against phase-8 multi-chat.
    last_notify_at: OrderedDict[int, float] = OrderedDict()

    async def on_subagent_start(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> AsyncHookJSONOutput | SyncHookJSONOutput:
        del tool_use_id, ctx
        raw = cast(dict[str, Any], input_data)
        agent_id = str(raw.get("agent_id") or "")
        agent_type = str(raw.get("agent_type") or "")
        parent_session = raw.get("session_id")
        if not agent_id:
            log.warning("subagent_start_missing_agent_id", raw_keys=list(raw.keys()))
            return {}

        # Wave-2 B-W2-4 + S-1: ContextVar set by the picker?
        request_id = CURRENT_REQUEST_ID.get()
        if request_id is not None:
            patched = await store.update_sdk_agent_id_for_claimed_request(
                job_id=request_id,
                sdk_agent_id=agent_id,
                parent_session_id=parent_session,
            )
            if patched:
                log.info(
                    "subagent_start_picker_claimed",
                    request_id=request_id,
                    agent_id=agent_id,
                    agent_type=agent_type,
                )
                return {}
            # If we couldn't patch, fall through to record_started as a
            # defensive INSERT. The warning was already logged inside the
            # store.
            log.warning(
                "subagent_start_picker_claim_failed_fallback_insert",
                request_id=request_id,
                agent_id=agent_id,
            )

        # Native-Task spawn (or picker-mismatch fallback): plain INSERT.
        try:
            await store.record_started(
                sdk_agent_id=agent_id,
                agent_type=agent_type,
                parent_session_id=parent_session,
                callback_chat_id=settings.owner_chat_id,
                spawned_by_kind="user",
                spawned_by_ref=None,
            )
        except Exception:
            log.warning(
                "subagent_start_record_failed",
                agent_id=agent_id,
                exc_info=True,
            )
        log.info(
            "subagent_start",
            agent_id=agent_id,
            agent_type=agent_type,
            parent_session=parent_session,
        )
        return {}

    async def on_subagent_stop(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> AsyncHookJSONOutput | SyncHookJSONOutput:
        """Record terminal status + schedule a shielded Telegram notify.

        GAP #12: we NEVER await the delivery inside the hook. The
        shielded task is registered on `pending_updates` so Daemon.stop
        can drain it; the hook returns `{}` immediately so the SDK
        iterator keeps moving.
        """
        del tool_use_id, ctx
        raw = cast(dict[str, Any], input_data)
        agent_id = str(raw.get("agent_id") or "")
        if not agent_id:
            log.warning("subagent_stop_missing_agent_id", raw_keys=list(raw.keys()))
            return {}
        transcript_path_str = raw.get("agent_transcript_path")
        session_id = raw.get("session_id")

        # Primary: runtime field on the SDK input dict (B-W2-2).
        last_msg = str(raw.get("last_assistant_message") or "")
        if not last_msg and transcript_path_str:
            # Fallback: retry once after 250 ms, then walk the JSONL.
            # Fix-pack CRITICAL #4 (CR I-6 pitfall #12): transcripts
            # for long subagents can be MB-scale; `path.read_text()`
            # inside an async hook would block the SDK's event loop
            # for the entire file read. Off-load the read to a worker
            # thread so the loop stays responsive.
            await asyncio.sleep(_TRANSCRIPT_RETRY_S)
            last_msg = await asyncio.to_thread(
                _read_last_assistant_from_transcript,
                Path(transcript_path_str),
            )

        was_cancelled = await store.is_cancel_requested(agent_id)
        status = "stopped" if was_cancelled else "completed"

        try:
            await store.record_finished(
                sdk_agent_id=agent_id,
                status=status,
                result_summary=last_msg[:500] if last_msg else None,
                transcript_path=(str(transcript_path_str) if transcript_path_str else None),
                sdk_session_id=str(session_id) if session_id else None,
                cost_usd=None,  # GAP #11 — deferred to phase 9.
            )
        except Exception:
            log.warning(
                "subagent_stop_record_failed",
                agent_id=agent_id,
                exc_info=True,
            )

        job = await store.get_by_agent_id(agent_id)
        if job is None:
            # Either the Start hook never wrote (bug) or the row was
            # recovered to 'interrupted' between Stop and now. Either
            # way, without a row we can't notify the owner meaningfully.
            log.warning("subagent_stop_unknown_agent", agent_id=agent_id)
            return {}

        body_text = last_msg or "(subagent produced no final message)"
        if not last_msg:
            log.warning("subagent_stop_empty_body", agent_id=agent_id)

        body = format_notification(
            result_text=body_text,
            job=job,
            max_body_bytes=settings.subagent.result_body_max_bytes,
        )

        job_id = job.id
        callback_chat_id = job.callback_chat_id
        throttle_ms = settings.subagent.notify_throttle_ms

        async def _deliver() -> None:
            await _throttle(
                last_notify_at,
                callback_chat_id,
                throttle_ms,
                max_entries=_THROTTLE_MAX,
            )
            try:
                # The shield keeps the send_text round-trip alive if
                # the outer notify task is cancelled mid-send; the
                # Daemon.stop drain awaits the shielded coroutine.
                await asyncio.shield(adapter.send_text(callback_chat_id, body))
            except asyncio.CancelledError:
                log.info("subagent_notify_shielded_cancel", job_id=job_id)
                raise
            except Exception:
                log.warning(
                    "subagent_notify_failed",
                    job_id=job_id,
                    exc_info=True,
                )

        task = asyncio.create_task(_deliver(), name=f"subagent_notify_{job_id}")
        pending_updates.add(task)
        task.add_done_callback(pending_updates.discard)
        return {}

    async def on_pretool_cancel_gate(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> AsyncHookJSONOutput | SyncHookJSONOutput:
        """Cancel-flag poll for subagent-emitted tool calls (S-2).

        SDK types.py `_SubagentContextMixin` documents `agent_id:
        str` as "present only when the hook fires from inside a Task-
        spawned sub-agent". Main-turn calls don't carry it — we no-op
        there. Subagent calls carry it and we check the ledger; a
        True flag denies the call, unwinding the subagent's stack on
        its next tool invocation.

        Tool-free subagents (those that never call a tool) are
        UNCANCELLABLE via this mechanism — documented in
        `skills/task/SKILL.md`.
        """
        del tool_use_id, ctx
        raw = cast(dict[str, Any], input_data)
        maybe_agent_id = raw.get("agent_id")
        if not maybe_agent_id:
            return {}
        agent_id = str(maybe_agent_id)
        if await store.is_cancel_requested(agent_id):
            log.info(
                "subagent_cancel_denied_tool",
                agent_id=agent_id,
                tool_name=raw.get("tool_name"),
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "subagent cancelled by owner",
                },
            }
        return {}

    return {
        "SubagentStart": [HookMatcher(hooks=[on_subagent_start])],
        "SubagentStop": [HookMatcher(hooks=[on_subagent_stop])],
        "PreToolUse": [HookMatcher(hooks=[on_pretool_cancel_gate])],
    }


def _read_last_assistant_from_transcript(path: Path) -> str:
    """Walk the JSONL transcript, return text of the LAST assistant entry.

    Observed shape (S-6-0 raw Q9):
        {"parentUuid": "...", "isSidechain": true, "agentId": "...",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "..."}]},
         ...}

    OS-level read errors return the empty string — the caller handles
    the "no message" fallback path and notifies a placeholder.
    """
    if not path.exists():
        return ""
    last_text = ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") != "text":
                continue
            text_value = blk.get("text")
            if isinstance(text_value, str) and text_value:
                last_text = text_value
    return last_text


async def _throttle(
    last_notify_at: OrderedDict[int, float],
    chat_id: int,
    interval_ms: int,
    *,
    max_entries: int = _THROTTLE_MAX,
) -> None:
    """Per-chat min-interval throttle with LRU eviction.

    Non-reentrant per chat (single-user bot today — safe). Moves the
    chat key to the end of the OrderedDict on each use and evicts the
    oldest entry when the dict grows past `max_entries` (GAP #15).
    """
    now = time.monotonic()
    last = last_notify_at.get(chat_id, 0.0)
    delta_ms = (now - last) * 1000.0
    if delta_ms < interval_ms:
        await asyncio.sleep((interval_ms - delta_ms) / 1000.0)
    last_notify_at[chat_id] = time.monotonic()
    last_notify_at.move_to_end(chat_id)
    while len(last_notify_at) > max_entries:
        last_notify_at.popitem(last=False)
