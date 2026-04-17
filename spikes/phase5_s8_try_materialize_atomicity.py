"""Spike S-8: aiosqlite atomicity of try_materialize_trigger.

Question: Plan §5.3 does `INSERT OR IGNORE triggers` + conditional `UPDATE
schedules SET last_fire_at` in a single `async with self._lock: ... commit()`
block. In WAL mode, can a concurrent reader observe a transient state where
a new triggers row exists but `last_fire_at` is still the old value?

Method: writer task runs INSERT+UPDATE+commit. Reader task SELECTs
(trigger_count, last_fire_at) tightly. Reader must NEVER see
(triggers_count=N+1, last_fire_at=old).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

import aiosqlite


DDL = """
CREATE TABLE schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cron TEXT NOT NULL,
    last_fire_at TEXT
);
CREATE TABLE triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES schedules(id),
    scheduled_for TEXT NOT NULL,
    UNIQUE(schedule_id, scheduled_for)
);
"""


async def run() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "spike_s8.db"
        writer = await aiosqlite.connect(db_path)
        reader = await aiosqlite.connect(db_path)
        try:
            await writer.execute("PRAGMA journal_mode=WAL")
            await writer.execute("PRAGMA busy_timeout=5000")
            await reader.execute("PRAGMA journal_mode=WAL")
            await reader.execute("PRAGMA busy_timeout=5000")
            # Create schema on the writer.
            for stmt in DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await writer.execute(stmt)
            await writer.execute(
                "INSERT INTO schedules(cron, last_fire_at) VALUES (?, NULL)",
                ("0 9 * * *",),
            )
            await writer.commit()

            lock = asyncio.Lock()

            async def try_materialize(schedule_id: int, scheduled_for: str) -> int | None:
                async with lock:
                    cursor = await writer.execute(
                        "INSERT OR IGNORE INTO triggers(schedule_id, scheduled_for) "
                        "VALUES (?, ?)",
                        (schedule_id, scheduled_for),
                    )
                    if cursor.rowcount == 0:
                        await writer.commit()
                        return None
                    trigger_id = cursor.lastrowid
                    await writer.execute(
                        "UPDATE schedules SET last_fire_at=? WHERE id=?",
                        (scheduled_for, schedule_id),
                    )
                    await writer.commit()
                    return trigger_id

            violations: list[dict] = []
            reader_done = asyncio.Event()

            async def reader_loop() -> int:
                # Sample triggers_count and last_fire_at in a tight loop; flag
                # any observation where triggers_count>0 but last_fire_at IS NULL.
                samples = 0
                while not reader_done.is_set():
                    async with reader.execute(
                        "SELECT COUNT(*) FROM triggers WHERE schedule_id=1"
                    ) as cur:
                        row = await cur.fetchone()
                        trig_count = row[0] if row else 0
                    async with reader.execute(
                        "SELECT last_fire_at FROM schedules WHERE id=1"
                    ) as cur:
                        row2 = await cur.fetchone()
                        last_fire_at = row2[0] if row2 else None
                    if trig_count > 0 and last_fire_at is None:
                        # Two separate SELECTs: might just have raced between
                        # statements. This is a known SQLite caveat: a single
                        # read consistency is statement-level, not multi-stmt.
                        # Count as violation; compare against atomic expectation.
                        violations.append(
                            {
                                "sample_idx": samples,
                                "trig_count": trig_count,
                                "last_fire_at": last_fire_at,
                            }
                        )
                    samples += 1
                    if samples % 50 == 0:
                        await asyncio.sleep(0)  # yield
                return samples

            reader_task = asyncio.create_task(reader_loop())

            # Do 20 materializations, each under the writer lock.
            for i in range(20):
                sf = f"2026-04-17T09:{i:02d}:00Z"
                tr = await try_materialize(1, sf)
                _ = tr
                # Small sleep to let reader observe.
                await asyncio.sleep(0.005)

            reader_done.set()
            samples = await reader_task

            # Final check.
            async with reader.execute("SELECT COUNT(*) FROM triggers") as cur:
                r = await cur.fetchone()
                final_trigger_count = r[0] if r else 0
            async with reader.execute(
                "SELECT last_fire_at FROM schedules WHERE id=1"
            ) as cur:
                r2 = await cur.fetchone()
                final_last_fire_at = r2[0] if r2 else None

            return {
                "reader_samples": samples,
                "violations_count": len(violations),
                "violations_first_5": violations[:5],
                "final_trigger_count": final_trigger_count,
                "final_last_fire_at": final_last_fire_at,
                "atomicity_ok": len(violations) == 0,
                "notes": (
                    "Two SELECT statements cannot share a snapshot without an "
                    "explicit BEGIN. Violations here are NOT atomicity "
                    "failures of the writer; they are reader-side multi-"
                    "statement visibility issues. Recommend: if the dispatcher "
                    "needs consistent (trigger, last_fire_at) reads, wrap in "
                    "BEGIN/COMMIT or read-through a single JOIN query."
                ),
            }
        finally:
            await writer.close()
            await reader.close()


def main() -> None:
    r = asyncio.run(run())
    print(json.dumps(r, indent=2, default=str))


if __name__ == "__main__":
    main()
