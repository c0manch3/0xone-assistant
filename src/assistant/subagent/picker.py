"""Consumer loop: poll `subagent_jobs` for CLI-pending requests, dispatch.

Wave-2 B-W2-4 / S-1: sets the module-level `CURRENT_REQUEST_ID`
ContextVar in `subagent/context.py` before calling `bridge.ask(...)`.
The on_subagent_start hook reads the var (in the same event loop — the
SDK preserves contextvars across its internal Task boundary, verified
empirically by `spikes/phase6_s1_contextvar_hook.py`) and patches the
pending `requested` row with the SDK-assigned `agent_id`.

Wave-2 B-W2-6: the caller (`Daemon.start`) MUST pass a DEDICATED
`ClaudeBridge` instance — NOT the user-chat bridge. Each bridge owns
its own `asyncio.Semaphore(max_concurrent)`; sharing would let a
picker flood starve user turns. Q6 PASS guarantees the shared
SubagentStop hook still fires regardless of which bridge dispatches.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from assistant.config import Settings
from assistant.logger import get_logger
from assistant.subagent.context import CURRENT_REQUEST_ID
from assistant.subagent.store import SubagentJob, SubagentStore

if TYPE_CHECKING:
    from assistant.bridge.claude import ClaudeBridge

log = get_logger("subagent.picker")


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
    """Poll `subagent_jobs` for `status='requested' AND sdk_agent_id IS NULL`
    rows, dispatch each through the dedicated picker bridge.

    Lifecycle:
      * `run()` loop: wait on the stop_event with a `picker_tick_s`
        timeout; on timeout check for pending rows.
      * `request_stop()` sets the stop_event for graceful shutdown.

    Invariants:
      * ONE picker per Daemon (single-flock world). The store-level
        partial UNIQUE on `sdk_agent_id` would catch a rogue second
        instance at Start-hook patch time, but we don't rely on that.
      * `_inflight: set[int]` tracks job_ids currently being dispatched
        so a slow bridge + fast tick cannot re-enqueue the same row.
      * If a row's `cancel_requested=1` BEFORE the picker claims it,
        the row is left alone; `recover_orphans` (next daemon start or
        long-running sweep) transitions the stale `requested` row to
        `dropped` after the 1-h window.
    """

    def __init__(
        self,
        store: SubagentStore,
        bridge: ClaudeBridge,
        *,
        settings: Settings,
    ) -> None:
        self._store = store
        self._bridge = bridge
        self._settings = settings
        self._stop_event = asyncio.Event()
        self._inflight: set[int] = set()
        # Keep strong refs to dispatch tasks so GC doesn't drop a fire-
        # and-forget coroutine mid-flight (RUF006). The discard callback
        # clears the ref when the task's `finally` block runs.
        self._dispatch_tasks: set[asyncio.Task[None]] = set()

    def request_stop(self) -> None:
        """Signal the run loop to exit at its next tick boundary."""
        self._stop_event.set()

    def inflight(self) -> set[int]:
        """Expose the inflight snapshot for integration tests."""
        return set(self._inflight)

    def dispatch_tasks(self) -> set[asyncio.Task[None]]:
        """Expose the currently-in-flight dispatch tasks.

        Phase-6 fix-pack C-3 / devil C-3: `Daemon.stop()` must drain
        these BEFORE closing the aiosqlite connection. Without this
        the SDK subprocess keeps running, the Stop hook's
        `record_finished` UPDATE runs against a closed connection
        (`ProgrammingError: Cannot operate on a closed database`),
        AND the shielded `adapter.send_text` notify fails because
        the adapter was already stopped. Mirrors the phase-5
        `SchedulerDispatcher.pending_updates()` accessor pattern.

        Returns a snapshot `set` so the caller can iterate safely
        while the discard callback continues to mutate the underlying
        `self._dispatch_tasks`.
        """
        return set(self._dispatch_tasks)

    async def run(self) -> None:
        """Cooperative poll loop. Returns when the stop_event is set.

        Note: individual `_dispatch_one` tasks created during the
        loop's lifetime are NOT awaited here — they live on
        `self._dispatch_tasks` (discarded by the done callback). The
        authoritative drain lives in `Daemon.stop()`, which calls
        `dispatch_tasks()` and gathers them with a timeout BEFORE
        closing the DB connection and stopping the adapter. Closing
        them here would double the shutdown latency (one drain inside
        run()'s finally, another in Daemon.stop's gather), without
        the benefit of ordered DB close ahead of anything else."""
        tick = self._settings.subagent.picker_tick_s
        while not self._stop_event.is_set():
            try:
                pending = await self._store.list_pending_requests(limit=1)
            except Exception:
                log.warning("picker_list_failed", exc_info=True)
                pending = []
            for job in pending:
                if job.id in self._inflight:
                    continue
                if job.cancel_requested:
                    log.info("picker_skipping_cancelled", job_id=job.id)
                    continue
                self._inflight.add(job.id)
                task = asyncio.create_task(
                    self._dispatch_one(job),
                    name=f"picker_dispatch_{job.id}",
                )
                self._dispatch_tasks.add(task)
                task.add_done_callback(self._dispatch_tasks.discard)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=tick)
            except TimeoutError:
                continue
        log.info("picker_stopped")

    async def _dispatch_one(self, job: SubagentJob) -> None:
        """Dispatch one pending request via the dedicated picker bridge.

        Sets CURRENT_REQUEST_ID so on_subagent_start patches the row
        from `requested` → `started` with the real `sdk_agent_id`.
        """
        # Import lazily — avoids the circular `picker → bridge → hooks`
        # at module load time, and keeps picker importable from CLI
        # contexts that don't have the bridge.
        from assistant.bridge.claude import ClaudeBridgeError

        token = CURRENT_REQUEST_ID.set(job.id)
        prompt = _PICKER_PROMPT_TEMPLATE.format(kind=job.agent_type, task_text=job.task_text or "")
        try:
            async for _msg in self._bridge.ask(
                chat_id=self._settings.owner_chat_id,
                user_text=prompt,
                history=[],
            ):
                pass
        except ClaudeBridgeError:
            log.warning("picker_bridge_error", job_id=job.id, exc_info=True)
        except asyncio.CancelledError:
            log.info("picker_dispatch_cancelled", job_id=job.id)
            raise
        except Exception:
            log.warning("picker_unexpected_error", job_id=job.id, exc_info=True)
        finally:
            CURRENT_REQUEST_ID.reset(token)
            self._inflight.discard(job.id)
