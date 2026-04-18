"""aiosqlite-backed ledger for SDK-native subagent jobs (phase 6).

Reuses `ConversationStore.conn` + `ConversationStore.lock` — phase-5 S-1
confirmed the shared writer-lock has p99 ~3.4 ms, well below the 100 ms
budget, so a dedicated connection is unnecessary (pitfall #8).

Every transition UPDATE carries a status precondition in the `WHERE`
clause (pitfall #9 / phase-5 G-W2-6). On `rowcount=0` we log a skew
warning and do NOT raise — `mark_pending_retry`-style idempotency lets
racing paths (cancel vs Stop hook, recover_orphans vs Stop hook) settle
without crashing the caller.

Status machine:
    requested → started → (completed | failed | stopped |
                           interrupted | error | dropped)

Only `requested` rows carry `sdk_agent_id IS NULL`. Partial UNIQUE on
`sdk_agent_id WHERE sdk_agent_id IS NOT NULL` lets the CLI/picker pre-
create multiple NULL rows without constraint conflicts while still
blocking double-start on the same SDK-assigned id.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from assistant.logger import get_logger

log = get_logger("subagent.store")


_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "stopped", "interrupted", "error", "dropped"}
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _utcnow_iso() -> str:
    return _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SubagentJob:
    """Row view of `subagent_jobs`. Immutable — transitions rebuild."""

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
    depth: int
    created_at: str
    started_at: str | None
    finished_at: str | None


_SELECT_COLS = (
    "id, sdk_agent_id, sdk_session_id, parent_session_id, agent_type, "
    "task_text, transcript_path, status, cancel_requested, result_summary, "
    "cost_usd, callback_chat_id, spawned_by_kind, spawned_by_ref, depth, "
    "created_at, started_at, finished_at"
)


def _row_to_job(row: Sequence[Any]) -> SubagentJob:
    return SubagentJob(
        id=int(row[0]),
        sdk_agent_id=row[1],
        sdk_session_id=row[2],
        parent_session_id=row[3],
        agent_type=str(row[4]),
        task_text=row[5],
        transcript_path=row[6],
        status=str(row[7]),
        cancel_requested=bool(row[8]),
        result_summary=row[9],
        cost_usd=float(row[10]) if row[10] is not None else None,
        callback_chat_id=int(row[11]),
        spawned_by_kind=str(row[12]),
        spawned_by_ref=row[13],
        depth=int(row[14]),
        created_at=str(row[15]),
        started_at=row[16],
        finished_at=row[17],
    )


class SubagentStore:
    """aiosqlite ledger for SDK-native subagent jobs."""

    def __init__(self, conn: aiosqlite.Connection, *, lock: asyncio.Lock) -> None:
        self._conn = conn
        self._lock = lock

    # ---------- INSERT ----------

    async def record_pending_request(
        self,
        *,
        agent_type: str,
        task_text: str,
        callback_chat_id: int,
        spawned_by_kind: str,
        spawned_by_ref: str | None = None,
    ) -> int:
        """INSERT a pre-picker request row.

        Used by `tools/task/main.py spawn` — row has `status='requested'`
        AND `sdk_agent_id IS NULL` (the partial UNIQUE index tolerates).
        The picker consumes these via `list_pending_requests` and the
        Start hook patches `sdk_agent_id` via
        `update_sdk_agent_id_for_claimed_request`.

        Returns the auto-increment id so the CLI can print
        `{"job_id": N, "status": "requested"}`.
        """
        async with self._lock:
            cur = await self._conn.execute(
                "INSERT INTO subagent_jobs("
                "agent_type, task_text, status, callback_chat_id, "
                "spawned_by_kind, spawned_by_ref) "
                "VALUES (?, ?, 'requested', ?, ?, ?)",
                (
                    agent_type,
                    task_text,
                    callback_chat_id,
                    spawned_by_kind,
                    spawned_by_ref,
                ),
            )
            await self._conn.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

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
        """INSERT a NEW row for a native-Task (main-turn) spawn.

        The picker path uses `update_sdk_agent_id_for_claimed_request`
        instead. This path is taken when the ContextVar is unset (owner
        typed "use Task tool" in a main turn). Partial UNIQUE blocks
        double-start on the same `sdk_agent_id` — on `IntegrityError`
        we log skew and return the existing row's id so the caller can
        still attach the Stop hook to it.
        """
        started_at = _utcnow_iso()
        async with self._lock:
            try:
                cur = await self._conn.execute(
                    "INSERT INTO subagent_jobs("
                    "sdk_agent_id, parent_session_id, agent_type, status, "
                    "callback_chat_id, spawned_by_kind, spawned_by_ref, started_at) "
                    "VALUES (?, ?, ?, 'started', ?, ?, ?, ?)",
                    (
                        sdk_agent_id,
                        parent_session_id,
                        agent_type,
                        callback_chat_id,
                        spawned_by_kind,
                        spawned_by_ref,
                        started_at,
                    ),
                )
                await self._conn.commit()
            except aiosqlite.IntegrityError:
                # Partial UNIQUE blocked — someone already wrote this agent_id.
                await self._conn.rollback()
                log.warning(
                    "subagent_record_started_duplicate",
                    sdk_agent_id=sdk_agent_id,
                )
                async with self._conn.execute(
                    "SELECT id FROM subagent_jobs WHERE sdk_agent_id=?",
                    (sdk_agent_id,),
                ) as dup_cur:
                    dup_row = await dup_cur.fetchone()
                if dup_row is None:
                    # Extremely unlikely — raced against a DELETE? re-raise.
                    raise
                return int(dup_row[0])
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    async def update_sdk_agent_id_for_claimed_request(
        self,
        *,
        job_id: int,
        sdk_agent_id: str,
        parent_session_id: str | None,
    ) -> bool:
        """Status-precondition UPDATE: flip a `requested` row to `started`
        and stamp the real `sdk_agent_id` + `parent_session_id`.

        Returns True iff `rowcount > 0`. Used by on_subagent_start when
        the ContextVar says "this is a picker-claimed request".
        """
        started_at = _utcnow_iso()
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE subagent_jobs SET "
                "sdk_agent_id=?, parent_session_id=?, status='started', "
                "started_at=? "
                "WHERE id=? AND status='requested'",
                (sdk_agent_id, parent_session_id, started_at, job_id),
            )
            await self._conn.commit()
        updated = (cur.rowcount or 0) > 0
        if not updated:
            log.warning(
                "subagent_update_sdk_agent_id_skew",
                job_id=job_id,
                sdk_agent_id=sdk_agent_id,
            )
        return updated

    async def record_finished(
        self,
        *,
        sdk_agent_id: str,
        status: str,
        result_summary: str | None,
        transcript_path: str | None,
        sdk_session_id: str | None,
        cost_usd: float | None = None,
    ) -> None:
        """UPDATE to a terminal status with a status precondition.

        Transitions ONLY from `status='started'`. Concurrent
        `recover_orphans` (which transitions orphan `started` rows to
        `interrupted`) or a cancel-dropped race could flip the row
        before us; `rowcount=0` → log skew and return (pitfall #9).
        """
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"record_finished: status {status!r} not terminal")
        finished_at = _utcnow_iso()
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE subagent_jobs SET "
                "status=?, result_summary=?, transcript_path=?, "
                "sdk_session_id=?, cost_usd=?, finished_at=? "
                "WHERE sdk_agent_id=? AND status='started'",
                (
                    status,
                    result_summary,
                    transcript_path,
                    sdk_session_id,
                    cost_usd,
                    finished_at,
                    sdk_agent_id,
                ),
            )
            await self._conn.commit()
        if (cur.rowcount or 0) == 0:
            log.warning(
                "subagent_record_finished_skew",
                sdk_agent_id=sdk_agent_id,
                target_status=status,
            )

    # ---------- cancel ----------

    async def set_cancel_requested(self, job_id: int) -> dict[str, str | bool]:
        """UPDATE `cancel_requested=1` for a non-terminal row.

        Returns `{"cancel_requested": True, "previous_status": <str>}`
        on success, or `{"already_terminal": <str>}` if the row is
        already past a terminal transition (no-op). A `requested`-status
        row that is cancelled BEFORE the picker picks it up stays as
        `requested` — the picker's dispatch loop checks
        `cancel_requested` and short-circuits; recover_orphans then
        transitions it to `dropped` at the next stale bucket window.
        """
        async with self._lock:
            async with self._conn.execute(
                "SELECT status FROM subagent_jobs WHERE id=?",
                (job_id,),
            ) as sel_cur:
                row = await sel_cur.fetchone()
            if row is None:
                await self._conn.commit()
                return {"already_terminal": "missing"}
            current_status = str(row[0])
            if current_status in _TERMINAL_STATUSES:
                await self._conn.commit()
                return {"already_terminal": current_status}
            cur = await self._conn.execute(
                "UPDATE subagent_jobs SET cancel_requested=1 "
                "WHERE id=? AND status IN ('requested', 'started')",
                (job_id,),
            )
            await self._conn.commit()
        if (cur.rowcount or 0) == 0:
            # Race: status flipped between SELECT and UPDATE.
            log.info("subagent_cancel_raced_terminal", job_id=job_id)
            return {"already_terminal": current_status}
        return {"cancel_requested": True, "previous_status": current_status}

    async def drop_cancelled_request(self, job_id: int) -> bool:
        """Transition a cancelled `requested` row straight to `dropped`.

        Phase-6 fix-pack HIGH #1 (CR I-3 / devil H-3). The picker
        previously logged `picker_skipping_cancelled` on every tick
        for a row in `status='requested' AND cancel_requested=1` —
        plan §3.6 promises `recover_orphans` will eventually drop
        those via the 1-h stale bucket, but that is 3600 log lines
        per cancelled row in the interim. With this method the
        picker transitions the row explicitly on the first
        observation, so subsequent ticks don't see it at all.

        Status precondition: the row MUST still be `requested` AND
        carry `cancel_requested=1`. A race where the picker already
        flipped the row to `started` between `list_pending_requests`
        and this call leaves `rowcount=0` — we log skew and return
        False so the caller can fall through cleanly.

        Returns True iff the row transitioned.
        """
        finished_at = _utcnow_iso()
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE subagent_jobs SET status='dropped', finished_at=? "
                "WHERE id=? AND status='requested' AND cancel_requested=1",
                (finished_at, job_id),
            )
            await self._conn.commit()
        dropped = (cur.rowcount or 0) > 0
        if not dropped:
            log.info("subagent_drop_cancelled_noop", job_id=job_id)
        return dropped

    async def is_cancel_requested(self, sdk_agent_id: str) -> bool:
        """Read the cancel flag by `sdk_agent_id`.

        Used by PreToolUse flag-poll hook (S-6-0 Q7 fallback). Returns
        False if the row is missing — a race where the PreToolUse fires
        before the ledger writes the row (shouldn't happen — Start hook
        runs before any tool call — but defensive).
        """
        async with self._conn.execute(
            "SELECT cancel_requested FROM subagent_jobs WHERE sdk_agent_id=?",
            (sdk_agent_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        return bool(row[0])

    # ---------- recovery ----------

    async def recover_orphans(self, *, stale_requested_after_s: int = 3600) -> dict[str, int]:
        """Single run at Daemon.start BEFORE picker/bridge accept new turns.

        Four branches (returns counts for two buckets):
          * `status='started' AND finished_at IS NULL`  → `'interrupted'`
          * `status='started' AND sdk_agent_id IS NULL` → `'dropped'`
            (defensive; shouldn't occur in normal flow but if a bug
            ever writes such a row we transition it out of running-state
            so picker/hooks don't crash on it)
          * `status='requested' AND created_at < now - stale_after` → `'dropped'`
          * `status='requested' AND created_at >= now - stale_after` → leave

        Returns `{"interrupted": N1, "dropped": N2}`. Caller (Daemon.start)
        emits a split owner-facing notify summarising the two categories.
        """
        now = _utcnow()
        cutoff_iso = (now - timedelta(seconds=stale_requested_after_s)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        interrupted_ts = _utcnow_iso()
        async with self._lock:
            # Branch 1: started + not finished → interrupted. Includes the
            # defensive sdk_agent_id-NULL branch by the same terminal state.
            int_cur = await self._conn.execute(
                "UPDATE subagent_jobs SET status='interrupted', finished_at=? "
                "WHERE status='started' AND finished_at IS NULL "
                "AND sdk_agent_id IS NOT NULL",
                (interrupted_ts,),
            )
            interrupted = int_cur.rowcount or 0
            # Defensive sdk_agent_id IS NULL in 'started' — transition out
            # but to 'dropped' so it's distinguishable from real interrupts.
            drop_null_cur = await self._conn.execute(
                "UPDATE subagent_jobs SET status='dropped', finished_at=? "
                "WHERE status='started' AND sdk_agent_id IS NULL",
                (interrupted_ts,),
            )
            dropped_defensive = drop_null_cur.rowcount or 0
            # Branch 2: requested stale → dropped.
            drop_cur = await self._conn.execute(
                "UPDATE subagent_jobs SET status='dropped', finished_at=? "
                "WHERE status='requested' AND created_at < ?",
                (interrupted_ts, cutoff_iso),
            )
            dropped_stale = drop_cur.rowcount or 0
            await self._conn.commit()
        if dropped_defensive:
            log.warning(
                "subagent_recover_orphans_dropped_defensive",
                count=dropped_defensive,
            )
        return {
            "interrupted": interrupted,
            "dropped": dropped_stale + dropped_defensive,
        }

    # ---------- queries ----------

    async def get_by_agent_id(self, sdk_agent_id: str) -> SubagentJob | None:
        async with self._conn.execute(
            f"SELECT {_SELECT_COLS} FROM subagent_jobs WHERE sdk_agent_id=?",
            (sdk_agent_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_job(row)

    async def get_by_id(self, job_id: int) -> SubagentJob | None:
        async with self._conn.execute(
            f"SELECT {_SELECT_COLS} FROM subagent_jobs WHERE id=?",
            (job_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_job(row)

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
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(int(limit))
        sql = f"SELECT {_SELECT_COLS} FROM subagent_jobs {where}ORDER BY id DESC LIMIT ?"
        async with self._conn.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [_row_to_job(r) for r in rows]

    async def list_pending_requests(self, limit: int = 10) -> list[SubagentJob]:
        """Picker source: rows with `status='requested' AND sdk_agent_id IS NULL`,
        oldest `created_at` first."""
        async with self._conn.execute(
            f"SELECT {_SELECT_COLS} FROM subagent_jobs "
            "WHERE status='requested' AND sdk_agent_id IS NULL "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            (int(limit),),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_job(r) for r in rows]

    async def claim_pending_request(self, job_id: int) -> bool:
        """No-op status change kept for readability of picker code.

        The single-daemon flock invariant means we don't need a separate
        claim marker — the picker reads `list_pending_requests`, calls
        `bridge.ask`, and on Start hook the row flips
        `requested → started`. A daemon crash between read and flip
        leaves the row at `requested`, which `recover_orphans` handles
        via the 1-hour stale bucket.
        """
        del job_id
        return True
