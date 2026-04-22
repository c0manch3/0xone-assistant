"""memory_delete handler — H2.5 ordering + confirmation gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.tools_sdk import memory as mm


async def _write(args: dict) -> dict:
    return await mm.memory_write.handler(args)


async def _delete(args: dict) -> dict:
    return await mm.memory_delete.handler(args)


@pytest.mark.asyncio
async def test_memory_delete_happy(memory_ctx: tuple[Path, Path]) -> None:
    vault, _idx = memory_ctx
    await _write({"path": "inbox/x.md", "title": "T", "body": "b"})
    res = await _delete({"path": "inbox/x.md", "confirmed": True})
    assert res.get("is_error") is not True
    assert not (vault / "inbox" / "x.md").exists()


@pytest.mark.asyncio
async def test_memory_delete_not_confirmed(
    memory_ctx: tuple[Path, Path],
) -> None:
    await _write({"path": "inbox/x.md", "title": "T", "body": "b"})
    res = await _delete({"path": "inbox/x.md", "confirmed": False})
    assert res.get("is_error") is True
    assert "(code=10)" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_memory_delete_not_found(memory_ctx: tuple[Path, Path]) -> None:
    res = await _delete({"path": "inbox/absent.md", "confirmed": True})
    assert res.get("is_error") is True
    assert "(code=2)" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_memory_delete_bad_path_before_confirmed(
    memory_ctx: tuple[Path, Path],
) -> None:
    """H2.5: path validation FIRST; a bad path + confirmed=false returns
    (code=1), NOT (code=10). The ordering prevents leaking that
    confirmation is the only blocker.
    """
    res = await _delete({"path": "../escape.md", "confirmed": False})
    assert res.get("is_error") is True
    assert "(code=1)" in res["content"][0]["text"]
