"""Phase 8 — ``ensure_vault_auto_commit_seed`` gating paths.

All three GitHubSettings-level guards must short-circuit BEFORE the
``BEGIN IMMEDIATE`` critical section (v2 B-B1 — so we don't hold a
write lock while deciding we should do nothing):

    1. ``auto_commit_enabled == False`` → skip, no row, no warning.
    2. ``vault_remote_url == ""`` → skip, no row, log ``warning``
       (in practice the ``model_validator`` flips ``auto_commit_enabled``
       off when URL is empty; we must still exercise the raw guard).
    3. ``vault_ssh_key_path`` missing on disk → skip, no row, log
       ``warning``.

In every path the ``schedules`` table remains empty (count=0) and the
function returns ``None``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from assistant.config import GitHubSettings
from assistant.scheduler.seed import (
    SEED_KEY_VAULT_AUTO_COMMIT,
    ensure_vault_auto_commit_seed,
)
from assistant.scheduler.store import SchedulerStore
from assistant.state.db import apply_schema, connect


async def _fresh_store(tmp_path: Path) -> SchedulerStore:
    conn = await connect(tmp_path / "sched.db")
    await apply_schema(conn)
    return SchedulerStore(conn, asyncio.Lock())


async def _count_seed_rows(store: SchedulerStore) -> int:
    async with store._conn.execute(
        "SELECT COUNT(*) FROM schedules WHERE seed_key=?",
        (SEED_KEY_VAULT_AUTO_COMMIT,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_disabled_flag_skips_seed(tmp_path: Path) -> None:
    key_path = tmp_path / "id_vault"
    key_path.write_text("stub\n", encoding="utf-8")
    gh = GitHubSettings(
        vault_remote_url="git@github.com:acme/vault.git",
        vault_ssh_key_path=key_path,
        auto_commit_enabled=False,  # explicit off
    )
    store = await _fresh_store(tmp_path)
    try:
        sid = await ensure_vault_auto_commit_seed(store, gh)
        assert sid is None
        assert await _count_seed_rows(store) == 0
    finally:
        await store._conn.close()


async def test_empty_remote_url_skips_seed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Empty URL path: model_validator auto-disables the flag, so the
    first guard (``auto_commit_enabled``) fires. We still assert
    no row exists — this is the behaviour the Daemon relies on when a
    freshly-installed box has not configured a remote yet."""
    key_path = tmp_path / "id_vault"
    key_path.write_text("stub\n", encoding="utf-8")
    gh = GitHubSettings(
        vault_remote_url="",  # → auto_commit_enabled flipped to False
        vault_ssh_key_path=key_path,
        auto_commit_enabled=True,  # will be overridden by model_validator
    )
    # Sanity: model_validator flipped the flag.
    assert gh.auto_commit_enabled is False

    store = await _fresh_store(tmp_path)
    try:
        with caplog.at_level(logging.INFO, logger="scheduler.seed"):
            sid = await ensure_vault_auto_commit_seed(store, gh)
        assert sid is None
        assert await _count_seed_rows(store) == 0
    finally:
        await store._conn.close()


async def test_missing_ssh_key_skips_seed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing_key = tmp_path / "no_such_file"
    # Deliberately do NOT create the key file.
    gh = GitHubSettings(
        vault_remote_url="git@github.com:acme/vault.git",
        vault_ssh_key_path=missing_key,
        auto_commit_enabled=True,
    )
    assert gh.auto_commit_enabled is True
    assert not gh.vault_ssh_key_path.is_file()

    store = await _fresh_store(tmp_path)
    try:
        with caplog.at_level(logging.WARNING, logger="scheduler.seed"):
            sid = await ensure_vault_auto_commit_seed(store, gh)
        assert sid is None
        assert await _count_seed_rows(store) == 0
    finally:
        await store._conn.close()
