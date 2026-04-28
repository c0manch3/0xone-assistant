"""Consumer loop: poll ``subagent_jobs`` for ``status='requested'`` rows
and dispatch each through a dedicated picker bridge.

Wave-2 design:
  * B-W2-4: ``CURRENT_REQUEST_ID`` ContextVar set BEFORE invoking
    ``bridge.ask`` so the on_subagent_start hook can correlate the
    SDK-assigned agent_id with the pending request row.
  * B-W2-6: takes a dedicated ``ClaudeBridge`` instance — caller
    (``Daemon.start``) MUST NOT share the user-chat bridge. Independent
    semaphore prevents picker dispatches from starving owner turns.
  * Devil H-6 + research RQ3: dispatch is awaited INLINE per tick (no
    per-tick ``create_task``) — picker is itself the bg coroutine
    anchored on ``Daemon._bg_tasks``, so cancellation propagates
    cleanly through the in-flight dispatch's ``record_started`` /
    ``record_finished`` SQL.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from assistant.adapters.base import MessengerAdapter
from assistant.bridge.claude import ClaudeBridge, ClaudeBridgeError
from assistant.config import Settings
from assistant.subagent.hooks import CURRENT_REQUEST_ID
from assistant.subagent.store import SubagentJob, SubagentStore

_log = structlog.get_logger(__name__)


_PICKER_PROMPT_TEMPLATE = """\
Delegate the following task to the `{kind}` subagent using the Task tool.
After you have invoked the Task tool once (and ONLY once), reply with
exactly the word `dispatched` and stop. Do NOT wait for the subagent's
result, do NOT summarise, do NOT add any other text.

