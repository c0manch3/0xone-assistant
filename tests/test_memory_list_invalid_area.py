"""Fix 9 / QA M1 — ``memory_list`` rejects area names that don't match
the top-level path-segment regex.

``list_notes`` happily accepts any string and returns an empty result
set when it doesn't match any area — leaving the model with no signal
that ``INVALID!`` or ``../escape`` was the actual problem. Up-front
regex validation returns a specific error code (``CODE_AREA``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.tools_sdk import memory as mm


async def _list(args: dict) -> dict:
    return await mm.memory_list.handler(args)


@pytest.mark.asyncio
async def test_memory_list_rejects_uppercase(
    memory_ctx: tuple[Path, Path],
) -> None:
    res = await _list({"area": "INVALID!"})
    assert res.get("is_error") is True
    assert "(code=7)" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_memory_list_rejects_traversal(
    memory_ctx: tuple[Path, Path],
) -> None:
    res = await _list({"area": "../etc"})
    assert res.get("is_error") is True
    assert "(code=7)" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_memory_list_rejects_whitespace(
    memory_ctx: tuple[Path, Path],
) -> None:
    res = await _list({"area": "has space"})
    assert res.get("is_error") is True
    assert "(code=7)" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_memory_list_accepts_valid_area(
    memory_ctx: tuple[Path, Path],
) -> None:
    async def _write(args: dict) -> dict:
        return await mm.memory_write.handler(args)

    await _write({"path": "inbox/a.md", "title": "A", "body": "b"})
    res = await _list({"area": "inbox"})
    assert res.get("is_error") is not True
    assert res["count"] == 1
