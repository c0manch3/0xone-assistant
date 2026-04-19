"""Phase 8 — migrations 0005 (seed_key) + 0006 (seed_tombstones).

Covers:
    * Sequential v0 → v6 migration via ``apply_schema``; ``user_version``
      ends at 6.
    * ``schedules`` has the new ``seed_key`` column.
    * ``idx_schedules_seed_key`` exists AND is **partial** (SF-D3): its
      DDL in ``sqlite_master.sql`` must contain
      ``WHERE seed_key IS NOT NULL`` — otherwise a full UNIQUE INDEX
      would reject multiple NULL rows (all user-created schedules carry
      ``seed_key=NULL``).
    * Two rows with ``seed_key=NULL`` coexist fine (the partial index
      ignores them).
    * Two rows with the same non-NULL ``seed_key`` raise ``IntegrityError``.
    * The ``seed_tombstones`` table exists with ``seed_key`` as PRIMARY KEY.
    * ``_apply_v5`` and ``_apply_v6`` survive re-running (idempotent) —
      ``apply_schema`` called twice does not fail.
    * SF-F2 contract: neither migration used ``executescript`` (asserted
      by source inspection so a future refactor can't silently regress).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import aiosqlite
import pytest

from assistant.state import db as db_module
from assistant.state.db import apply_schema, connect


async def _column_names(conn: aiosqlite.Connection, table: str) -> list[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return [r[1] for r in rows]


async def test_apply_schema_reaches_v6(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "m.db")
    try:
        await apply_schema(conn)
        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 6
    finally:
        await conn.close()


async def test_schedules_has_seed_key_column(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "m.db")
    try:
        await apply_schema(conn)
        cols = await _column_names(conn, "schedules")
        assert "seed_key" in cols, (
            f"schedules missing seed_key column (have: {cols})"
        )
    finally:
        await conn.close()


async def test_seed_tombstones_table_shape(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "m.db")
    try:
        await apply_schema(conn)
        cols = await _column_names(conn, "seed_tombstones")
        assert "seed_key" in cols
        assert "deleted_at" in cols

        # Primary key is seed_key.
        async with conn.execute("PRAGMA table_info(seed_tombstones)") as cur:
            rows = await cur.fetchall()
        pk_cols = [r[1] for r in rows if r[5] > 0]  # r[5] = pk position
        assert pk_cols == ["seed_key"], (
            f"expected PK=['seed_key'], got {pk_cols}"
        )
    finally:
        await conn.close()


async def test_idx_schedules_seed_key_is_partial(tmp_path: Path) -> None:
    """SF-D3: assert the index DDL literally contains the
    ``WHERE seed_key IS NOT NULL`` partial clause. Without this, the
    UNIQUE INDEX would reject two NULL-keyed rows (every user-created
    schedule carries ``seed_key=NULL``)."""
    conn = await connect(tmp_path / "m.db")
    try:
        await apply_schema(conn)
        async with conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_schedules_seed_key'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None, "idx_schedules_seed_key was not created"
        sql_text = str(row[0])
        # Normalise quoting before matching; SQLite may render the stored
        # DDL with or without surrounding backticks / double quotes.
        normalised = sql_text.upper().replace("`", "").replace('"', "")
        assert "WHERE SEED_KEY IS NOT NULL" in normalised, (
            f"partial clause missing from index DDL: {sql_text!r}"
        )
    finally:
        await conn.close()


async def test_multiple_null_seed_key_rows_allowed(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "m.db")
    try:
        await apply_schema(conn)
        # Both rows have seed_key NULL — partial index must ignore them.
        await conn.execute(
            "INSERT INTO schedules(cron, prompt, tz, enabled, seed_key) "
            "VALUES (?, ?, ?, 1, NULL)",
            ("0 9 * * *", "a", "UTC"),
        )
        await conn.execute(
            "INSERT INTO schedules(cron, prompt, tz, enabled, seed_key) "
            "VALUES (?, ?, ?, 1, NULL)",
            ("0 10 * * *", "b", "UTC"),
        )
        await conn.commit()

        async with conn.execute(
            "SELECT COUNT(*) FROM schedules WHERE seed_key IS NULL"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 2
    finally:
        await conn.close()


async def test_duplicate_non_null_seed_key_rejected(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "m.db")
    try:
        await apply_schema(conn)
        await conn.execute(
            "INSERT INTO schedules(cron, prompt, tz, enabled, seed_key) "
            "VALUES (?, ?, ?, 1, ?)",
            ("0 3 * * *", "first", "UTC", "vault_auto_commit"),
        )
        await conn.commit()

        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO schedules(cron, prompt, tz, enabled, seed_key) "
                "VALUES (?, ?, ?, 1, ?)",
                ("0 4 * * *", "second", "UTC", "vault_auto_commit"),
            )
            await conn.commit()
    finally:
        await conn.close()


async def test_apply_schema_is_idempotent(tmp_path: Path) -> None:
    """Run ``apply_schema`` twice on a fresh DB — second call must be a
    no-op (no ``duplicate column name: seed_key`` error, no ``table
    seed_tombstones already exists`` error)."""
    conn = await connect(tmp_path / "m.db")
    try:
        await apply_schema(conn)
        await apply_schema(conn)  # must not raise
        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 6
    finally:
        await conn.close()


def test_apply_v5_v6_do_not_use_executescript() -> None:
    """SF-F2: source-level guard. Neither ``_apply_v5`` nor ``_apply_v6``
    may *invoke* ``executescript`` — on aiosqlite it auto-commits each
    statement, silently bypassing ``BEGIN IMMEDIATE`` and defeating
    migration atomicity. A future refactor that tries to "simplify"
    the migrations back into a single script will trip this test.

    We match on ``.executescript(`` (attribute access + call) so the
    rationale-explaining docstring that mentions the name by itself
    does not false-positive.
    """
    forbidden = ".executescript("
    for fn in (db_module._apply_v5, db_module._apply_v6):
        # Strip docstring to scan only executable source.
        src = inspect.getsource(fn)
        doc = fn.__doc__ or ""
        src_without_doc = src.replace(doc, "") if doc else src
        assert forbidden not in src_without_doc, (
            f"{fn.__name__} must not call executescript (SF-F2); "
            f"use explicit conn.execute() statements inside BEGIN IMMEDIATE"
        )
