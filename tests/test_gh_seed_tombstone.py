"""Phase 8 — `tools/schedule/main.py rm <id>` tombstones seeded rows.

End-to-end via subprocess:

    1. Insert a ``vault_auto_commit`` seed row via
       ``SchedulerStore.insert_schedule(seed_key=...)``.
    2. ``python tools/schedule/main.py rm <id>`` returns JSON with
       ``tombstoned_seed_key == "vault_auto_commit"``; the ``schedules``
       row is soft-deleted (``enabled=0``), NOT hard-deleted.
    3. ``seed_tombstones`` now carries one row for ``vault_auto_commit``.
    4. ``ensure_vault_auto_commit_seed`` is a no-op: tombstone blocks
       re-seed (returns ``None``).
    5. ``python tools/schedule/main.py revive-seed vault_auto_commit``
       returns JSON with ``tombstone_removed == True`` and the
       ``seed_tombstones`` row is gone.
    6. After revive: the soft-deleted row is still there, so
       ``ensure_vault_auto_commit_seed`` skips with ``action=="exists"``
       (it does NOT resurrect the row). Count of seed rows stays at 1.
       This is the documented behaviour — operator must explicitly
       ``schedule enable <id>`` (or delete the row outright outside the
       CLI and let the Daemon re-insert) to get a fresh seed.

The subprocess invocations exercise the full sync-sqlite3 path in
``cmd_rm`` / ``cmd_revive_seed`` — same shape as
``tests/test_schedule_cli.py`` (B-D1).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from assistant.config import GitHubSettings
from assistant.scheduler.seed import (
    SEED_KEY_VAULT_AUTO_COMMIT,
    ensure_vault_auto_commit_seed,
)
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


def _make_gh(ssh_key: Path) -> GitHubSettings:
    return GitHubSettings(
        vault_remote_url="git@github.com:acme/vault.git",
        vault_ssh_key_path=ssh_key,
        auto_commit_enabled=True,
        auto_commit_cron="0 3 * * *",
        auto_commit_tz="Europe/Moscow",
    )


async def _count_tombstones(store: SchedulerStore, seed_key: str) -> int:
    async with store._conn.execute(
        "SELECT COUNT(*) FROM seed_tombstones WHERE seed_key=?",
        (seed_key,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _enabled_flag(store: SchedulerStore, schedule_id: int) -> int:
    async with store._conn.execute(
        "SELECT enabled FROM schedules WHERE id=?", (schedule_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None, "row must still exist after soft-delete"
    return int(row[0])


async def _count_rows_by_seed(store: SchedulerStore, seed_key: str) -> int:
    async with store._conn.execute(
        "SELECT COUNT(*) FROM schedules WHERE seed_key=?", (seed_key,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_rm_seeded_row_inserts_tombstone_and_revive_removes_it(
    tmp_path: Path,
) -> None:
    key_path = tmp_path / "id_vault"
    key_path.write_text("stub ssh key\n", encoding="utf-8")
    gh = _make_gh(ssh_key=key_path)

    # 1. Seed the row through the same helper the Daemon uses.
    store = await _fresh_store(tmp_path)
    try:
        sid = await ensure_vault_auto_commit_seed(store, gh)
        assert sid is not None and sid > 0
        # Sanity: seed_tombstones empty, row is enabled.
        assert await _count_tombstones(store, SEED_KEY_VAULT_AUTO_COMMIT) == 0
        assert await _enabled_flag(store, sid) == 1
    finally:
        await store._conn.close()

    # 2. `rm` via subprocess — writes to the same DB file.
    r = _run(tmp_path, "rm", str(sid))
    assert r.returncode == 0, r.stderr
    rm_payload = json.loads(r.stdout)
    assert rm_payload["ok"] is True
    assert rm_payload["data"]["id"] == sid
    assert rm_payload["data"]["deleted"] is True
    assert (
        rm_payload["data"]["tombstoned_seed_key"]
        == SEED_KEY_VAULT_AUTO_COMMIT
    )

    # 3. DB state: row soft-deleted (enabled=0, still present),
    #    tombstone present.
    store = await _fresh_store(tmp_path)
    try:
        assert await _enabled_flag(store, sid) == 0
        assert await _count_rows_by_seed(store, SEED_KEY_VAULT_AUTO_COMMIT) == 1
        assert await _count_tombstones(store, SEED_KEY_VAULT_AUTO_COMMIT) == 1

        # 4. Re-calling the seed helper: blocked by tombstone → None, no
        #    new row inserted.
        sid2 = await ensure_vault_auto_commit_seed(store, gh)
        assert sid2 is None
        assert await _count_rows_by_seed(store, SEED_KEY_VAULT_AUTO_COMMIT) == 1
    finally:
        await store._conn.close()

    # 5. `revive-seed` via subprocess.
    r = _run(tmp_path, "revive-seed", SEED_KEY_VAULT_AUTO_COMMIT)
    assert r.returncode == 0, r.stderr
    revive_payload = json.loads(r.stdout)
    assert revive_payload["ok"] is True
    assert (
        revive_payload["data"]["seed_key"] == SEED_KEY_VAULT_AUTO_COMMIT
    )
    assert revive_payload["data"]["tombstone_removed"] is True

    # 6. Tombstone gone. The soft-deleted row is still there, so the
    #    helper now returns the existing row id (action="exists") and
    #    does NOT insert a duplicate — honouring the partial UNIQUE
    #    INDEX ``idx_schedules_seed_key``.
    store = await _fresh_store(tmp_path)
    try:
        assert await _count_tombstones(store, SEED_KEY_VAULT_AUTO_COMMIT) == 0
        sid3 = await ensure_vault_auto_commit_seed(store, gh)
        assert sid3 == sid, (
            "after revive the helper finds the existing (soft-deleted) "
            "row via seed_key — no new row inserted"
        )
        assert await _count_rows_by_seed(store, SEED_KEY_VAULT_AUTO_COMMIT) == 1
    finally:
        await store._conn.close()


async def test_revive_seed_is_noop_when_no_tombstone(tmp_path: Path) -> None:
    """``revive-seed`` on a fresh DB with no tombstones returns
    ``tombstone_removed=False`` (rc=0) — idempotent / safe for
    tab-complete typos."""
    store = await _fresh_store(tmp_path)
    try:
        assert await _count_tombstones(store, SEED_KEY_VAULT_AUTO_COMMIT) == 0
    finally:
        await store._conn.close()

    r = _run(tmp_path, "revive-seed", SEED_KEY_VAULT_AUTO_COMMIT)
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["data"]["seed_key"] == SEED_KEY_VAULT_AUTO_COMMIT
    assert payload["data"]["tombstone_removed"] is False
