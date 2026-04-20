from __future__ import annotations

import json
import uuid
from typing import Any

import aiosqlite


class ConversationStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def append(
        self,
        chat_id: int,
        turn_id: str,
        role: str,
        blocks: list[dict[str, Any]],
        meta: dict[str, Any] | None = None,
    ) -> int:
        async with self._conn.execute(
            "INSERT INTO conversations(chat_id, turn_id, role, content_json, meta_json) "
            "VALUES (?,?,?,?,?) RETURNING id",
            (
                chat_id,
                turn_id,
                role,
                json.dumps(blocks, ensure_ascii=False),
                json.dumps(meta, ensure_ascii=False) if meta else None,
            ),
        ) as cur:
            row = await cur.fetchone()
        await self._conn.commit()
        assert row is not None
        row_id: int = row[0]
        return row_id

    @staticmethod
    def new_turn_id() -> str:
        return uuid.uuid4().hex
