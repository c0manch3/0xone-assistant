"""SubagentStart + SubagentStop + cancel-flag PreToolUse hooks.

Wave-2 changes from pre-wipe spec:
  * Hook returns ``{}`` immediately; delivery runs as a shielded bg
    task registered on ``pending_updates`` (GAP #12).
  * ``on_subagent_start`` reads ``CURRENT_REQUEST_ID`` ContextVar
    (S-1 PASS) — when set, picker-claimed request is patched via
    ``update_sdk_agent_id_for_claimed_request``.
  * Throttle dict bounded at 64 entries with LRU eviction (GAP #15).
  * Cancel-gate PreToolUse reads ``raw.get("agent_id")`` directly per
    SDK ``_SubagentContextMixin`` contract (S-2 wave-2 verified).

Spike anchors: S-6-0 Q5/Q6/Q7, S-1 ContextVar, S-2 sandbox traversal,
research RQ-RESPIKE Q5/Q4/Q7.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, cast

import structlog
from claude_agent_sdk import HookContext, HookInput, HookJSONOutput

from assistant.adapters.base import MessengerAdapter
from assistant.config import Settings
from assistant.subagent.notify import format_notification
from assistant.subagent.store import SubagentJob, SubagentStore

_log = structlog.get_logger(__name__)

# Picker sets this ContextVar before invoking ``bridge.ask`` so that the
# subsequent on_subagent_start hook can correlate the SDK-assigned
# agent_id back to the pending request row.  S-1 spike PASS
# (1001/1002 propagation across two back-to-back runs).
CURRENT_REQUEST_ID: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "phase6_current_request_id", default=None
)

# Fix-pack F2 (QA HIGH-1): the handler sets this to the originating
# IncomingMessage.origin BEFORE invoking bridge.ask. Native ``Task``
# Start hook reads it to differentiate ``"user"`` vs ``"scheduler"``
# spawn provenance; the ``subagent_spawn`` @tool reads it to
# differentiate ``"tool"`` vs ``"scheduler"`` provenance. Without
# this, every scheduler-fired turn that delegates to a subagent was
# misclassified — owner forensics + future analytics break.
#
# ``Origin`` shape mirrors ``adapters.base.Origin`` but is duplicated
# as a plain str default to keep this module's import graph free of
# the adapters package.
CURRENT_TURN_ORIGIN: contextvars.ContextVar[str] = contextvars.ContextVar(
    "phase6_current_turn_origin", default="telegram"
)

# Bound on the per-chat throttle dict. Phase 6 always uses
# OWNER_CHAT_ID (single-user bot), so the dict effectively has 1 entry;
# the bound is defensive for any future multi-chat extension. GAP #15.
_THROTTLE_MAX = 64


def make_subagent_hooks(
    *,
    store: SubagentStore,
    adapter: MessengerAdapter,
    settings: Settings,
    pending_updates: set[asyncio.Task[Any]],
) -> dict[str, list[Any]]:
    """Return SDK-hook dict ready to merge into ``ClaudeAgentOptions.hooks``.

    Lazy import of ``HookMatcher`` (pitfall #11): keeps the validator
    module's import graph free of SDK-specific symbols and lets tests
    inject a fake matcher in unit-test contexts.

    Closes over ``store`` / ``adapter`` / ``settings`` /
    ``pending_updates``. The returned dict has three keys:
      * ``"SubagentStart"`` — ledger row INSERT or claim-patch.
      * ``"SubagentStop"`` — ledger UPDATE + shielded notify task.
      * ``"PreToolUse"`` — cancel-flag-poll deny gate; merges INTO the
        bridge's existing PreToolUse list (see bridge.claude wiring).
    """
    from claude_agent_sdk import HookMatcher

    last_notify_at: OrderedDict[int, float] = OrderedDict()
    # Fix-pack F4 (code H2): without a lock, two concurrent SubagentStop
    # hooks for the same chat_id race in ``_throttle`` — both read the
    # same ``last`` value, both decide they don't need to sleep, and the
    # send_text calls land on Telegram back-to-back instead of being
    # serialised by the configured min interval. Single-owner deployment
    # makes a global lock fine; per-chat sharding is a phase-8 concern.
    throttle_lock = asyncio.Lock()

    async def on_subagent_start(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> HookJSONOutput:
        del tool_use_id, ctx
        raw = cast(dict[str, Any], input_data)
        agent_id = str(raw.get("agent_id") or "")
        agent_type = str(raw.get("agent_type") or "unknown")
        parent_session = raw.get("session_id")
        if not agent_id:
            _log.warning("subagent_start_no_agent_id", payload_keys=list(raw))
            return cast(HookJSONOutput, {})

        request_id = CURRENT_REQUEST_ID.get()
        if request_id is not None:
            patched = await store.update_sdk_agent_id_for_claimed_request(
                job_id=request_id,
                sdk_agent_id=agent_id,
                parent_session_id=parent_session,
            )
            if patched:
                _log.info(
                    "subagent_start_picker_claimed",
                    request_id=request_id,
                    agent_id=agent_id,
                    agent_type=agent_type,
                )
                return cast(HookJSONOutput, {})
            _log.warning(
                "subagent_start_claim_mismatch",
                request_id=request_id,
                agent_id=agent_id,
            )
            # Fall through to defensive INSERT.

        # Fix-pack F2 (QA HIGH-1): native Task Start path inherits its
        # provenance from the originating turn. Scheduler-fired turn that
        # delegates → "scheduler"; everything else (owner Telegram turn)
        # → "user".
        origin = CURRENT_TURN_ORIGIN.get()
        spawned_by_kind = "scheduler" if origin == "scheduler" else "user"
        try:
            await store.record_started(
                sdk_agent_id=agent_id,
                agent_type=agent_type,
                parent_session_id=parent_session,
                callback_chat_id=settings.owner_chat_id,
                spawned_by_kind=spawned_by_kind,
                spawned_by_ref=None,
            )
        except Exception:
            _log.warning(
                "subagent_record_started_failed",
                agent_id=agent_id,
                exc_info=True,
            )
        _log.info(
            "subagent_start",
            agent_id=agent_id,
            agent_type=agent_type,
            parent_session=parent_session,
        )
        return cast(HookJSONOutput, {})

    async def on_subagent_stop(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> HookJSONOutput:
        del tool_use_id, ctx
        raw = cast(dict[str, Any], input_data)
        agent_id = str(raw.get("agent_id") or "")
        transcript_path = raw.get("agent_transcript_path")
        session_id = raw.get("session_id")
        if not agent_id:
            _log.warning("subagent_stop_no_agent_id", payload_keys=list(raw))
            return cast(HookJSONOutput, {})

        # Primary path (S-6-0 Q5 raw evidence): runtime field.
        # Wave-2 B-W2-2 / research RQ-RESPIKE Q5: not in TypedDict, so
        # JSONL fallback below is the safety net.
        last_msg = str(raw.get("last_assistant_message") or "")
        if not last_msg and transcript_path:
            await asyncio.sleep(0.25)
            last_msg = await asyncio.to_thread(
                _read_last_assistant_from_transcript, Path(transcript_path)
            )

        # Fix-pack F3 (QA HIGH-2): the SDK can emit
        # ``TaskNotificationMessage(status='failed')`` when a subagent
        # blew through ``maxTurns`` or hit a hard error. Read both the
        # bare ``status`` key (TypedDict shape) AND ``task_status`` (the
        # alternate shape some 0.1.6x bundled CLIs emit). Cancellation
        # takes precedence — owner clicked stop, that wins over an SDK
        # error in the same turn.
        sdk_status = raw.get("status") or raw.get("task_status")
        was_cancelled = await store.is_cancel_requested(agent_id)
        if was_cancelled:
            terminal_status = "stopped"
            last_error = None
        elif sdk_status == "failed":
            terminal_status = "failed"
            err_raw = (
                raw.get("error")
                or raw.get("error_message")
                or "subagent failed"
            )
            last_error = str(err_raw)
        else:
            terminal_status = "completed"
            last_error = None

        try:
            await store.record_finished(
                sdk_agent_id=agent_id,
                status=terminal_status,
                result_summary=last_msg[:500] if last_msg else None,
                transcript_path=str(transcript_path) if transcript_path else None,
                sdk_session_id=str(session_id) if session_id else None,
                cost_usd=None,
                last_error=last_error,
            )
        except Exception:
            _log.warning(
                "subagent_record_finished_failed",
                agent_id=agent_id,
                exc_info=True,
            )

        job = await store.get_by_agent_id(agent_id)
        if job is None:
            _log.warning("subagent_stop_unknown_agent", agent_id=agent_id)
            return cast(HookJSONOutput, {})
        body_text = last_msg or "(subagent produced no final message)"
        body = format_notification(
            result_text=body_text,
            job=job,
            max_body_bytes=settings.subagent.result_body_max_bytes,
        )

        async def _deliver(captured_job: SubagentJob, payload: str) -> None:
            try:
                await _throttle(
                    last_notify_at,
                    throttle_lock,
                    captured_job.callback_chat_id,
                    settings.subagent.notify_throttle_ms,
                )
                await asyncio.shield(
                    adapter.send_text(captured_job.callback_chat_id, payload)
                )
            except asyncio.CancelledError:
                _log.info(
                    "subagent_notify_cancelled",
                    job_id=captured_job.id,
                )
                raise
            except Exception:
                _log.warning(
                    "subagent_notify_failed",
                    job_id=captured_job.id,
                    exc_info=True,
                )

        task = asyncio.create_task(
            _deliver(job, body), name=f"subagent_notify_{job.id}"
        )
        pending_updates.add(task)
        task.add_done_callback(pending_updates.discard)
        _log.info(
            "subagent_stop",
            job_id=job.id,
            agent_id=agent_id,
            status=terminal_status,
            body_chars=len(body_text),
        )
        return cast(HookJSONOutput, {})

    async def on_pretool_cancel_gate(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> HookJSONOutput:
        """PreToolUse cancel-flag poll for subagent-emitted tool calls.

        SDK ``_SubagentContextMixin`` populates ``agent_id`` on
        ``input_data`` whenever the hook fires from inside a Task-spawned
        subagent (S-2 wave-2 verified 5/5). Main-turn calls have no
        ``agent_id`` → hook no-ops.
        """
        del tool_use_id, ctx
        raw = cast(dict[str, Any], input_data)
        agent_id = raw.get("agent_id")
        if not agent_id:
            return cast(HookJSONOutput, {})
        if await store.is_cancel_requested(str(agent_id)):
            _log.info(
                "subagent_cancel_denied_tool",
                agent_id=str(agent_id),
            )
            return cast(
                HookJSONOutput,
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "subagent cancelled by owner"
                        ),
                    },
                },
            )
        return cast(HookJSONOutput, {})

    return {
        "SubagentStart": [HookMatcher(hooks=[on_subagent_start])],
        "SubagentStop": [HookMatcher(hooks=[on_subagent_stop])],
        "PreToolUse": [HookMatcher(hooks=[on_pretool_cancel_gate])],
    }


# ---------------------------------------------------------------------------
# JSONL streaming-read fallback for last assistant message
# ---------------------------------------------------------------------------
def _read_last_assistant_from_transcript(path: Path) -> str:
    """Stream-read the JSONL transcript and return the LAST assistant
    message's text content.

    Memory bound: O(longest_line) + O(best_assistant_text); on a 50 MB
    transcript with one 200 KB assistant block, peak resident is ~250 KB.

    Robustness:
      * Tail completeness guard — drop the final line if the file does
        NOT end with ``\\n`` (SDK CLI may have flushed mid-line right
        before the SubagentStop hook fired; B-W2-2 race).
      * Returns ``""`` on missing file / zero assistant blocks / OS errors.
      * Tolerant of malformed lines (skipped silently).

    Observed schema (S-6-0 raw Q9):
        {"type": "assistant",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "..."}]}}
    """
    if not path.is_file():
        return ""
    try:
        with path.open("rb") as fh_bin:
            fh_bin.seek(0, 2)
            file_size = fh_bin.tell()
            if file_size == 0:
                return ""
            fh_bin.seek(file_size - 1)
            last_byte = fh_bin.read(1)
    except OSError:
        return ""
    drop_last_line = last_byte != b"\n"

    last_text = ""
    pending_line: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                if pending_line is not None:
                    candidate = _extract_assistant_text(pending_line)
                    if candidate is not None:
                        last_text = candidate
                pending_line = raw_line
            # Process the final pending line ONLY if file ended with \n.
            if pending_line is not None and not drop_last_line:
                candidate = _extract_assistant_text(pending_line)
                if candidate is not None:
                    last_text = candidate
    except OSError:
        return last_text
    return last_text


def _extract_assistant_text(raw_line: str) -> str | None:
    """Decode one JSONL line; return assistant text or ``None``."""
    s = raw_line.strip()
    if not s:
        return None
    try:
        envelope = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None
    # Tolerate either ``"type": "assistant"`` or ``"role": "assistant"``
    # at the envelope level — different SDK CLI minor versions differ.
    msg = envelope.get("message")
    if not isinstance(msg, dict):
        # Some shapes may inline content without nested "message".
        if envelope.get("type") == "assistant" and isinstance(
            envelope.get("content"), list
        ):
            return _join_text_blocks(envelope.get("content") or [])
        return None
    if msg.get("role") != "assistant":
        return None
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    return _join_text_blocks(content)


def _join_text_blocks(content: list[Any]) -> str | None:
    parts: list[str] = []
    for blk in content:
        if isinstance(blk, dict) and blk.get("type") == "text":
            text = blk.get("text")
            if isinstance(text, str):
                parts.append(text)
    if not parts:
        return None
    return "".join(parts)


async def _throttle(
    last_notify_at: OrderedDict[int, float],
    lock: asyncio.Lock,
    chat_id: int,
    interval_ms: int,
) -> None:
    """Per-chat min-interval throttle. Non-reentrant per chat (single
    owner — ok). LRU-bounded to ``_THROTTLE_MAX`` entries (GAP #15).

    Fix-pack F4 (code H2): the ``lock`` argument serialises concurrent
    callers so two simultaneous SubagentStop hooks for the same
    ``chat_id`` cannot both observe the same stale ``last`` value and
    skip the sleep. Holding the lock across ``asyncio.sleep`` is
    intentional — that IS the serialisation: caller B blocks on the
    lock while caller A sleeps out the interval, then takes its turn.
    """
    async with lock:
        now = time.monotonic()
        last = last_notify_at.get(chat_id, 0.0)
        delta_ms = (now - last) * 1000.0
        if delta_ms < interval_ms:
            await asyncio.sleep((interval_ms - delta_ms) / 1000.0)
        last_notify_at[chat_id] = time.monotonic()
        last_notify_at.move_to_end(chat_id)
        while len(last_notify_at) > _THROTTLE_MAX:
            last_notify_at.popitem(last=False)