Task for the subagent:
<<<TASK>>>
{task_text}
<<<END>>>
"""


class SubagentRequestPicker:
    """Tick-based picker for pre-queued subagent jobs."""

    def __init__(
        self,
        store: SubagentStore,
        bridge: ClaudeBridge,
        *,
        settings: Settings,
        adapter: MessengerAdapter | None = None,
    ) -> None:
        self._store = store
        self._bridge = bridge
        self._settings = settings
        # Fix-pack F1: optional adapter so terminal-error notify can
        # reach the owner. Daemon.start passes the live TelegramAdapter;
        # tests can pass ``None`` and assert on the store transition only.
        self._adapter = adapter
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        tick = self._settings.subagent.picker_tick_s
        _log.info("subagent_picker_started", tick_s=tick)
        try:
            while not self._stop.is_set():
                try:
                    pending = await self._store.list_pending_requests(limit=1)
                except Exception:
                    _log.warning("picker_list_failed", exc_info=True)
                    pending = []
                job = pending[0] if pending else None
                if job is not None:
                    if job.cancel_requested:
                        # Cancel arrived before pickup — drop without
                        # dispatching. recover_orphans-Branch 3 will
                        # transition stale ones; here we simply skip.
                        _log.info(
                            "picker_skip_cancelled", job_id=job.id
                        )
                    else:
                        await self._dispatch_one(job)
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=tick
                    )
                except TimeoutError:
                    continue
        finally:
            _log.info("subagent_picker_stopped")

    async def _dispatch_one(self, job: SubagentJob) -> None:
        """Dispatch one pending request via the dedicated picker bridge.

        Sets ``CURRENT_REQUEST_ID`` so on_subagent_start patches the
        row from ``'requested'`` → ``'started'`` with the real
        ``sdk_agent_id``. Uses ``timeout_override=claude_subagent_timeout``
        (15 min default) so a long subagent dispatch isn't killed by
        the user-turn 300s default.

        Fix-pack F1 (code H1 / devil C-W2-4 / QA HIGH-3):
        on a ``ClaudeBridgeError`` (claude CLI down, OAuth expired,
        SDK 5xx) we increment ``attempts`` via ``mark_dispatch_failed``;
        once the attempts threshold is hit the row flips to terminal
        ``'error'`` and the owner gets a one-shot notify so they know
        the job did not slip silently. After bridge.ask completes
        without exception we additionally check whether the row is
        STILL ``'requested'`` (Start hook never fired → model returned
        the ``"dispatched"`` stub WITHOUT actually invoking the Task
        tool); same fail-fast path applies.
        """
        log = _log.bind(job_id=job.id, kind=job.agent_type)
        token = CURRENT_REQUEST_ID.set(job.id)
        prompt = _PICKER_PROMPT_TEMPLATE.format(
            kind=job.agent_type, task_text=job.task_text or ""
        )
        bridge_error: ClaudeBridgeError | None = None
        try:
            try:
                async for _msg in self._bridge.ask(
                    chat_id=job.callback_chat_id,
                    user_text=prompt,
                    history=[],
                    timeout_override=(
                        self._settings.subagent.claude_subagent_timeout
                    ),
                ):
                    # We rely on SubagentStop hook to deliver the result
                    # to the owner via Telegram. The picker does not
                    # forward main-turn output (it's just the model's
                    # "dispatched" stub).
                    pass
            except ClaudeBridgeError as exc:
                bridge_error = exc
                log.warning(
                    "picker_bridge_error",
                    error=repr(exc)[:200],
                )
            except asyncio.CancelledError:
                log.info("picker_dispatch_cancelled")
                raise
            except Exception:
                log.warning("picker_unexpected_error", exc_info=True)
        finally:
            CURRENT_REQUEST_ID.reset(token)

        # Fix-pack F1 — finalisation path.
        await self._finalize_dispatch(job, bridge_error)
        log.info("picker_dispatch_done")

    async def _finalize_dispatch(
        self,
        job: SubagentJob,
        bridge_error: ClaudeBridgeError | None,
    ) -> None:
        """Apply ``mark_dispatch_failed`` policy after a dispatch returns.

        Two distinct failure shapes converge here:

        * ``bridge_error`` set → the SDK invocation itself failed; the
          row never reached ``'started'``. Increment attempts.
        * ``bridge_error`` is None but the row is STILL ``'requested'``
          → model returned a clean response without invoking the Task
          tool (refusal, confusion, no-op). Same treatment: attempts++.

        When ``mark_dispatch_failed`` returns ``"error"`` the row is
        terminal and we send a one-shot Telegram notify so the owner
        does not assume the job is just slow.
        """
        log = _log.bind(job_id=job.id, kind=job.agent_type)
        # Re-fetch — the Start hook may have flipped the row to
        # ``'started'`` in the happy path.
        latest = await self._store.get_by_id(job.id)
        if latest is None:
            return
        if latest.status != "requested":
            # Either Start hook fired (status='started') or the row was
            # cancelled/finalized by another path. Nothing to do here.
            return
        if bridge_error is not None:
            reason = f"bridge: {repr(bridge_error)[:300]}"
        else:
            reason = "model did not invoke Task tool"
        new_status = await self._store.mark_dispatch_failed(
            job_id=job.id, reason=reason
        )
        log.warning(
            "picker_dispatch_failed",
            reason=reason[:200],
            new_status=new_status,
            attempts=latest.attempts + 1,
        )
        if new_status == "error":
            await self._notify_terminal_error(latest, reason)

    async def _notify_terminal_error(
        self, job: SubagentJob, reason: str
    ) -> None:
        """Best-effort one-shot Telegram notify on terminal ``'error'``.

        Uses the adapter handle from ``self._adapter`` if injected;
        callers that did not pass one (test fixtures) get a logged
        skip rather than an AttributeError. The send_text exception is
        swallowed — losing the notify must not crash the picker loop.
        """
        adapter = getattr(self, "_adapter", None)
        if adapter is None:
            _log.info(
                "picker_terminal_error_no_adapter",
                job_id=job.id,
            )
            return
        body = (
            f"subagent job_id={job.id} kind={job.agent_type} "
            f"failed dispatch — marked error after retries.\n\n"
            f"reason: {reason[:200]}"
        )
        try:
            await adapter.send_text(job.callback_chat_id, body)
        except Exception:
            _log.warning(
                "picker_terminal_error_notify_failed",
                job_id=job.id,
                exc_info=True,
            )


# Helper used by tests / Daemon for symmetry with phase-5 dispatcher.
async def picker_idle_tick(picker: SubagentRequestPicker) -> Any:
    """Test helper: wait until the picker's stop event is set OR until
    its tick interval elapses. Useful in tests that want a barrier
    without poking ``picker._stop`` directly.
    """
    return await asyncio.wait_for(
        picker._stop.wait(),
        timeout=picker._settings.subagent.picker_tick_s,
    )
