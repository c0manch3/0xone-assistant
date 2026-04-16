from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import aiosqlite


class ConversationStore:
    """Persistence for conversation blocks (role + content_json + block_type).

    Schema (v2): rows live in `conversations(id PK, chat_id, turn_id FK, role,
    content_json, block_type, created_at)`. Each SDK block gets its own row;
    role ∈ {user, assistant, tool}; block_type ∈ {text, tool_use, tool_result,
    thinking}. Turn lifecycle lives in `assistant.state.turns.TurnStore` — this
    class never inserts/updates `turns`.
    """

    def __init__(self, conn: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
        self._conn = conn
        self._lock = lock or asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def append(
        self,
        chat_id: int,
        turn_id: str,
        role: str,
        blocks: list[dict[str, Any]],
        *,
        block_type: str | None,
    ) -> int:
        """Append a single block-row. `block_type` is required (may be None only
        for legacy-compat paths)."""
        content_json = json.dumps(blocks, ensure_ascii=False, default=str)
        async with self._lock:
            async with self._conn.execute(
                "INSERT INTO conversations(chat_id, turn_id, role, content_json, block_type) "
                "VALUES (?, ?, ?, ?, ?) RETURNING id",
                (chat_id, turn_id, role, content_json, block_type),
            ) as cur:
                row = await cur.fetchone()
            await self._conn.commit()
        assert row is not None
        return int(row[0])

    async def load_recent(self, chat_id: int, limit_turns: int = 20) -> list[dict[str, Any]]:
        """Rows belonging to the last N *complete* turns, chronological order.

        Interrupted / pending turns are skipped entirely (SDK refuses orphan
        tool_use without a matching tool_result).
        """
        # NB: insertion order (`t.rowid`) is the deterministic tiebreaker —
        # `_utcnow_iso` has 1-second granularity, so multiple turns within the
        # same second would otherwise have undefined ordering.
        sql = """
            SELECT c.id, c.chat_id, c.turn_id, c.role, c.content_json,
                   c.block_type, c.created_at
            FROM conversations c
            WHERE c.chat_id = ?
              AND c.turn_id IN (
                  SELECT t.turn_id
                  FROM turns t
                  WHERE t.chat_id = ? AND t.status = 'complete'
                  ORDER BY COALESCE(t.completed_at, t.started_at) DESC,
                           t.started_at DESC,
                           t.rowid DESC
                  LIMIT ?
              )
            ORDER BY c.id ASC
        """
        async with self._conn.execute(sql, (chat_id, chat_id, limit_turns)) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": row[0],
                "chat_id": row[1],
                "turn_id": row[2],
                "role": row[3],
                "content": json.loads(row[4]),
                "block_type": row[5],
                "created_at": row[6],
            }
            for row in rows
        ]

    @staticmethod
    def new_turn_id() -> str:
        return uuid.uuid4().hex
