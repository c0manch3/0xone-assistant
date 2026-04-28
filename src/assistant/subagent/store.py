"""SQLite-backed CRUD + state-transition helpers for ``subagent_jobs``.

Runs on the shared ``assistant.db`` connection owned by
:class:`Daemon`. All multi-statement transactions are serialised on
the per-instance ``_lock`` (mirrors phase-5 ``SchedulerStore``).
``ConversationStore`` exposes no ``.lock`` attribute today; we keep
the same self-owned-lock pattern.

Status machine (research RQ4):
    requested → started → (completed|failed|stopped|interrupted|error|dropped)

Key invariants:
  * ``sdk_agent_id`` is the single identity key when present. Pre-picker
    rows carry NULL until on_subagent_start patches them via
    :meth:`update_sdk_agent_id_for_claimed_request`.
  * Every state-mutating UPDATE has a ``WHERE status='<expected>'``
    precondition. ``rowcount=0`` → log skew, never raise (phase-5 G-W2-6).
  * ``recover_orphans`` runs in three branches in a deterministic order
    (research RQ4 / devil H-7).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import aiosqlite
import structlog

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SubagentJob:
    id: int
    sdk_agent_id: str | None
    sdk_session_id: str | None
    parent_session_id: str | None
    agent_type: str
    task_text: str | None
    transcript_path: str | None
    status: str
    cancel_requested: bool
    result_summary: str | None
    cost_usd: float | None
    callback_chat_id: int
    spawned_by_kind: str
    spawned_by_ref: str | None
    # Fix-pack F1: picker-dispatch attempt counter + last error.
    # ``attempts`` increments every time the picker hits an error before
    # SubagentStart fires; once it reaches ``mark_dispatch_failed``'s
    # threshold the row flips to terminal ``'error'``.
    attempts: int
    last_error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class OrphanRecovery:
    """Tuple of recover_orphans branch counts (research RQ4)."""

    dropped_no_sdk: int
    interrupted: int
    dropped_stale: int

    @property
    def total(self) -> int:
        return self.dropped_no_sdk + self.interrupted + self.dropped_stale


# Terminal statuses — mutations against these are no-ops with a skew log.
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "stopped", "interrupted", "error", "dropped"}
)


class SubagentStore:
    """aiosqlite ledger for SDK-native subagent jobs."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        """Expose the inner lock so tests / picker can serialise compound ops."""
        return self._lock

    # ------------------------------------------------------------------
    # INSERT — pre-picker request rows (from @tool ``subagent_spawn``)
    # ------------------------------------------------------------------
    async def record_pending_request(
        self,
        *,
        agent_type: str,
        task_text: str,
        callback_chat_id: int,
        spawned_by_kind: str,
        spawned_by_ref: str | None = None,
    ) -> int:
        """INSERT a ``status='requested'`` row; ``sdk_agent_id`` stays NULL.

        Picker consumes via :meth:`list_pending_requests`; the SubagentStart
        hook patches ``sdk_agent_id`` + flips status to 'started' via
        :meth:`update_sdk_agent_id_for_claimed_request`.
        """
        async with self._lock:
            cur = await self._conn.execute(
                "INSERT INTO subagent_jobs("
                "agent_type, task_text, status, callback_chat_id, "
                "spawned_by_kind, spawned_by_ref) "
                "VALUES(?,?,?,?,?,?)",
                (
                    agent_type,
                    task_text,
                    "requested",
                    callback_chat_id,
                    spawned_by_kind,
                    spawned_by_ref,
                ),
            )
            await self._conn.commit()
            new_id = cur.lastrowid
            if new_id is None:
                raise RuntimeError(
                    "INSERT into subagent_jobs returned no id"
                )
            return int(new_id)

    # ------------------------------------------------------------------
    # INSERT — fresh row from on_subagent_start (native Task path)
    # ------------------------------------------------------------------
    async def record_started(
        self,
        *,
        sdk_agent_id: str,
        agent_type: str,
        parent_session_id: str | None,
        callback_chat_id: int,
        spawned_by_kind: str,
        spawned_by_ref: str | None,
    ) -> int:
        """INSERT a row with ``status='started'`` for native-Task spawns.

        Used when the parent turn called the SDK ``Task`` tool directly
        (no pre-picker request row exists). Partial UNIQUE on
        ``sdk_agent_id`` blocks duplicate Start fires for the same
        agent — on ``IntegrityError`` we log skew and return the
        existing row's id (looked up via ``get_by_agent_id``).
        """
        async with self._lock:
            try:
                cur = await self._conn.execute(
                    "INSERT INTO subagent_jobs("
                    "sdk_agent_id, agent_type, parent_session_id, "
                    "status, callback_chat_id, spawned_by_kind, "
                    "spawned_by_ref, started_at) "
                    "VALUES(?,?,?,?,?,?,?, "
                    "strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                    (
                        sdk_agent_id,
                        agent_type,
                        parent_session_id,
                        "started",
                        callback_chat_id,
                        spawned_by_kind,
                        spawned_by_ref,
                    ),
                )
                await self._conn.commit()
                new_id = cur.lastrowid
                if new_id is None:
                    raise RuntimeError(
                        "INSERT into subagent_jobs returned no id"
                    )
                return int(new_id)
            except aiosqlite.IntegrityError:
                # Duplicate Start hook fire — log + return existing id.
                await self._conn.rollback()
                _log.warning(
                    "subagent_record_started_duplicate",
                    sdk_agent_id=sdk_agent_id,
                )
                cur2 = await self._conn.execute(
                    "SELECT id FROM subagent_jobs WHERE sdk_agent_id=?",
                    (sdk_agent_id,),
                )
                row = await cur2.fetchone()
                return int(row[0]) if row else -1

    # ------------------------------------------------------------------
    # UPDATE — picker-claimed request gets its sdk_agent_id patched in
    # ------------------------------------------------------------------
    async def update_sdk_agent_id_for_claimed_request(
        self,
        *,
        job_id: int,
        sdk_agent_id: str,
        parent_session_id: str | None,
    ) -> bool:
        """Patch ``sdk_agent_id`` + ``parent_session_id`` and flip
        ``status`` from ``'requested'`` to ``'started'``.

        Status-precondition SQL: rowcount=0 means the row was already
        terminal (cancelled before pickup, dropped, etc.) — caller
        handles by falling back to ``record_started``.
        """
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE subagent_jobs SET "
                "sdk_agent_id=?, parent_session_id=?, status='started', "
                "started_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE id=? AND status='requested' AND sdk_agent_id IS NULL",
                (sdk_agent_id, parent_session_id, job_id),
            )
            await self._conn.commit()
            patched = (cur.rowcount or 0) > 0
            if not patched:
                _log.info(
                    "subagent_claim_skew",
                    job_id=job_id,
                    sdk_agent_id=sdk_agent_id,
                )
            return patched

    # ------------------------------------------------------------------
    # UPDATE — on_subagent_stop terminal transition
    # ------------------------------------------------------------------
    async def record_finished(
        self,
        *,
        sdk_agent_id: str,
        status: str,
        result_summary: str | None,
        transcript_path: str | None,
        sdk_session_id: str | None,
        cost_usd: float | None = None,
        last_error: str | None = None,
    ) -> bool:
        """UPDATE WHERE ``sdk_agent_id=?`` AND ``status='started'``.

        rowcount=0 → log skew, return False (phase-5 G-W2-6 / pitfall #9).

        Fix-pack F5 (devil H-W2-4 — Start↔Stop race):
        if the standard ``status='started'`` predicate fails AND a
        ``'requested'`` row carrying the same ``sdk_agent_id`` exists
        (Stop hook beat Start hook to the commit), we finalize THAT
        row instead of dropping the notify on the floor. The ContextVar
        on the picker bridge guarantees that a Stop hook only ever
        sees an ``sdk_agent_id`` that another path attempted to bind.

        Fix-pack F3 (QA HIGH-2): ``last_error`` parameter so Stop
        hook can persist the SDK's error text on a ``failed`` terminal
        transition without an extra UPDATE round-trip.
        """
        if status not in _TERMINAL_STATUSES:
            raise ValueError(
                f"record_finished called with non-terminal status {status!r}"
            )
        # Trim last_error so a runaway SDK error string can't bloat the row.
        trimmed_last_error = last_error[:500] if last_error else None
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE subagent_jobs SET "
                "status=?, result_summary=?, transcript_path=?, "
                "sdk_session_id=?, cost_usd=?, last_error=?, "
                "finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE sdk_agent_id=? AND status='started'",
                (
                    status,
                    result_summary,
                    transcript_path,
                    sdk_session_id,
                    cost_usd,
                    trimmed_last_error,
                    sdk_agent_id,
                ),
            )
            if (cur.rowcount or 0) > 0:
                await self._conn.commit()
                return True
            # Fix-pack F5: Start↔Stop race recovery. The Stop hook fired
            # before update_sdk_agent_id_for_claimed_request committed
            # the ``requested → started`` flip — the row is still
            # ``'requested'`` but already carries the agent_id that is
            # only ever assigned by the picker's ContextVar bind path.
            cur2 = await self._conn.execute(
                "SELECT id FROM subagent_jobs "
                "WHERE sdk_agent_id=? AND status='requested'",
                (sdk_agent_id,),
            )
            row = await cur2.fetchone()
            if row is not None:
                await self._conn.execute(
                    "UPDATE subagent_jobs SET "
                    "status=?, result_summary=?, transcript_path=?, "
                    "sdk_session_id=?, cost_usd=?, last_error=?, "
                    "started_at=COALESCE(started_at, strftime('%Y-%m-%dT%H:%M:%SZ','now')), "
                    "finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                    "WHERE id=?",
                    (
                        status,
                        result_summary,
                        transcript_path,
                        sdk_session_id,
                        cost_usd,
                        trimmed_last_error,
                        int(row[0]),
                    ),
                )
                await self._conn.commit()
                _log.warning(
                    "subagent_finished_via_race_recovery",
                    sdk_agent_id=sdk_agent_id,
                    job_id=int(row[0]),
                    desired_status=status,
                )
                return True
            await self._conn.commit()
            _log.info(
                "subagent_finished_skew",
                sdk_agent_id=sdk_agent_id,
                desired_status=status,
            )
            return False

    # ------------------------------------------------------------------
    # cancel
    # ------------------------------------------------------------------
    async def set_cancel_requested(
        self, job_id: int
    ) -> dict[str, Any]:
        """Flip ``cancel_requested=1`` on a non-terminal row.

        Returns either ``{"cancel_requested": True, "previous_status": <s>}``
        or ``{"already_terminal": <s>}``. Idempotent: a second call on a
        row with ``cancel_requested=1`` returns the same shape (no skew).

        Fix-pack F1 (code H1 / QA HIGH-4): when the row is still in
        ``'requested'`` (cancel arrived BEFORE the picker dispatched),
        we transition it directly to ``'stopped'`` AND set ``finished_at``
        in the SAME UPDATE. The previous behaviour left the row in
        ``'requested'`` with ``cancel_requested=1`` forever — the picker
        loop would log ``picker_skip_cancelled`` every tick because
        ``list_pending_requests`` excludes nothing on cancel state. Owner
        also never saw a ``stopped`` notify because no Stop hook fires
        without a SubagentStart.
        """
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT status FROM subagent_jobs WHERE id=?",
                (job_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return {"error": "job not found"}
            previous = str(row[0])
            if previous in _TERMINAL_STATUSES:
                return {"already_terminal": previous}
            await self._conn.execute(
                "UPDATE subagent_jobs SET "
                "cancel_requested=1, "
                "status=CASE WHEN status='requested' THEN 'stopped' "
                "  ELSE status END, "
                "finished_at=CASE WHEN status='requested' "
                "  THEN strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "  ELSE finished_at END "
                "WHERE id=? AND status NOT IN "
                "('completed','failed','stopped','interrupted','error','dropped')",
                (job_id,),
            )
            await self._conn.commit()
            return {
                "cancel_requested": True,
                "previous_status": previous,
            }

    async def mark_dispatch_failed(
        self,
        *,
        job_id: int,
        reason: str,
        max_attempts: int = 3,
    ) -> str:
        """Bump ``attempts`` on a still-``requested`` row; flip to ``'error'``
        when the threshold is reached. Returns the post-update status.

        Fix-pack F1 (code H1 / devil C-W2-4 / QA HIGH-3):
        the picker calls this after a ``ClaudeBridgeError`` (claude CLI
        down, OAuth expired, SDK 5xx) OR after a successful bridge.ask
        that did NOT trigger SubagentStart (model returned without
        invoking the Task tool). Without this, the row stays
        ``'requested'`` forever and the picker re-tries every tick —
        the original livelock devil C-W2-4 flagged.

        Mirrors phase-5 ``SchedulerStore`` dead-attempts pattern.

        ``reason`` is trimmed to 500 chars before persistence so a
        runaway exception ``repr`` doesn't bloat the row.
        """
        trimmed = reason[:500] if reason else None
        async with self._lock:
            await self._conn.execute(
                "UPDATE subagent_jobs SET "
                "attempts=attempts+1, last_error=?, "
                "status=CASE WHEN attempts+1 >= ? THEN 'error' "
                "  ELSE status END, "
                "finished_at=CASE WHEN attempts+1 >= ? "
                "  THEN strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "  ELSE finished_at END "
                "WHERE id=? AND status='requested'",
                (trimmed, max_attempts, max_attempts, job_id),
            )
            await self._conn.commit()
            cur = await self._conn.execute(
                "SELECT status FROM subagent_jobs WHERE id=?", (job_id,)
            )
            row = await cur.fetchone()
            return str(row[0]) if row else "unknown"

    async def is_cancel_requested(self, sdk_agent_id: str) -> bool:
        """SELECT cancel_requested by ``sdk_agent_id``.

        Used by the PreToolUse cancel-flag-poll hook (S-6-0 Q7 fallback).
        Returns False if the row is missing — race-tolerant.
        """
        cur = await self._conn.execute(
            "SELECT cancel_requested FROM subagent_jobs "
            "WHERE sdk_agent_id=?",
            (sdk_agent_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        return bool(int(row[0]))

    # ------------------------------------------------------------------
    # recovery — research RQ4 ordered branches
    # ------------------------------------------------------------------
    async def recover_orphans(
        self, *, stale_requested_after_s: int = 3600
    ) -> OrphanRecovery:
        """Three-branch boot recovery (research RQ4 / devil H-7).

        Branch 1 ("started but never SDK-delivered"): rows where
        the picker began ``record_started`` but the SDK never fired
        SubagentStart (or the daemon crashed inside the ~few-second
        gap). ``sdk_agent_id IS NULL`` so the row cannot be linked
        to any transcript. → ``'dropped'``. MUST run before Branch 2
        because Branch 2's predicate would otherwise swallow these.

        Branch 2 ("started, SDK got it, daemon crashed"): rows with
        ``status='started'`` AND ``sdk_agent_id IS NOT NULL`` AND
        ``finished_at IS NULL``. Real subagent existed; transcript may
        or may not have flushed; we cannot reliably read its result.
        → ``'interrupted'``; daemon notifies owner.

        Branch 3 ("requested but never picked up"): ``status='requested'``
        rows older than ``stale_requested_after_s``. Drop silently;
        do NOT notify (owner has already moved on).

        Each branch is its own UPDATE so a single transient SQLite
        error in one branch does not poison the others. Returns the
        per-branch rowcount for caller log + notify.
        """
        async with self._lock:
            cur1 = await self._conn.execute(
                "UPDATE subagent_jobs SET "
                "status='dropped', "
                "finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE status='started' AND sdk_agent_id IS NULL "
                "AND finished_at IS NULL"
            )
            dropped_no_sdk = int(cur1.rowcount or 0)
            cur2 = await self._conn.execute(
                "UPDATE subagent_jobs SET "
                "status='interrupted', "
                "finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE status='started' AND sdk_agent_id IS NOT NULL "
                "AND finished_at IS NULL"
            )
            interrupted = int(cur2.rowcount or 0)
            cur3 = await self._conn.execute(
                "UPDATE subagent_jobs SET "
                "status='dropped', "
                "finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE status='requested' AND "
                "julianday('now') - julianday(created_at) > ?/86400.0",
                (stale_requested_after_s,),
            )
            dropped_stale = int(cur3.rowcount or 0)
            await self._conn.commit()
        return OrphanRecovery(
            dropped_no_sdk=dropped_no_sdk,
            interrupted=interrupted,
            dropped_stale=dropped_stale,
        )

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    async def get_by_agent_id(
        self, sdk_agent_id: str
    ) -> SubagentJob | None:
        cur = await self._conn.execute(
            _SELECT_COLS + " WHERE sdk_agent_id=?", (sdk_agent_id,)
        )
        row = await cur.fetchone()
        return _row_to_job(row) if row else None

    async def get_by_id(self, job_id: int) -> SubagentJob | None:
        cur = await self._conn.execute(
            _SELECT_COLS + " WHERE id=?", (job_id,)
        )
        row = await cur.fetchone()
        return _row_to_job(row) if row else None

    async def list_jobs(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[SubagentJob]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if kind is not None:
            clauses.append("agent_type=?")
            params.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        cur = await self._conn.execute(
            _SELECT_COLS + where + " ORDER BY id DESC LIMIT ?",
            tuple(params),
        )
        rows = await cur.fetchall()
        return [_row_to_job(r) for r in rows]

    async def list_pending_requests(
        self, *, limit: int = 1
    ) -> list[SubagentJob]:
        """Picker source: rows with ``status='requested'`` AND
        ``sdk_agent_id IS NULL``, oldest ``created_at`` first.
        """
        cur = await self._conn.execute(
            _SELECT_COLS
            + " WHERE status='requested' AND sdk_agent_id IS NULL "
            "ORDER BY created_at ASC LIMIT ?",
            (int(limit),),
        )
        rows = await cur.fetchall()
        return [_row_to_job(r) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_SELECT_COLS = (
    "SELECT id, sdk_agent_id, sdk_session_id, parent_session_id, "
    "agent_type, task_text, transcript_path, status, cancel_requested, "
    "result_summary, cost_usd, callback_chat_id, spawned_by_kind, "
    "spawned_by_ref, attempts, last_error, created_at, started_at, "
    "finished_at "
    "FROM subagent_jobs"
)


def _row_to_job(row: Any) -> SubagentJob:
    return SubagentJob(
        id=int(row[0]),
        sdk_agent_id=row[1],
        sdk_session_id=row[2],
        parent_session_id=row[3],
        agent_type=str(row[4]),
        task_text=row[5],
        transcript_path=row[6],
        status=str(row[7]),
        cancel_requested=bool(int(row[8])),
        result_summary=row[9],
        cost_usd=float(row[10]) if row[10] is not None else None,
        callback_chat_id=int(row[11]),
        spawned_by_kind=str(row[12]),
        spawned_by_ref=row[13],
        attempts=int(row[14] or 0),
        last_error=row[15],
        created_at=str(row[16]),
        started_at=row[17],
        finished_at=row[18],
    )
