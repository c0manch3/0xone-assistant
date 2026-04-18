-- 0004_subagent.sql — phase 6 (SDK-native subagent ledger, wave-2)
--
-- One table: `subagent_jobs`. Keyed on `sdk_agent_id` (the SDK's own agent_id
-- from SubagentStart/Stop hooks). Column is NULLable because CLI/picker
-- pre-create rows before the SDK assigns the id; a partial UNIQUE index
-- (see below) keeps the identity contract tight once a row is dispatched.
--
-- Status machine:
--     requested → started → (completed | failed | stopped | interrupted | error | dropped)
--
-- Only `requested` rows carry `sdk_agent_id IS NULL`. The Start hook patches
-- them via `update_sdk_agent_id_for_claimed_request`; recover_orphans
-- transitions a `requested` row older than 1 h to `dropped`.
--
-- `sdk_session_id` (from Stop hook) is intentionally NOT used as a lookup key
-- (it is asymmetric between Start and Stop — Start sees the parent session,
-- Stop sees the subagent's own session; GAP #9). Stored for forensic access.
--
-- `cost_usd` stays NULL for phase 6 (GAP #11 — per-child attribution deferred
-- to phase 9 where we read `TaskNotificationMessage.usage` or diff main-turn
-- totals before/after each child). The column is provisioned now so phase 9
-- does not require another migration.
--
-- Forward compat: any future column bump goes in `0005_*.sql` (add-only;
-- never rename). See GAP #16.

CREATE TABLE IF NOT EXISTS subagent_jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    -- sdk_agent_id is NULL for pre-picker rows; filled by Start hook /
    -- picker.update_sdk_agent_id_for_claimed_request. Partial UNIQUE index
    -- below prevents duplicates only among non-NULL values.
    sdk_agent_id      TEXT,
    sdk_session_id    TEXT,                             -- subagent's own session_id (from Stop hook) — forensic only, see GAP #9
    parent_session_id TEXT,                             -- parent session_id (from Start hook)
    agent_type        TEXT    NOT NULL,                 -- 'general' | 'worker' | 'researcher'
    task_text         TEXT,                             -- populated for CLI/picker flow; NULL for native-Task main-turn spawn
    transcript_path   TEXT,                             -- agent_transcript_path from Stop hook
    -- status machine: requested → started → (completed|failed|stopped|interrupted|error|dropped)
    status            TEXT    NOT NULL DEFAULT 'started',
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    result_summary    TEXT,                             -- first 500 chars of last_assistant_message
    cost_usd          REAL,                             -- nullable; reserved for phase-9 accounting (GAP #11)
    callback_chat_id  INTEGER NOT NULL,                 -- always OWNER_CHAT_ID for phase 6
    spawned_by_kind   TEXT    NOT NULL,                 -- 'user' | 'scheduler' | 'cli'
    spawned_by_ref    TEXT,                             -- schedule_id on scheduler spawns; null otherwise
    depth             INTEGER NOT NULL DEFAULT 0,       -- always 0 in phase 6 (see pitfall #2 + regression test)
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    started_at        TEXT,                             -- set on SubagentStart hook fire
    finished_at       TEXT                              -- set on SubagentStop hook fire
);

-- Partial UNIQUE — only non-NULL sdk_agent_id values must be unique
-- (SQLite 3.8+ syntax). Pending CLI rows carry NULL with no conflict.
CREATE UNIQUE INDEX IF NOT EXISTS idx_subagent_jobs_sdk_agent_id_uq
    ON subagent_jobs(sdk_agent_id) WHERE sdk_agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_subagent_jobs_status_started ON subagent_jobs(status, started_at);
CREATE INDEX IF NOT EXISTS idx_subagent_jobs_status_created ON subagent_jobs(status, created_at);

-- Forward compat (GAP #16): future schema bumps use 0005_*.sql; do not
-- rename columns here — add-only.
PRAGMA user_version = 4;
