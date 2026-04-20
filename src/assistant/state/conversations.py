from __future__ import annotations

import json
import uuid
from typing import Any

import aiosqlite


class ConversationStore:
    """Phase-2 conversation store: rows in ``conversations`` grouped by
    turn, with per-turn lifecycle tracked in the ``turns`` table.

    Lifecycle:
      - ``start_turn(chat_id)`` → ``turn_id`` (status='pending')
      - 0..N ``append(...)`` calls while the bridge streams blocks
      - ``complete_turn(turn_id, meta)`` on successful ResultMessage
        OR ``interrupt_turn(turn_id)`` on timeout/exception/early exit.

    Timestamp invariant (SW2): all ``created_at`` / ``completed_at``
    columns — in BOTH tables, in BOTH defaults AND runtime UPDATE/INSERT
    — use ``strftime('%Y-%m-%dT%H:%M:%SZ','now')`` (seconds precision).
    Mixing second- and millisecond-precision formats breaks lex-ordering
    because ``Z`` (0x5A) sorts AFTER ``.`` (0x2E), so a row with
    ``...:00Z`` would incorrectly sort after one with ``...:00.500Z``.
    Phase 2 does not need sub-second precision; keeping one format
    everywhere is simpler than normalising legacy rows on upgrade.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Turn-ID helpers
    # ------------------------------------------------------------------
    @staticmethod
    def new_turn_id() -> str:
        """Generate a fresh turn-id. Kept as staticmethod for phase-1 tests
        that don't own a turns row (they just insert into conversations)."""
        return uuid.uuid4().hex

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------
    async def start_turn(self, chat_id: int) -> str:
        turn_id = uuid.uuid4().hex
        await self._conn.execute(
            "INSERT INTO turns(chat_id, turn_id, status, created_at) "
            "VALUES (?,?,'pending', strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (chat_id, turn_id),
        )
        await self._conn.commit()
        return turn_id

    async def complete_turn(self, turn_id: str, meta: dict[str, Any]) -> None:
        await self._conn.execute(
            "UPDATE turns SET status='complete', "
            "completed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
            "meta_json=? WHERE turn_id=?",
            (json.dumps(meta, ensure_ascii=False), turn_id),
        )
        await self._conn.commit()

    async def interrupt_turn(self, turn_id: str) -> None:
        """Mark a pending turn as interrupted. No-op if already terminal."""
        await self._conn.execute(
            "UPDATE turns SET status='interrupted', "
            "completed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE turn_id=? AND status='pending'",
            (turn_id,),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Row append — phase-2 signature adds `block_type` (B5); phase-1 callers
    # must pass it. `meta` is kept as kwarg for parity with phase 1.
    # ------------------------------------------------------------------
    async def append(
        self,
        chat_id: int,
        turn_id: str,
        role: str,
        blocks: list[dict[str, Any]],
        *,
        block_type: str = "text",
        meta: dict[str, Any] | None = None,
    ) -> int:
        async with self._conn.execute(
            "INSERT INTO conversations"
            "(chat_id, turn_id, role, content_json, meta_json, block_type) "
            "VALUES (?,?,?,?,?,?) RETURNING id",
            (
                chat_id,
                turn_id,
                role,
                json.dumps(blocks, ensure_ascii=False),
                json.dumps(meta, ensure_ascii=False) if meta else None,
                block_type,
            ),
        ) as cur:
            row = await cur.fetchone()
        await self._conn.commit()
        assert row is not None
        return int(row[0])

    # ------------------------------------------------------------------
    # History load — turn-limited (B6).
    # ------------------------------------------------------------------
    async def load_recent(self, chat_id: int, limit: int) -> list[dict[str, Any]]:
        """Return all rows belonging to the N most recent COMPLETE turns,
        in chronological order (ASC by id).

        ``limit`` is a turn count, not a row count (B6 fix). Interrupted and
        pending turns are skipped wholesale — a turn that was cut mid-stream
        may have an assistant ``tool_use`` without a matching ``tool_result``,
        which would poison replay if surfaced.
        """
        q = (
            "WITH recent_turns AS ("
            "  SELECT turn_id FROM turns "
            "  WHERE chat_id = ? AND status = 'complete' "
            "  ORDER BY completed_at DESC LIMIT ?"
            ") "
            "SELECT c.id, c.chat_id, c.turn_id, c.role, c.content_json, "
            "c.meta_json, c.created_at, c.block_type "
            "FROM conversations c "
            "JOIN recent_turns t ON c.turn_id = t.turn_id "
            "ORDER BY c.id ASC"
        )
        async with self._conn.execute(q, (chat_id, limit)) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "chat_id": r[1],
                "turn_id": r[2],
                "role": r[3],
                "content": json.loads(r[4]),
                "meta": json.loads(r[5]) if r[5] else None,
                "created_at": r[6],
                "block_type": r[7],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Orphan cleanup — S10.
    # ------------------------------------------------------------------
    async def cleanup_orphan_pending_turns(self) -> int:
        """Mark any turns still in ``pending`` status as ``interrupted``.

        Call at daemon startup — after a crash mid-turn, the turns row keeps
        ``status='pending'`` forever. ``load_recent``'s filter skips them but
        they accumulate indefinitely otherwise (S10 fix).

        Returns the number of rows updated for logging.
        """
        cur = await self._conn.execute(
            "UPDATE turns SET status='interrupted', "
            "completed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE status='pending'"
        )
        await self._conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0
