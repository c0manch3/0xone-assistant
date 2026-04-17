"""Phase 4 B3: fabricate conversation rows without running the SDK.

The bridge-level fix (UserMessage tool_result persistence) makes history
replay tests finally meaningful, but running a real SDK turn is billable
and non-deterministic. Tests instead seed the exact row shape through
these helpers and feed rows into `history_to_user_envelopes`.

All three helpers are idempotent w.r.t. the `turns` FK parent row — call
them in whatever order the scenario requires.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite


async def _ensure_turn(conn: aiosqlite.Connection, *, chat_id: int, turn_id: str) -> None:
    """Idempotent insert of a completed `turns` row matching the FK."""
    async with conn.execute("SELECT 1 FROM turns WHERE turn_id = ?", (turn_id,)) as cur:
        if await cur.fetchone() is not None:
            return
    await conn.execute(
        "INSERT INTO turns(turn_id, chat_id, status, started_at, completed_at) "
        "VALUES (?, ?, 'complete', "
        "strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
        "strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
        (turn_id, chat_id),
    )
    await conn.commit()


async def seed_user_text_row(
    conn: aiosqlite.Connection,
    *,
    chat_id: int,
    turn_id: str,
    text: str,
) -> None:
    """Insert a `user`/`text` conversation row."""
    await _ensure_turn(conn, chat_id=chat_id, turn_id=turn_id)
    payload = [{"type": "text", "text": text}]
    await conn.execute(
        "INSERT INTO conversations(chat_id, turn_id, role, content_json, block_type) "
        "VALUES (?, ?, 'user', ?, 'text')",
        (chat_id, turn_id, json.dumps(payload, ensure_ascii=False)),
    )
    await conn.commit()


async def seed_tool_use_row(
    conn: aiosqlite.Connection,
    *,
    chat_id: int,
    turn_id: str,
    tool_use_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
) -> None:
    """Insert an `assistant`/`tool_use` conversation row."""
    await _ensure_turn(conn, chat_id=chat_id, turn_id=turn_id)
    payload = [
        {
            "type": "tool_use",
            "id": tool_use_id,
            "name": tool_name,
            "input": tool_input or {},
        }
    ]
    await conn.execute(
        "INSERT INTO conversations(chat_id, turn_id, role, content_json, block_type) "
        "VALUES (?, ?, 'assistant', ?, 'tool_use')",
        (chat_id, turn_id, json.dumps(payload, ensure_ascii=False)),
    )
    await conn.commit()


async def seed_tool_result_row(
    conn: aiosqlite.Connection,
    *,
    chat_id: int,
    turn_id: str,
    tool_use_id: str,
    content: str | list[dict[str, Any]] | None,
    is_error: bool = False,
) -> None:
    """Insert a `tool`/`tool_result` conversation row (matches a prior tool_use_id)."""
    await _ensure_turn(conn, chat_id=chat_id, turn_id=turn_id)
    payload = [
        {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
            "is_error": is_error,
        }
    ]
    await conn.execute(
        "INSERT INTO conversations(chat_id, turn_id, role, content_json, block_type) "
        "VALUES (?, ?, 'tool', ?, 'tool_result')",
        (chat_id, turn_id, json.dumps(payload, ensure_ascii=False)),
    )
    await conn.commit()
