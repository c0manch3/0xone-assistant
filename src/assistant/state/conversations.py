from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import aiosqlite


class ConversationStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()

    async def append(
        self,
        chat_id: int,
        turn_id: str,
        role: str,
        blocks: list[dict[str, Any]],
        meta: dict[str, Any] | None = None,
    ) -> int:
        content_json = json.dumps(blocks, ensure_ascii=False, default=str)
        meta_json = json.dumps(meta, ensure_ascii=False, default=str) if meta is not None else None
        async with self._lock:
            async with self._conn.execute(
                "INSERT INTO conversations(chat_id, turn_id, role, content_json, meta_json) "
                "VALUES (?,?,?,?,?) RETURNING id",
                (chat_id, turn_id, role, content_json, meta_json),
            ) as cur:
                row = await cur.fetchone()
            await self._conn.commit()
        assert row is not None
        return int(row[0])

    async def load_recent(self, chat_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """Return most-recent turns as dicts with parsed content/meta, oldest-first."""
        async with self._conn.execute(
            "SELECT id, chat_id, turn_id, role, content_json, meta_json, created_at "
            "FROM conversations WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        result: list[dict[str, Any]] = [
            {
                "id": row[0],
                "chat_id": row[1],
                "turn_id": row[2],
                "role": row[3],
                "content": json.loads(row[4]),
                "meta": json.loads(row[5]) if row[5] is not None else None,
                "created_at": row[6],
            }
            for row in rows
        ]
        result.reverse()
        return result

    @staticmethod
    def new_turn_id() -> str:
        return uuid.uuid4().hex
