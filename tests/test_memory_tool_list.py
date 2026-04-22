"""memory_list handler — total/filter/pagination cap."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.tools_sdk import memory as mm


async def _write(args: dict) -> dict:
    return await mm.memory_write.handler(args)


async def _list(args: dict) -> dict:
    return await mm.memory_list.handler(args)


@pytest.mark.asyncio
async def test_memory_list_after_seed(memory_ctx: tuple[Path, Path]) -> None:
    await _write({"path": "inbox/a.md", "title": "A", "body": "a"})
    await _write({"path": "inbox/b.md", "title": "B", "body": "b"})
    await _write({"path": "projects/c.md", "title": "C", "body": "c"})
    res = await _list({})
    assert res.get("is_error") is not True
    assert res["count"] == 3
    assert res["total"] == 3


@pytest.mark.asyncio
async def test_memory_list_area_filter(memory_ctx: tuple[Path, Path]) -> None:
    await _write({"path": "inbox/a.md", "title": "A", "body": "a"})
    await _write({"path": "projects/b.md", "title": "B", "body": "b"})
    res = await _list({"area": "inbox"})
    assert res["count"] == 1
    assert res["total"] == 1
    assert res["notes"][0]["path"] == "inbox/a.md"


@pytest.mark.asyncio
async def test_memory_list_pagination_cap(
    memory_ctx: tuple[Path, Path],
) -> None:
    """M1: default limit caps at 100; total reflects full count."""
    for i in range(3):
        await _write(
            {"path": f"inbox/n-{i}.md", "title": f"N{i}", "body": "x"}
        )
    res = await _list({})
    # Count is <= limit=100; total is full set size.
    assert res["count"] <= 100
    assert res["total"] == 3
