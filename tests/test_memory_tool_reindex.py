"""memory_reindex handler — disaster recovery."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from assistant.tools_sdk import memory as mm


async def _write(args: dict) -> dict:
    return await mm.memory_write.handler(args)


async def _reindex() -> dict:
    return await mm.memory_reindex.handler({})


@pytest.mark.asyncio
async def test_memory_reindex_disaster_recovery(
    memory_ctx: tuple[Path, Path],
) -> None:
    _vault, idx = memory_ctx
    # Seed 3 notes via the write tool (so index is in sync).
    await _write({"path": "inbox/a.md", "title": "A", "body": "a"})
    await _write({"path": "inbox/b.md", "title": "B", "body": "b"})
    await _write({"path": "projects/c.md", "title": "C", "body": "c"})

    # Corrupt the index by wiping all rows (simulating a bad index).
    conn = sqlite3.connect(idx)
    try:
        conn.execute("DELETE FROM notes")
        conn.commit()
    finally:
        conn.close()

    res = await _reindex()
    assert res.get("is_error") is not True
    assert res["reindexed"] == 3

    conn = sqlite3.connect(idx)
    try:
        total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    finally:
        conn.close()
    assert total == 3
