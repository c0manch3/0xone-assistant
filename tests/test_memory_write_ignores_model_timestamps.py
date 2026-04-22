"""Fix 3 / H4-W3 — ``memory_write`` MUST NOT read ``created`` / ``updated``
from model-controlled args.

The schema does not advertise those fields; a prior implementation
still looked at ``args.get("created")``, so a model that decided to
pin ``updated: "9999-99-99"`` could always-sort-first in
``memory_list``. The handler now stamps both fields server-side.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from assistant.tools_sdk import _memory_core as core
from assistant.tools_sdk import memory as mm


async def _write(args: dict) -> dict:
    return await mm.memory_write.handler(args)


@pytest.mark.asyncio
async def test_memory_write_ignores_model_created(
    memory_ctx: tuple[Path, Path],
) -> None:
    vault, _idx = memory_ctx
    # Pass a 1999 ``created`` in args — the handler must ignore it and
    # stamp the current year instead.
    res = await _write(
        {
            "path": "inbox/ts.md",
            "title": "Timestamp test",
            "body": "body",
            "created": "1999-01-01T00:00:00+00:00",
        }
    )
    assert res.get("is_error") is not True
    note = vault / "inbox" / "ts.md"
    assert note.is_file()
    text = note.read_text(encoding="utf-8")
    fm, _ = core.parse_frontmatter(text)
    now_year = str(dt.datetime.now(dt.UTC).year)
    created_field = str(fm.get("created", ""))
    updated_field = str(fm.get("updated", ""))
    assert created_field.startswith(now_year), (
        f"created={created_field!r} must be stamped server-side"
    )
    assert updated_field.startswith(now_year), (
        f"updated={updated_field!r} must be stamped server-side"
    )
    # Sanity: the 1999 value is not anywhere in the serialised file.
    assert "1999" not in text


@pytest.mark.asyncio
async def test_memory_write_ignores_model_updated(
    memory_ctx: tuple[Path, Path],
) -> None:
    """Reading ``updated`` from args would let the model pin
    top-of-list forever; the handler ignores it.
    """
    vault, _idx = memory_ctx
    res = await _write(
        {
            "path": "inbox/u.md",
            "title": "U",
            "body": "b",
            "updated": "9999-12-31T23:59:59+00:00",
        }
    )
    assert res.get("is_error") is not True
    text = (vault / "inbox" / "u.md").read_text(encoding="utf-8")
    assert "9999" not in text
