"""Phase 5 / commit 1 — DB migration v3 (scheduler + triggers ledger).

Verifies:
  * `PRAGMA user_version = 3` after `apply_schema`.
  * `schedules` and `triggers` tables exist with the documented columns.
  * `UNIQUE(schedule_id, scheduled_for)` rejects the second INSERT of the
    same minute boundary (idempotent producer contract).
  * `REFERENCES schedules(id) ON DELETE CASCADE` drops trigger rows when
    the parent schedule is hard-deleted.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from assistant.state.db import apply_schema, connect


async def _column_names(conn: aiosqlite.Connection, table: str) -> list[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return [r[1] for r in rows]


async def test_v3_bumps_user_version(tmp_path: Path) -> None:
    """After phase 6 schema bump, `apply_schema` continues past v3 to the
    current SCHEMA_VERSION. We only need to prove v3 ran (tables exist);
    the final `user_version` reflects the current max, not 3."""
    conn = await connect(tmp_path / "m3.db")
    try:
        await apply_schema(conn)
        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] >= 3
    finally:
        await conn.close()


async def test_v3_tables_exist_with_expected_columns(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "m3.db")
    try:
        await apply_schema(conn)
        sched_cols = await _column_names(conn, "schedules")
        trig_cols = await _column_names(conn, "triggers")
    finally:
        await conn.close()

    for col in ("id", "cron", "prompt", "tz", "enabled", "created_at", "last_fire_at"):
        assert col in sched_cols, f"schedules missing {col!r} (have: {sched_cols})"
    for col in (
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
    ):
        assert col in trig_cols, f"triggers missing {col!r} (have: {trig_cols})"


async def test_v3_unique_schedule_scheduled_for(tmp_path: Path) -> None:
    """Second INSERT for the same (schedule_id, scheduled_for) must fail —
    this is the contract `try_materialize_trigger` relies on (plan §5.3)."""
    conn = await connect(tmp_path / "m3.db")
    try:
        await apply_schema(conn)
        await conn.execute(
            "INSERT INTO schedules(cron, prompt, tz) VALUES (?, ?, ?)",
            ("0 9 * * *", "ping", "UTC"),
        )
        await conn.commit()
        async with conn.execute("SELECT id FROM schedules") as cur:
            (sid,) = (await cur.fetchone()) or (None,)
        assert sid is not None

        await conn.execute(
            "INSERT INTO triggers(schedule_id, prompt, scheduled_for) VALUES (?, ?, ?)",
            (sid, "ping", "2026-04-15T09:00:00Z"),
        )
        await conn.commit()

        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO triggers(schedule_id, prompt, scheduled_for) VALUES (?, ?, ?)",
                (sid, "ping", "2026-04-15T09:00:00Z"),
            )
            await conn.commit()
    finally:
        await conn.close()


async def test_v3_cascade_delete_drops_triggers(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "m3.db")
    try:
        await apply_schema(conn)
        await conn.execute(
            "INSERT INTO schedules(cron, prompt) VALUES (?, ?)",
            ("0 9 * * *", "ping"),
        )
        await conn.commit()
        async with conn.execute("SELECT id FROM schedules") as cur:
            (sid,) = (await cur.fetchone()) or (None,)
        assert sid is not None

        for minute in ("2026-04-15T09:00:00Z", "2026-04-16T09:00:00Z"):
            await conn.execute(
                "INSERT INTO triggers(schedule_id, prompt, scheduled_for) VALUES (?, ?, ?)",
                (sid, "ping", minute),
            )
        await conn.commit()

        async with conn.execute("SELECT COUNT(*) FROM triggers WHERE schedule_id=?", (sid,)) as cur:
            (n_before,) = (await cur.fetchone()) or (0,)
        assert n_before == 2

        await conn.execute("DELETE FROM schedules WHERE id=?", (sid,))
        await conn.commit()

        async with conn.execute("SELECT COUNT(*) FROM triggers WHERE schedule_id=?", (sid,)) as cur:
            (n_after,) = (await cur.fetchone()) or (0,)
        assert n_after == 0
    finally:
        await conn.close()


async def test_v3_apply_is_idempotent(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "m3.db")
    try:
        await apply_schema(conn)
        await apply_schema(conn)
        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        assert row is not None
        # Idempotent across all migrations up to the current SCHEMA_VERSION.
        assert row[0] >= 3
    finally:
        await conn.close()
