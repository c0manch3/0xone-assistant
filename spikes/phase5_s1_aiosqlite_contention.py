"""Spike S-1: aiosqlite contention under scheduler+handler load.

Question: Phase-5 plan §1.11 places scheduler and handler on the same
aiosqlite connection, serialised via a single asyncio.Lock. Can one
connection+lock absorb INSERT-triggers (every 15ms, simulating compressed
cadence) concurrently with INSERT-conversations (every 50ms, bigger blob)
without pathological latency?

Pass criterion: p99 < 100ms for 200 concurrent inserts.

Method: one aiosqlite connection, one asyncio.Lock, two coroutines A and B.
A inserts into `triggers_stub` every 15ms (100 rows).
B inserts into `conversations_stub` every 50ms with a ~4KB blob (100 rows).
Measure per-insert wall-clock from acquire-lock → commit. Print p50/p95/p99.

Run:
    uv run python spikes/phase5_s1_aiosqlite_contention.py
"""

from __future__ import annotations

import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

import aiosqlite


async def setup_db(conn: aiosqlite.Connection) -> None:
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS triggers_stub (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id INTEGER NOT NULL,
            scheduled_for TEXT NOT NULL,
            status TEXT NOT NULL
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations_stub (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            blob TEXT NOT NULL
        )
        """
    )
    await conn.commit()


async def producer_triggers(
    conn: aiosqlite.Connection,
    lock: asyncio.Lock,
    latencies: list[float],
    n: int,
    cadence_s: float,
) -> None:
    for i in range(n):
        await asyncio.sleep(cadence_s)
        t0 = time.perf_counter()
        async with lock:
            await conn.execute(
                "INSERT INTO triggers_stub(schedule_id, scheduled_for, status) "
                "VALUES (?, ?, 'pending')",
                (1, f"2026-04-17T09:{i:02d}:00Z"),
            )
            await conn.commit()
        latencies.append((time.perf_counter() - t0) * 1000.0)


async def producer_conversations(
    conn: aiosqlite.Connection,
    lock: asyncio.Lock,
    latencies: list[float],
    n: int,
    cadence_s: float,
    blob_bytes: int,
) -> None:
    blob = "x" * blob_bytes
    for i in range(n):
        await asyncio.sleep(cadence_s)
        t0 = time.perf_counter()
        async with lock:
            await conn.execute(
                "INSERT INTO conversations_stub(chat_id, blob) VALUES (?, ?)",
                (42, blob),
            )
            await conn.commit()
        latencies.append((time.perf_counter() - t0) * 1000.0)


def _percentiles(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    s = sorted(xs)
    n = len(s)
    return {
        "count": n,
        "mean_ms": statistics.mean(s),
        "p50_ms": s[int(n * 0.5)],
        "p95_ms": s[min(int(n * 0.95), n - 1)],
        "p99_ms": s[min(int(n * 0.99), n - 1)],
        "max_ms": s[-1],
    }


async def run() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "spike_s1.db"
        conn = await aiosqlite.connect(db_path)
        try:
            await setup_db(conn)
            lock = asyncio.Lock()
            lat_tr: list[float] = []
            lat_conv: list[float] = []
            t0 = time.perf_counter()
            await asyncio.gather(
                producer_triggers(conn, lock, lat_tr, n=100, cadence_s=0.015),
                producer_conversations(
                    conn, lock, lat_conv, n=100, cadence_s=0.050, blob_bytes=4096
                ),
            )
            wall_s = time.perf_counter() - t0
            return {
                "wall_seconds": wall_s,
                "triggers": _percentiles(lat_tr),
                "conversations": _percentiles(lat_conv),
                "combined": _percentiles(lat_tr + lat_conv),
                "p99_under_100ms": max(lat_tr + lat_conv) < 100.0,
            }
        finally:
            await conn.close()


def main() -> None:
    result = asyncio.run(run())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
