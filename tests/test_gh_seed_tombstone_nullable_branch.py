"""Phase 8 SF-D5 — `rm` on user-created rows does NOT insert tombstones.

User-created schedules (``tools/schedule/main.py add ...``) have
``seed_key IS NULL``. Removing them must preserve the phase-5 soft-delete
semantics and leave the ``seed_tombstones`` table completely untouched —
tombstones are an operator-intent signal for Daemon-managed seeds only.

This covers the ``if seed_key:`` branch in ``cmd_rm`` (falsy for
``None`` and empty string, though empty string shouldn't occur because
the seed helper always writes a non-empty key).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from assistant.scheduler.store import SchedulerStore
from assistant.state.db import apply_schema, connect

_CLI = Path(__file__).resolve().parents[1] / "tools" / "schedule" / "main.py"


def _run(
    data_dir: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "ASSISTANT_DATA_DIR": str(data_dir)}
    return subprocess.run(
        [sys.executable, str(_CLI), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


async def _fresh_store(data_dir: Path) -> SchedulerStore:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = await connect(data_dir / "assistant.db")
    await apply_schema(conn)
    return SchedulerStore(conn, asyncio.Lock())


async def test_rm_user_row_does_not_tombstone(tmp_path: Path) -> None:
    # 1. Insert a user-created row via the async store — default
    #    `seed_key=None` matches what `cmd_add` writes today.
    store = await _fresh_store(tmp_path)
    try:
        sid = await store.insert_schedule(
            cron="0 9 * * *",
            prompt="user-created reminder",
            tz="UTC",
        )
        assert sid > 0

        # Sanity: column is NULL, nothing in seed_tombstones yet.
        async with store._conn.execute(
            "SELECT seed_key FROM schedules WHERE id=?", (sid,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] is None

        async with store._conn.execute(
            "SELECT COUNT(*) FROM seed_tombstones"
        ) as cur:
            count_row = await cur.fetchone()
        assert count_row is not None
        assert count_row[0] == 0
    finally:
        await store._conn.close()

    # 2. `rm` via subprocess.
    r = _run(tmp_path, "rm", str(sid))
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["data"]["id"] == sid
    assert payload["data"]["deleted"] is True
    # Critical: the `tombstoned_seed_key` field MUST be absent for
    # user-created rows. We assert absence rather than `None` so a
    # future bug that adds the key with value `null` also fails.
    assert "tombstoned_seed_key" not in payload["data"], (
        f"user-created row must not tombstone; got {payload['data']!r}"
    )

    # 3. seed_tombstones still empty; row still present (soft-deleted).
    store = await _fresh_store(tmp_path)
    try:
        async with store._conn.execute(
            "SELECT COUNT(*) FROM seed_tombstones"
        ) as cur:
            count_row = await cur.fetchone()
        assert count_row is not None
        assert count_row[0] == 0, (
            "seed_tombstones must be untouched when rm'ing user rows"
        )

        async with store._conn.execute(
            "SELECT enabled FROM schedules WHERE id=?", (sid,)
        ) as cur:
            en_row = await cur.fetchone()
        assert en_row is not None, "row must remain (soft-delete, not hard)"
        assert en_row[0] == 0
    finally:
        await store._conn.close()
