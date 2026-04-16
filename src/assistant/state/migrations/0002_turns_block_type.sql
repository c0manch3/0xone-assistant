-- Migration 0002: introduce `turns` table, `block_type` column, and FK CASCADE.
--
-- Pre-requisite (set by migration runner): PRAGMA foreign_keys = OFF for the
-- duration of the script; the runner restores ON after COMMIT.

CREATE TABLE IF NOT EXISTS turns (
    turn_id      TEXT PRIMARY KEY,
    chat_id      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | complete | interrupted
    started_at   TEXT NOT NULL,
    completed_at TEXT,
    meta_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_chat_status
    ON turns(chat_id, status, completed_at);

-- Backfill: every existing distinct turn_id becomes a synthetic complete turn.
INSERT OR IGNORE INTO turns (turn_id, chat_id, status, started_at, completed_at, meta_json)
SELECT
    turn_id,
    chat_id,
    'complete',
    MIN(created_at),
    MAX(created_at),
    -- lift meta_json from the first row of the turn (if any)
    (SELECT meta_json FROM conversations c2
        WHERE c2.turn_id = c.turn_id ORDER BY c2.id LIMIT 1)
FROM conversations c
GROUP BY turn_id, chat_id;

-- Recreate `conversations` to: (a) add `block_type`, (b) drop `meta_json`
-- (now lives on `turns`), (c) add FK to `turns(turn_id)` ON DELETE CASCADE.
CREATE TABLE conversations_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    turn_id      TEXT NOT NULL,
    role         TEXT NOT NULL,
    content_json TEXT NOT NULL,
    block_type   TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    FOREIGN KEY (turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
);

-- Backfill block_type: only `role='user'` legacy rows are known to be 'text'.
INSERT INTO conversations_new (id, chat_id, turn_id, role, content_json, block_type, created_at)
SELECT
    id, chat_id, turn_id, role, content_json,
    CASE WHEN role = 'user' THEN 'text' ELSE NULL END,
    created_at
FROM conversations;

DROP TABLE conversations;
ALTER TABLE conversations_new RENAME TO conversations;

CREATE INDEX IF NOT EXISTS idx_conversations_chat_time
    ON conversations(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_turn
    ON conversations(chat_id, turn_id);
