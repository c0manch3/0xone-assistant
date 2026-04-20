-- Migration 0002 — add `turns` table + `conversations.block_type` column.
-- REFERENCE ONLY. Actual runner in src/assistant/state/db.py executes
-- each statement individually to avoid the executescript implicit-COMMIT trap
-- (see plan/phase2/implementation.md §2.1 / §2.2, B2 rationale).
-- This .sql file is kept so engineers can read the schema changes in one place.

DROP TABLE IF EXISTS conversations_new;

CREATE TABLE conversations_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    turn_id      TEXT NOT NULL,
    role         TEXT NOT NULL,
    content_json TEXT NOT NULL,
    meta_json    TEXT,
    block_type   TEXT NOT NULL DEFAULT 'text',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

INSERT INTO conversations_new (
    id, chat_id, turn_id, role, content_json, meta_json, created_at, block_type
)
SELECT id, chat_id, turn_id, role, content_json, meta_json, created_at, 'text'
FROM conversations;

DROP TABLE conversations;
ALTER TABLE conversations_new RENAME TO conversations;

CREATE INDEX idx_conversations_chat_time
    ON conversations(chat_id, created_at);
CREATE INDEX idx_conversations_turn
    ON conversations(chat_id, turn_id);

CREATE TABLE turns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    turn_id      TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|complete|interrupted
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    completed_at TEXT,
    meta_json    TEXT
);

INSERT OR IGNORE INTO turns (chat_id, turn_id, status, created_at, completed_at)
SELECT chat_id, turn_id, 'complete', MIN(created_at), MAX(created_at)
FROM conversations
GROUP BY chat_id, turn_id;

CREATE INDEX idx_turns_chat_completed
    ON turns(chat_id, completed_at);
