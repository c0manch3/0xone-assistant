from __future__ import annotations

from pathlib import Path

from assistant.bridge.history import history_to_sdk_envelopes
from assistant.state.db import apply_schema, connect


async def test_null_block_type_defaults_to_text(tmp_path: Path) -> None:
    """R12 defence-in-depth: a row with block_type=NULL (which cannot
    happen after a clean migration, but COULD on a broken backfill)
    must be treated as 'text' and NOT crash the envelope builder."""
    db = tmp_path / "null.db"
    conn = await connect(db)
    await apply_schema(conn)

    # The v=2 migration adds a NOT NULL DEFAULT on block_type, so a direct
    # UPDATE to NULL is rejected. Drop the constraint for this one test by
    # rebuilding the table without it — mirrors what a broken future
    # migration might produce, which is what R12 defends against.
    await conn.execute(
        "CREATE TABLE conversations_null_test ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "chat_id INTEGER NOT NULL, "
        "turn_id TEXT NOT NULL, "
        "role TEXT NOT NULL, "
        "content_json TEXT NOT NULL, "
        "meta_json TEXT, "
        "block_type TEXT, "  # no NOT NULL
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))"
    )
    await conn.execute("DROP TABLE conversations")
    await conn.execute("ALTER TABLE conversations_null_test RENAME TO conversations")
    await conn.execute(
        "INSERT INTO turns(chat_id, turn_id, status, completed_at) "
        "VALUES (1, 't-null', 'complete', strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
    )
    await conn.execute(
        "INSERT INTO conversations(chat_id, turn_id, role, content_json, block_type) "
        "VALUES (1, 't-null', 'user', ?, NULL)",
        ('[{"type":"text","text":"hello"}]',),
    )
    await conn.commit()

    async with conn.execute("SELECT block_type FROM conversations WHERE turn_id='t-null'") as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] is None

    # Simulate a load_recent output row with block_type=None and feed it
    # through the envelope builder.
    rows = [
        {
            "id": 1,
            "chat_id": 1,
            "turn_id": "t-null",
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
            "meta": None,
            "created_at": "2026-04-20T12:00:00Z",
            "block_type": None,
        }
    ]
    envs = list(history_to_sdk_envelopes(rows, chat_id=1))
    # Post S13 fix: history collapses into a single context envelope; the
    # defence is still that a NULL block_type is treated as 'text' and
    # the row's text content appears in the rendered context (not dropped
    # or crashed).
    assert len(envs) == 1
    assert envs[0]["type"] == "user"
    content = envs[0]["message"]["content"]
    assert isinstance(content, str)
    assert "user: hello" in content

    await conn.close()
