"""Phase 8 — ``ensure_vault_auto_commit_seed`` is idempotent across calls.

Covers:
    * First call inserts exactly one row with ``seed_key='vault_auto_commit'``.
    * Second call is a no-op (returns the same schedule_id, no new row).
    * ``PRAGMA user_version`` is 6 after ``apply_schema`` (migrations
      0005+0006 ran and bumped the schema version).

The partial UNIQUE INDEX is the defence-in-depth last barrier here:
the pre-INSERT ``find_by_seed_key`` inside ``ensure_seed_row`` already
covers the happy path; the UNIQUE INDEX catches the hypothetical case
of two racing Daemons bypassing the flock.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from assistant.config import GitHubSettings
from assistant.scheduler.seed import (
    SEED_KEY_VAULT_AUTO_COMMIT,
    ensure_vault_auto_commit_seed,
)
from assistant.scheduler.store import SchedulerStore
from assistant.state.db import apply_schema, connect


def _make_gh_settings(*, ssh_key: Path) -> GitHubSettings:
    """Build a valid ``GitHubSettings`` with a real on-disk key file.

    The model-validator flips ``auto_commit_enabled`` off when the URL
    is empty, so we supply a real-looking SSH URL.
    """
    return GitHubSettings(
        vault_remote_url="git@github.com:acme/vault.git",
        vault_ssh_key_path=ssh_key,
        auto_commit_enabled=True,
        auto_commit_cron="0 3 * * *",
        auto_commit_tz="Europe/Moscow",
    )


async def _fresh_store(tmp_path: Path) -> SchedulerStore:
    conn = await connect(tmp_path / "sched.db")
    await apply_schema(conn)
    return SchedulerStore(conn, asyncio.Lock())


async def test_schema_version_is_6_after_apply(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "v.db")
    try:
        await apply_schema(conn)
        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 6, f"expected user_version=6, got {row[0]}"
    finally:
        await conn.close()


async def test_seed_inserts_row_on_first_call(tmp_path: Path) -> None:
    key_path = tmp_path / "id_vault"
    key_path.write_text("stub ssh key\n", encoding="utf-8")
    gh = _make_gh_settings(ssh_key=key_path)

    store = await _fresh_store(tmp_path)
    try:
        sid = await ensure_vault_auto_commit_seed(store, gh)
        assert sid is not None and sid > 0

        async with store._conn.execute(
            "SELECT id, cron, prompt, tz, enabled, seed_key "
            "FROM schedules WHERE seed_key=?",
            (SEED_KEY_VAULT_AUTO_COMMIT,),
        ) as cur:
            rows = await cur.fetchall()
        assert len(rows) == 1
        (row_id, cron, prompt, tz, enabled, seed_key) = rows[0]
        assert row_id == sid
        assert cron == gh.auto_commit_cron
        assert tz == gh.auto_commit_tz
        assert enabled == 1
        assert seed_key == SEED_KEY_VAULT_AUTO_COMMIT
        assert "vault" in prompt.lower()
    finally:
        await store._conn.close()


async def test_seed_second_call_is_no_op(tmp_path: Path) -> None:
    key_path = tmp_path / "id_vault"
    key_path.write_text("stub ssh key\n", encoding="utf-8")
    gh = _make_gh_settings(ssh_key=key_path)

    store = await _fresh_store(tmp_path)
    try:
        sid1 = await ensure_vault_auto_commit_seed(store, gh)
        sid2 = await ensure_vault_auto_commit_seed(store, gh)
        assert sid1 is not None
        assert sid2 == sid1, (
            "second call must return the existing row id, not create a new one"
        )

        async with store._conn.execute(
            "SELECT COUNT(*) FROM schedules WHERE seed_key=?",
            (SEED_KEY_VAULT_AUTO_COMMIT,),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1, "second call must not insert a duplicate row"
    finally:
        await store._conn.close()


async def test_seed_tombstone_blocks_re_seed(tmp_path: Path) -> None:
    """If the owner tombstoned the seed, ``ensure_vault_auto_commit_seed``
    returns ``None`` and inserts no row — respects Q10 intent."""
    key_path = tmp_path / "id_vault"
    key_path.write_text("stub ssh key\n", encoding="utf-8")
    gh = _make_gh_settings(ssh_key=key_path)

    store = await _fresh_store(tmp_path)
    try:
        # Simulate owner intent: tombstone exists before first Daemon boot.
        await store.insert_tombstone(SEED_KEY_VAULT_AUTO_COMMIT)

        sid = await ensure_vault_auto_commit_seed(store, gh)
        assert sid is None

        async with store._conn.execute(
            "SELECT COUNT(*) FROM schedules WHERE seed_key=?",
            (SEED_KEY_VAULT_AUTO_COMMIT,),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0, "tombstone must block the insert"
    finally:
        await store._conn.close()
