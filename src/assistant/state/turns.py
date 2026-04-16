from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import aiosqlite


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TurnStore:
    """Lifecycle management for `turns` rows.

    A turn represents one round-trip with the model: from the moment the user
    sends a message until the SDK yields `ResultMessage` (→ `complete`) or the
    stream dies (→ `interrupted`). All blocks persisted through
    `ConversationStore.append(..., turn_id=...)` must reference a turn_id
    registered here first (FK CASCADE).
    """

    def __init__(self, conn: aiosqlite.Connection, lock: asyncio.Lock | None = None) -> None:
        self._conn = conn
        # Share a single writer-lock with ConversationStore when supplied.
        self._lock = lock or asyncio.Lock()

    async def start(self, chat_id: int) -> str:
        turn_id = uuid.uuid4().hex
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO turns(turn_id, chat_id, status, started_at) "
                "VALUES (?, ?, 'pending', ?)",
                (turn_id, chat_id, _utcnow_iso()),
            )
            await self._conn.commit()
        return turn_id

    async def complete(self, turn_id: str, meta: dict[str, Any] | None = None) -> None:
        meta_json = json.dumps(meta, ensure_ascii=False, default=str) if meta is not None else None
        async with self._lock:
            await self._conn.execute(
                "UPDATE turns SET status = 'complete', completed_at = ?, meta_json = ? "
                "WHERE turn_id = ?",
                (_utcnow_iso(), meta_json, turn_id),
            )
            await self._conn.commit()

    async def interrupt(self, turn_id: str) -> None:
        async with self._lock:
            await self._conn.execute(
                "UPDATE turns SET status = 'interrupted', completed_at = ? "
                "WHERE turn_id = ? AND status != 'complete'",
                (_utcnow_iso(), turn_id),
            )
            await self._conn.commit()
