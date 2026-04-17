"""aiosqlite-backed persistence for schedules + triggers (phase 5).

All writes go through the shared `ConversationStore.lock` (plan §1.11 /
spike S-1: p99 ~3.4 ms is two orders of magnitude below the 100 ms
budget, so no dedicated connection). Every transition is gated by a
status precondition in the `WHERE` clause — if a concurrent path
already moved the row (sweep, cancel, operator delete) the UPDATE is a
no-op (rowcount=0) and the caller logs a `scheduler_trigger_state_skew`
warning instead of raising. This keeps the dispatcher's in-memory view
honest without requiring a second consistency check per transition.

The store never owns queue state; `_inflight` lives on the dispatcher
and is passed into `revert_stuck_sent` as an `exclude_ids` set so the
runtime sweep does not punish a consumer that is mid-delivery.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from assistant.logger import get_logger
from assistant.scheduler import from_iso_utc, iso_utc_z
from assistant.scheduler.cron import matches_local, parse_cron

log = get_logger("scheduler.store")


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class SchedulerStore:
    """aiosqlite wrapper for `schedules` + `triggers`."""

    def __init__(self, conn: aiosqlite.Connection, lock: asyncio.Lock) -> None:
        self._conn = conn
        self._lock = lock

    # ------------------------------------------------------------------ schedules

    async def insert_schedule(self, *, cron: str, prompt: str, tz: str = "UTC") -> int:
        """Insert a new enabled schedule. Returns the new row id."""
        async with self._lock:
            cur = await self._conn.execute(
                "INSERT INTO schedules(cron, prompt, tz, enabled) VALUES (?, ?, ?, 1)",
                (cron, prompt, tz),
            )
            await self._conn.commit()
        # aiosqlite types `lastrowid` as `int | None`; INSERT always sets it.
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    async def count_enabled(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) FROM schedules WHERE enabled=1") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def list_schedules(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT id, cron, prompt, tz, enabled, created_at, last_fire_at FROM schedules"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id ASC"
        async with self._conn.execute(sql) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "cron": r[1],
                "prompt": r[2],
                "tz": r[3],
                "enabled": bool(r[4]),
                "created_at": r[5],
                "last_fire_at": r[6],
            }
            for r in rows
        ]

    async def iter_enabled_schedules(self) -> list[dict[str, Any]]:
        """Snapshot of every enabled schedule — producer loop iterates this
        once per tick."""
        return await self.list_schedules(enabled_only=True)

    async def set_enabled(self, schedule_id: int, enabled: bool) -> bool:
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE schedules SET enabled=? WHERE id=?",
                (1 if enabled else 0, schedule_id),
            )
            await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def delete_schedule(self, schedule_id: int) -> bool:
        """Hard-delete — cascades to triggers via FK."""
        async with self._lock:
            cur = await self._conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
            await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        async with self._conn.execute(
            "SELECT id, cron, prompt, tz, enabled, created_at, last_fire_at "
            "FROM schedules WHERE id=?",
            (schedule_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "cron": row[1],
            "prompt": row[2],
            "tz": row[3],
            "enabled": bool(row[4]),
            "created_at": row[5],
            "last_fire_at": row[6],
        }

    # ------------------------------------------------------------------ producer

    async def try_materialize_trigger(
        self, schedule_id: int, prompt: str, scheduled_for: datetime
    ) -> int | None:
        """INSERT OR IGNORE + UPDATE last_fire_at atomically (plan §5.3).

        Returns the new trigger_id on a successful INSERT, or None when the
        UNIQUE(schedule_id, scheduled_for) check rejected the row — in which
        case `last_fire_at` is NOT advanced (S-8 atomicity). The invariant
        `last_fire_at == max(triggers.scheduled_for)` relies on this.
        """
        iso = iso_utc_z(scheduled_for)
        async with self._lock:
            cur = await self._conn.execute(
                "INSERT OR IGNORE INTO triggers(schedule_id, prompt, scheduled_for) "
                "VALUES (?, ?, ?)",
                (schedule_id, prompt, iso),
            )
            if (cur.rowcount or 0) == 0:
                # UNIQUE violation — already materialised. No last_fire_at advance.
                await self._conn.commit()
                return None
            trigger_id = cur.lastrowid
            await self._conn.execute(
                "UPDATE schedules SET last_fire_at=? WHERE id=?",
                (iso, schedule_id),
            )
            await self._conn.commit()
        assert trigger_id is not None
        return int(trigger_id)

    # ------------------------------------------------------------------ transitions
    # Each transition UPDATE carries a status precondition (wave-2 G-W2-6).
    # rowcount=0 → caller logs `scheduler_trigger_state_skew` and returns.

    async def mark_sent(self, trigger_id: int) -> bool:
        """pending → sent (set sent_at=now). Returns True iff row was updated."""
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE triggers SET status='sent', sent_at=? WHERE id=? AND status='pending'",
                (_utcnow_iso(), trigger_id),
            )
            await self._conn.commit()
        updated = (cur.rowcount or 0) > 0
        if not updated:
            log.warning(
                "scheduler_trigger_state_skew",
                trigger_id=trigger_id,
                transition="pending->sent",
            )
        return updated

    async def mark_acked(self, trigger_id: int) -> bool:
        """sent → acked (set acked_at=now)."""
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE triggers SET status='acked', acked_at=? WHERE id=? AND status='sent'",
                (_utcnow_iso(), trigger_id),
            )
            await self._conn.commit()
        updated = (cur.rowcount or 0) > 0
        if not updated:
            log.warning(
                "scheduler_trigger_state_skew",
                trigger_id=trigger_id,
                transition="sent->acked",
            )
        return updated

    async def mark_pending_retry(self, trigger_id: int, last_error: str) -> int:
        """sent → pending with attempts += 1. Returns NEW attempts.

        When the precondition fails (e.g. sweep already reverted the row),
        we return the current attempts count without raising — the caller
        treats this as an idempotent no-op and logs the skew. The
        `asyncio.shield` wrapper at the dispatcher's `CancelledError`
        branch depends on this not-raising contract (wave-2 B-W2-3).
        """
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE triggers SET status='pending', attempts=attempts+1, "
                "last_error=? WHERE id=? AND status='sent'",
                (last_error, trigger_id),
            )
            await self._conn.commit()
            async with self._conn.execute(
                "SELECT attempts FROM triggers WHERE id=?", (trigger_id,)
            ) as row_cur:
                row = await row_cur.fetchone()
        attempts = int(row[0]) if row else 0
        if (cur.rowcount or 0) == 0:
            log.warning(
                "scheduler_trigger_state_skew",
                trigger_id=trigger_id,
                transition="sent->pending_retry",
                current_attempts=attempts,
            )
        return attempts

    async def mark_dead(self, trigger_id: int, last_error: str) -> bool:
        """pending → dead (terminal) — gated by attempts threshold satisfied
        in caller before this call (dispatcher checks attempts >= threshold)."""
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE triggers SET status='dead', last_error=? WHERE id=? AND status='pending'",
                (last_error, trigger_id),
            )
            await self._conn.commit()
        updated = (cur.rowcount or 0) > 0
        if not updated:
            log.warning(
                "scheduler_trigger_state_skew",
                trigger_id=trigger_id,
                transition="pending->dead",
            )
        return updated

    async def mark_dropped(self, trigger_id: int, reason: str) -> bool:
        """Terminal `dropped` state — only valid from pending or sent."""
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE triggers SET status='dropped', last_error=? "
                "WHERE id=? AND status IN ('pending','sent')",
                (reason, trigger_id),
            )
            await self._conn.commit()
        updated = (cur.rowcount or 0) > 0
        if not updated:
            log.warning(
                "scheduler_trigger_state_skew",
                trigger_id=trigger_id,
                transition="*->dropped",
            )
        return updated

    # ------------------------------------------------------------------ recovery

    async def clean_slate_sent(self) -> int:
        """Boot-only clean-slate: revert every `status='sent'` → `pending`.

        Runs from `Daemon.start()` BEFORE the dispatcher starts accepting,
        so the dispatcher's `_inflight` set is still empty — a row in
        `sent` state without an in-flight delivery is provably orphaned
        from a prior crash (§8.2).
        """
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE triggers SET status='pending', attempts=attempts+1 WHERE status='sent'"
            )
            await self._conn.commit()
        return cur.rowcount or 0

    async def revert_stuck_sent(self, timeout_s: int, exclude_ids: set[int]) -> int:
        """Runtime sweep: revert `sent` rows older than `timeout_s` that are
        NOT in `exclude_ids` (the dispatcher's `_inflight` snapshot). B2 /
        wave-2 B-W2-1.

        We cannot bind a variable-length `NOT IN (…)` list directly via
        parameters without building the placeholder list manually — do
        exactly that, keeping the SQL otherwise parameterised.
        """
        cutoff_iso = iso_utc_z(datetime.now(UTC) - timedelta(seconds=timeout_s))
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            sql = (
                "UPDATE triggers SET status='pending', attempts=attempts+1 "
                "WHERE status='sent' AND sent_at <= ? "
                f"AND id NOT IN ({placeholders})"
            )
            params: tuple[Any, ...] = (cutoff_iso, *exclude_ids)
        else:
            sql = (
                "UPDATE triggers SET status='pending', attempts=attempts+1 "
                "WHERE status='sent' AND sent_at <= ?"
            )
            params = (cutoff_iso,)
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            await self._conn.commit()
        return cur.rowcount or 0

    # ------------------------------------------------------------------ queries

    async def recent_triggers(
        self, *, schedule_id: int | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        if schedule_id is None:
            sql = (
                "SELECT id, schedule_id, prompt, scheduled_for, status, attempts, "
                "last_error, created_at, sent_at, acked_at "
                "FROM triggers ORDER BY id DESC LIMIT ?"
            )
            params: tuple[Any, ...] = (limit,)
        else:
            sql = (
                "SELECT id, schedule_id, prompt, scheduled_for, status, attempts, "
                "last_error, created_at, sent_at, acked_at "
                "FROM triggers WHERE schedule_id=? ORDER BY id DESC LIMIT ?"
            )
            params = (schedule_id, limit)
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        keys = (
            "id",
            "schedule_id",
            "prompt",
            "scheduled_for",
            "status",
            "attempts",
            "last_error",
            "created_at",
            "sent_at",
            "acked_at",
        )
        return [dict(zip(keys, r, strict=True)) for r in rows]

    async def count_catchup_misses(
        self,
        *,
        now: datetime,
        catchup_window_s: int = 3600,
        tz_default: str = "UTC",
    ) -> int:
        """Count missed trigger fires per enabled schedule at boot (GAP #16).

        For each schedule, walk minute boundaries in
            (max(last_fire_at | created_at, now - 4x catchup),
             now - catchup]
        — a bounded window — and sum matches. Cap at 4x catchup (wave-2
        G-W2-4): a schedule added a month ago with no fires still reports
        a bounded count instead of "thousands missed".

        tz parse failures / cron parse failures are skipped (best-effort
        aggregate); per-schedule warnings go to the structured log.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        del tz_default  # reserved for future fallback; per-row tz wins today

        schedules = await self.iter_enabled_schedules()
        total = 0
        upper = now.replace(second=0, microsecond=0) - timedelta(seconds=catchup_window_s)
        lower_cap = now.replace(second=0, microsecond=0) - timedelta(seconds=catchup_window_s * 4)
        for row in schedules:
            # lower-bound anchor per schedule.
            anchor_str = row["last_fire_at"] or row["created_at"]
            if not anchor_str:
                continue
            try:
                anchor = from_iso_utc(anchor_str)
            except ValueError:
                log.warning(
                    "scheduler_catchup_parse_failed",
                    schedule_id=row["id"],
                    anchor=anchor_str,
                )
                continue
            lower = max(anchor, lower_cap)
            if lower >= upper:
                continue
            try:
                expr = parse_cron(row["cron"])
            except Exception:
                log.warning("scheduler_catchup_cron_parse_failed", schedule_id=row["id"])
                continue
            try:
                tz = ZoneInfo(row["tz"])
            except (ZoneInfoNotFoundError, ValueError):
                log.warning(
                    "scheduler_catchup_tz_invalid",
                    schedule_id=row["id"],
                    tz=row["tz"],
                )
                continue
            # Walk minute-by-minute within (lower, upper]; each UTC minute
            # boundary converted to local and matched against expr.
            t = lower + timedelta(minutes=1)
            # Normalise to minute boundary.
            t = t.replace(second=0, microsecond=0)
            misses = 0
            while t <= upper:
                local = t.astimezone(tz).replace(tzinfo=None)
                if matches_local(expr, local):
                    misses += 1
                t += timedelta(minutes=1)
            total += misses
        return total
