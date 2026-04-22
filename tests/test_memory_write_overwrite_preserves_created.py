"""Fix 4 / H3 — overwrite preserves the existing note's ``created``.

Previously every overwrite replaced both ``created`` and ``updated``
with ``now`` — Obsidian's sort-by-created metadata was corrupted on
every edit, and the "date a note first appeared" was lost forever.
Overwrite now parses the on-disk frontmatter to recover the original
``created`` and only stamps a fresh ``updated``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assistant.tools_sdk import _memory_core as core
from assistant.tools_sdk import memory as mm


async def _write(args: dict) -> dict:
    return await mm.memory_write.handler(args)


@pytest.mark.asyncio
async def test_memory_write_overwrite_preserves_created(
    memory_ctx: tuple[Path, Path],
) -> None:
    vault, _idx = memory_ctx
    res = await _write({"path": "inbox/o.md", "title": "O1", "body": "one"})
    assert res.get("is_error") is not True
    note = vault / "inbox" / "o.md"
    fm_before, _ = core.parse_frontmatter(note.read_text(encoding="utf-8"))
    created_before = str(fm_before["created"])
    updated_before = str(fm_before["updated"])

    # Small sleep so ``updated`` will differ between writes.
    await asyncio.sleep(0.05)
    res2 = await _write(
        {
            "path": "inbox/o.md",
            "title": "O2",
            "body": "two",
            "overwrite": True,
        }
    )
    assert res2.get("is_error") is not True
    fm_after, body = core.parse_frontmatter(note.read_text(encoding="utf-8"))
    assert "two" in body
    # Original ``created`` preserved byte-for-byte.
    assert str(fm_after["created"]) == created_before, (
        f"expected preserved {created_before!r}, got {fm_after.get('created')!r}"
    )
    # ``updated`` MUST advance.
    assert str(fm_after["updated"]) != updated_before, (
        "updated timestamp did not change on overwrite"
    )
