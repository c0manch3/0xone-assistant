"""SQLite-backed CRUD + state-transition helpers for ``schedules`` and
``triggers``.

Runs on the shared ``assistant.db`` connection owned by
:class:`Daemon`. All multi-statement transactions are serialised on a
per-instance ``asyncio.Lock`` (CR-2): ``ConversationStore`` has no
``.lock`` attribute to borrow from; aiosqlite's internal writer thread
serialises single statements at the SQL layer already, but
Python-level interleave across ``INSERT`` + ``UPDATE`` pairs would
break materialise-then-mark-sent atomicity otherwise.

Key invariants enforced here:
  - ``UNIQUE(schedule_id, scheduled_for)`` on ``triggers`` is the
    at-least-once contract — :meth:`try_materialize_trigger` returns
    ``None`` on duplicate so the loop NEVER double-enqueues a minute.
  - :meth:`revert_to_pending` bumps ``attempts``; the dispatcher dead-
    letters after threshold (5 by default).
  - :meth:`clean_slate_sent` is called exactly once at boot; any
    ``status='sent'`` row survived a daemon crash and must be replayed.
  - :meth:`sweep_expired_sent` is called every loop tick (CR2.1) —
    reverts stale ``sent`` rows mid-run so a handler that hangs past
    ``sent_revert_timeout_s`` doesn't pin a trigger forever.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Literal

import aiosqlite
import structlog

_log = structlog.get_logger(__name__)

BootClass = Literal["clean-deploy", "suspend-or-crash", "first-boot"]


def _iso_z(when: dt.datetime) -> str:
    """Serialise a UTC-aware datetime to the project's canonical
    ``YYYY-MM-DDTHH:MM:SSZ`` form, matching the SQL defaults in the
    schema (``strftime('%Y-%m-%dT%H:%M:%SZ','now')``).
    """
    if when.tzinfo is None:
        raise ValueError("expected timezone-aware datetime")
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_z(raw: str) -> dt.datetime:
    """Inverse of :func:`_iso_z`; returns a UTC-aware datetime."""
    return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))


class SchedulerStore:
    """CRUD + state-machine front for ``schedules`` / ``triggers``.

    ``_tx_lock`` is owned — CR-2 fix. Any caller wanting to compose
    multiple ``SchedulerStore`` operations atomically must still grab
    their own outer lock; this class only guarantees the internal
    compound transactions (materialise + last_fire_at bump, for
    example) commit-or-rollback together.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._tx_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # schedule CRUD
    # ------------------------------------------------------------------
    async def add_schedule(
        self,
        *,
        cron: str,
        prompt: str,
        tz: str,
        max_schedules: int,
    ) -> int:
        """INSERT a new schedule row. Raises :class:`ValueError` if the
        enabled-schedule cap is reached — plan §B caps at 64 to keep the
        owner from accidentally creating a recursion bomb.
        """
        async with self._tx_lock:
            cur = await self._conn.execute(
                "SELECT COUNT(*) FROM schedules WHERE enabled=1"
            )
            row = await cur.fetchone()
            if row and row[0] >= max_schedules:
                raise ValueError(
                    f"schedule cap reached ({max_schedules} enabled)"
                )
            cur = await self._conn.execute(
                "INSERT INTO schedules(cron, prompt, tz, enabled) "
                "VALUES(?,?,?,1)",
                (cron, prompt, tz),
            )
            await self._conn.commit()
            new_id = cur.lastrowid
            if new_id is None:
                raise RuntimeError("INSERT into schedules returned no id")
            return int(new_id)

    async def list_schedules(
        self, *, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, cron, prompt, tz, enabled, created_at, last_fire_at "
            "FROM schedules"
        )
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        cur = await self._conn.execute(sql)
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

    async def get_schedule(self, sched_id: int) -> dict[str, Any] | None:
        cur = await self._conn.execute(
            "SELECT id, cron, prompt, tz, enabled, created_at, last_fire_at "
            "FROM schedules WHERE id=?",
            (sched_id,),
        )
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

    async def disable_schedule(self, sched_id: int) -> bool:
        """Idempotent: returns ``True`` iff the row transitioned
        enabled=1 → 0 on this call. Already-disabled rows return ``False``.
        """
        async with self._tx_lock:
            cur = await self._conn.execute(
                "UPDATE schedules SET enabled=0 WHERE id=? AND enabled=1",
                (sched_id,),
            )
            await self._conn.commit()
            return (cur.rowcount or 0) > 0

    async def enable_schedule(self, sched_id: int) -> bool:
        """Idempotent: returns ``True`` iff the row transitioned
        enabled=0 → 1 on this call.
        """
        async with self._tx_lock:
            cur = await self._conn.execute(
                "UPDATE schedules SET enabled=1 WHERE id=? AND enabled=0",
                (sched_id,),
            )
            await self._conn.commit()
            return (cur.rowcount or 0) > 0

    async def schedule_exists(self, sched_id: int) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM schedules WHERE id=?", (sched_id,)
        )
        return await cur.fetchone() is not None

    # ------------------------------------------------------------------
    # trigger lifecycle
    # ------------------------------------------------------------------
    async def try_materialize_trigger(
        self,
        schedule_id: int,
        prompt_snapshot: str,
        scheduled_for: dt.datetime,
    ) -> int | None:
        """Attempt to INSERT a new trigger row.

        Returns the new trigger id on success, or ``None`` if the
        ``UNIQUE(schedule_id, scheduled_for)`` constraint fires — this
        is the at-least-once contract's dedup gate. The caller MUST
        treat ``None`` as "already handled, skip" — NOT as an error.

        ``prompt_snapshot`` is copied into ``triggers.prompt`` so the
        dispatcher reads an immutable per-fire prompt even if the model
        edits the parent schedule's prompt between materialisation and
        delivery (CR2.2).
        """
        iso = _iso_z(scheduled_for)
        async with self._tx_lock:
            try:
                cur = await self._conn.execute(
                    "INSERT INTO triggers(schedule_id, prompt, scheduled_for, "
                    "status) VALUES(?,?,?,'pending')",
                    (schedule_id, prompt_snapshot, iso),
                )
                await self._conn.execute(
                    "UPDATE schedules SET last_fire_at=? WHERE id=?",
                    (iso, schedule_id),
                )
                await self._conn.commit()
                new_id = cur.lastrowid
                if new_id is None:
                    return None
                return int(new_id)
            except aiosqlite.IntegrityError:
                await self._conn.rollback()
                return None

    async def mark_sent(self, trigger_id: int) -> None:
        """Transition a ``pending`` trigger to ``sent``.

        Fix 1 / CR-1a: guarded on ``status='pending'`` so a concurrent
        dispatcher task that beat the loop to a terminal state
        (``acked``/``dropped``/``dead``) cannot be clobbered back to
        ``sent`` — which would otherwise be reverted to ``pending`` by
        the next tick's sweep and re-enqueued (double-fire after restart
        via ``clean_slate_sent``).
        """
        async with self._tx_lock:
            await self._conn.execute(
                "UPDATE triggers SET status='sent', "
                "sent_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE id=? AND status='pending'",
                (trigger_id,),
            )
            await self._conn.commit()

    async def mark_acked(self, trigger_id: int) -> None:
        async with self._tx_lock:
            await self._conn.execute(
                "UPDATE triggers SET status='acked', "
                "acked_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE id=?",
                (trigger_id,),
            )
            await self._conn.commit()

    async def mark_dropped(self, trigger_id: int) -> None:
        async with self._tx_lock:
            await self._conn.execute(
                "UPDATE triggers SET status='dropped' WHERE id=?",
                (trigger_id,),
            )
            await self._conn.commit()

    async def mark_dead(self, trigger_id: int, last_error: str) -> None:
        async with self._tx_lock:
            await self._conn.execute(
                "UPDATE triggers SET status='dead', last_error=? WHERE id=?",
                (last_error[:500], trigger_id),
            )
            await self._conn.commit()

    async def revert_to_pending(
        self, trigger_id: int, *, last_error: str
    ) -> int:
        """Revert a ``sent``/``pending`` row back to ``pending`` and bump
        ``attempts``. Returns the new attempts count so the caller can
        compare against ``dead_attempts_threshold``.
        """
        async with self._tx_lock:
            await self._conn.execute(
                "UPDATE triggers SET status='pending', "
                "attempts=attempts+1, last_error=? WHERE id=?",
                (last_error[:500], trigger_id),
            )
            cur = await self._conn.execute(
                "SELECT attempts FROM triggers WHERE id=?", (trigger_id,)
            )
            row = await cur.fetchone()
            await self._conn.commit()
            return int(row[0]) if row else 0

    async def note_queue_saturation(
        self, trigger_id: int, *, last_error: str
    ) -> None:
        """H-1 fix: record queue saturation on the trigger row without
        incrementing attempts — it's our infrastructure issue, not a
        per-trigger failure. The row remains ``pending`` so the loop's
        reclaim sweep picks it up on the next tick.

        Fix 10 / code-review C-3: scope the UPDATE to
        ``status IN ('pending','sent')`` so a terminal row
        (``acked``/``dead``/``dropped``) never gets stomped with a
        misleading "queue saturated" ``last_error``.
        """
        async with self._tx_lock:
            await self._conn.execute(
                "UPDATE triggers SET last_error=? "
                "WHERE id=? AND status IN ('pending','sent')",
                (last_error[:500], trigger_id),
            )
            await self._conn.commit()

    async def reclaim_pending_not_queued(
        self, inflight: set[int], *, older_than_s: int = 30
    ) -> list[dict[str, Any]]:
        """Return pending triggers not currently in flight and older
        than ``older_than_s`` seconds, scoped to rows that hit a genuine
        queue-saturation event.

        Fix 2 / CR-2: the previous implementation picked up ANY stale
        pending row — which during a catchup walk (where just-materialised
        triggers legitimately have ``scheduled_for`` many minutes behind
        ``now``) could re-queue a row that the same tick already
        materialised. The SQL now requires
        ``last_error LIKE 'queue saturated%'`` so reclaim fires ONLY
        when :meth:`note_queue_saturation` marked the row — the concrete
        signal that dispatch was deferred.

        H2.3 correction (applied in code, not plan docs): filter by
        ``scheduled_for`` — not ``created_at``. ``created_at`` records
        the insert timestamp; ``scheduled_for`` is the wall-clock
        minute the trigger was meant to fire.
        """
        cur = await self._conn.execute(
            "SELECT id, schedule_id, prompt, scheduled_for FROM triggers "
            "WHERE status='pending' AND "
            "last_error LIKE 'queue saturated%' AND "
            "julianday('now') - julianday(scheduled_for) > ?/86400.0",
            (older_than_s,),
        )
        rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "schedule_id": r[1],
                "prompt": r[2],
                "scheduled_for": r[3],
            }
            for r in rows
            if r[0] not in inflight
        ]

    async def sweep_expired_sent(
        self, *, sent_revert_timeout_s: int
    ) -> int:
        """CR2.1: revert stale ``sent`` rows mid-run.

        A trigger that sits in ``status='sent'`` longer than
        ``sent_revert_timeout_s`` (default 360 — Claude timeout 300 + 60
        margin) is evidence of a handler that hung past the bridge's
        own timeout without clearing. Without this sweep, the row
        would stick in ``sent`` until the next daemon restart (when
        :meth:`clean_slate_sent` runs once). Called every loop tick.

        Returns the number of rows reverted — useful for log cadence.
        """
        async with self._tx_lock:
            cur = await self._conn.execute(
                "UPDATE triggers SET status='pending', attempts=attempts+1, "
                "last_error='sent-expired: no ack within sent_revert_timeout_s' "
                "WHERE status='sent' AND sent_at IS NOT NULL AND "
                "julianday('now') - julianday(sent_at) > ?/86400.0",
                (sent_revert_timeout_s,),
            )
            await self._conn.commit()
            reverted = int(cur.rowcount or 0)
            if reverted:
                _log.warning(
                    "scheduler_sent_expired_reverted",
                    count=reverted,
                    timeout_s=sent_revert_timeout_s,
                )
            return reverted

    # ------------------------------------------------------------------
    # recovery
    # ------------------------------------------------------------------
    async def clean_slate_sent(self) -> int:
        """Revert every ``sent`` row to ``pending`` on boot.

        Singleton flock guarantees no other daemon is running, so any
        ``sent`` trigger is orphan work from the previous process. The
        ``attempts`` bump makes the eventual dead-letter threshold
        fire if the same trigger keeps hanging the handler.
        """
        async with self._tx_lock:
            cur = await self._conn.execute(
                "UPDATE triggers SET status='pending', attempts=attempts+1 "
                "WHERE status='sent'"
            )
            await self._conn.commit()
            return int(cur.rowcount or 0)

    async def count_catchup_misses(
        self, *, catchup_window_s: int
    ) -> int:
        """Count enabled schedules whose ``last_fire_at`` is older than
        the catchup window — they definitely missed firings.

        Plan §G: we use this count (not per-miss cardinality) to
        decide whether the recap notify fires at all.
        """
        cur = await self._conn.execute(
            "SELECT COUNT(*) FROM schedules WHERE enabled=1 AND "
            "last_fire_at IS NOT NULL AND "
            "julianday('now') - julianday(last_fire_at) > ?/86400.0",
            (catchup_window_s,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def top_missed_schedules(self, *, limit: int = 3) -> list[str]:
        cur = await self._conn.execute(
            "SELECT id, cron FROM schedules WHERE enabled=1 AND "
            "last_fire_at IS NOT NULL "
            "ORDER BY last_fire_at ASC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [f"id={r[0]} cron={r[1]!r}" for r in rows]

    # ------------------------------------------------------------------
    # boot classification (H-2)
    # ------------------------------------------------------------------
    async def classify_boot(
        self,
        *,
        marker_path: Path,
        max_age_s: int,
    ) -> BootClass:
        """Classify a boot based on ``.last_clean_exit`` marker state.

        M2.6 refinement: we look at marker **mtime** (falling back to
        the embedded ``ts`` only if mtime is unreadable) rather than
        simply existence, so an ancient leftover marker from months
        ago cannot masquerade as a clean deploy.

        Runs once at daemon boot; the stat+read call is cheap enough
        that hopping a thread pool would cost more than the ~100µs
        blocking hit. ASYNC240 silenced explicitly.
        """
        return await asyncio.to_thread(
            _classify_boot_sync, marker_path, max_age_s
        )

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------
    async def get_schedule_history(
        self, *, schedule_id: int | None, limit: int
    ) -> list[dict[str, Any]]:
        if schedule_id is not None:
            cur = await self._conn.execute(
                "SELECT id, schedule_id, scheduled_for, status, attempts, "
                "last_error, sent_at, acked_at FROM triggers "
                "WHERE schedule_id=? ORDER BY id DESC LIMIT ?",
                (schedule_id, limit),
            )
        else:
            cur = await self._conn.execute(
                "SELECT id, schedule_id, scheduled_for, status, attempts, "
                "last_error, sent_at, acked_at FROM triggers "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "schedule_id": r[1],
                "scheduled_for": r[2],
                "status": r[3],
                "attempts": r[4],
                "last_error": r[5],
                "sent_at": r[6],
                "acked_at": r[7],
            }
            for r in rows
        ]


def _classify_boot_sync(marker_path: Path, max_age_s: int) -> BootClass:
    """Synchronous impl of :meth:`SchedulerStore.classify_boot`.

    Split out so the async method can run us via ``asyncio.to_thread``
    without tripping ASYNC240. Private — call through the method.
    """
    if not marker_path.is_file():
        return "first-boot"
    try:
        mtime = marker_path.stat().st_mtime
        age = dt.datetime.now(dt.UTC).timestamp() - mtime
    except OSError:
        try:
            raw = marker_path.read_text(encoding="utf-8")
            obj = json.loads(raw)
            ts = _parse_iso_z(str(obj["ts"]))
            age = (dt.datetime.now(dt.UTC) - ts).total_seconds()
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return "first-boot"
    if age <= max_age_s:
        return "clean-deploy"
    return "suspend-or-crash"


def unlink_clean_exit_marker(marker: Path) -> None:
    """M2.7: called after boot classification so a later restart that
    happens >120s after a clean stop is still correctly classified as
    ``suspend-or-crash``. Best-effort; logs on failure.
    """
    try:
        marker.unlink(missing_ok=True)
    except OSError as exc:
        _log.warning(
            "clean_exit_marker_unlink_failed",
            path=str(marker),
            error=repr(exc),
        )


def write_clean_exit_marker(marker: Path) -> None:
    """Atomic tmp+rename write. Called from :meth:`Daemon.stop`.

    Fix 15 / DevOps §2: after rename, chmod to 0o600 so the marker
    matches the rest of ``<data_dir>`` (audit log, vault files). The
    marker body is short (``ts`` + ``pid``) and carries no secret, but
    ``<data_dir>`` convention is owner-only.
    """
    payload = {
        "ts": _iso_z(dt.datetime.now(dt.UTC)),
        "pid": os.getpid(),
    }
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_suffix(marker.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(str(tmp), str(marker))
    try:
        os.chmod(marker, 0o600)
    except OSError as exc:
        # Best-effort: a filesystem that rejects chmod (rare) shouldn't
        # fail the shutdown path. The marker still serves its purpose.
        _log.warning(
            "clean_exit_marker_chmod_failed",
            path=str(marker),
            error=repr(exc),
        )
