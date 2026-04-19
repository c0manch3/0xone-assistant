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
from collections.abc import Iterable
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

    async def insert_schedule(
        self,
        *,
        cron: str,
        prompt: str,
        tz: str = "UTC",
        seed_key: str | None = None,
    ) -> int:
        """Insert a new enabled schedule. Returns the new row id.

        Phase 8: optional ``seed_key`` tags the row as a Daemon-managed
        default seed (e.g. ``"vault_auto_commit"``). The partial UNIQUE
        INDEX ``idx_schedules_seed_key`` (migration 0005) keeps at most
        one non-NULL-keyed row per key so Daemon restarts cannot create
        duplicates. User-created rows (``tools/schedule/main.py add``)
        pass ``seed_key=None`` and are not constrained by the index.
        """
        async with self._lock:
            cur = await self._conn.execute(
                "INSERT INTO schedules(cron, prompt, tz, enabled, seed_key) "
                "VALUES (?, ?, ?, 1, ?)",
                (cron, prompt, tz, seed_key),
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

    # ------------------------------------------------------------------ seed_key (phase 8)

    async def find_by_seed_key(self, seed_key: str) -> dict[str, Any] | None:
        """Return the row carrying ``seed_key`` or ``None`` if absent.

        The partial UNIQUE INDEX ``idx_schedules_seed_key`` means at
        most one row can match; a soft-deleted row (``enabled=0``) still
        shows up here — callers rely on this to avoid resurrecting a row
        that the owner disabled.
        """
        async with self._conn.execute(
            "SELECT id, cron, prompt, tz, enabled, seed_key "
            "FROM schedules WHERE seed_key=?",
            (seed_key,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "id": int(row[0]),
            "cron": row[1],
            "prompt": row[2],
            "tz": row[3],
            "enabled": bool(row[4]),
            "seed_key": row[5],
        }

    async def tombstone_exists(self, seed_key: str) -> bool:
        """Return True iff the owner has explicitly tombstoned this seed.

        Q10 (plan I-8.9): ``tools/schedule/main.py rm`` inserts into
        ``seed_tombstones`` so the next Daemon boot does NOT re-seed.
        """
        async with self._conn.execute(
            "SELECT 1 FROM seed_tombstones WHERE seed_key=?", (seed_key,)
        ) as cur:
            row = await cur.fetchone()
        return row is not None

    async def insert_tombstone(self, seed_key: str) -> None:
        """Async insert kept for symmetry / future callers.

        v2 B-D1 note: the ``rm`` CLI does **not** call this — it inserts
        via sync sqlite3 inside ``cmd_rm``'s own ``BEGIN IMMEDIATE``
        transaction so the soft-delete and the tombstone land together.
        Tests may call this method directly to set up scenarios.
        """
        async with self._lock:
            await self._conn.execute(
                "INSERT OR REPLACE INTO seed_tombstones(seed_key) VALUES (?)",
                (seed_key,),
            )
            await self._conn.commit()

    async def delete_tombstone(self, seed_key: str) -> bool:
        """Return True iff a tombstone row was removed.

        v2 B-D1 note: the ``revive-seed`` CLI does **not** call this —
        it deletes via sync sqlite3. Tests may call this directly.
        """
        async with self._lock:
            cur = await self._conn.execute(
                "DELETE FROM seed_tombstones WHERE seed_key=?", (seed_key,)
            )
            await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def ensure_seed_row(
        self,
        *,
        seed_key: str,
        cron: str,
        prompt: str,
        tz: str,
    ) -> tuple[int, str]:
        """Atomic ``tombstone_exists`` + ``find_by_seed_key`` + ``INSERT``.

        Returns ``(schedule_id, action)`` where ``action`` is one of
        ``{"exists", "tombstoned", "inserted"}``. ``schedule_id`` is
        ``0`` for ``"tombstoned"`` (no row is owned/created).

        v2 B-B1 / SF-B1: all three SQL operations run inside a single
        ``BEGIN IMMEDIATE`` transaction so a concurrent writer cannot
        slip a row in between the check and the insert. Defence-in-depth
        against the pidfile flock we already hold at the Daemon layer.
        """
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                async with self._conn.execute(
                    "SELECT 1 FROM seed_tombstones WHERE seed_key=?",
                    (seed_key,),
                ) as cur:
                    tombstone_row = await cur.fetchone()
                if tombstone_row is not None:
                    await self._conn.rollback()
                    return (0, "tombstoned")
                async with self._conn.execute(
                    "SELECT id FROM schedules WHERE seed_key=?",
                    (seed_key,),
                ) as cur:
                    existing = await cur.fetchone()
                if existing is not None:
                    await self._conn.rollback()
                    return (int(existing[0]), "exists")
                ins = await self._conn.execute(
                    "INSERT INTO schedules(cron, prompt, tz, enabled, seed_key) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (cron, prompt, tz, seed_key),
                )
                await self._conn.commit()
                assert ins.lastrowid is not None
                return (int(ins.lastrowid), "inserted")
            except Exception:
                await self._conn.rollback()
                raise

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

    async def list_pending_retries(
        self, *, exclude_ids: Iterable[int], limit: int = 32
    ) -> list[dict[str, Any]]:
        """Fix-pack CRITICAL #1: return `pending` rows that a previous
        `mark_pending_retry` left behind (status='pending' AND attempts>0),
        excluding any id currently in the dispatcher's `_inflight` set.

        The caller (producer loop) re-enqueues these onto the in-process
        queue — without this pass a failed trigger would remain an
        orphan in the DB until the next daemon reboot's `clean_slate_sent`.
        The explicit `attempts > 0` gate keeps pristine rows
        (`try_materialize_trigger` just inserted them; the producer's
        cron-match path is the only thing that should enqueue those)
        out of the retry pass — avoids a double-enqueue race.

        `limit` is a soft cap so a pathological backlog (e.g. the
        dispatcher was completely broken for hours) can't flood the
        queue in a single tick. Subsequent sweep ticks pick up the rest.
        """
        excludes = {int(i) for i in exclude_ids}
        if excludes:
            placeholders = ",".join("?" for _ in excludes)
            sql = (
                "SELECT id, schedule_id, prompt, scheduled_for, attempts "
                "FROM triggers "
                "WHERE status='pending' AND attempts > 0 "
                f"AND id NOT IN ({placeholders}) "
                "ORDER BY scheduled_for ASC LIMIT ?"
            )
            params: tuple[Any, ...] = (*excludes, limit)
        else:
            sql = (
                "SELECT id, schedule_id, prompt, scheduled_for, attempts "
                "FROM triggers "
                "WHERE status='pending' AND attempts > 0 "
                "ORDER BY scheduled_for ASC LIMIT ?"
            )
            params = (limit,)
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": int(r[0]),
                "schedule_id": int(r[1]),
                "prompt": str(r[2]),
                "scheduled_for": str(r[3]),
                "attempts": int(r[4]),
                "status": "pending",
            }
            for r in rows
        ]

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
