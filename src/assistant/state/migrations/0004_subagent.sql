-- Migration 0004 — phase 6: subagent_jobs ledger.
-- REFERENCE ONLY. Actual runner in src/assistant/state/db.py executes
-- each statement individually to mirror the phase 2/3 pattern (avoids
-- the executescript implicit-COMMIT trap).
--
-- Status machine (research RQ4 + RQ7):
--    requested → started → (completed|failed|stopped|interrupted|error|dropped)
--
-- Wave-2 changes:
--   * sdk_agent_id is NULLable; partial UNIQUE allows multiple
--     pending CLI / @tool rows with NULL while still preventing
--     duplicate Start hook fires for a real agent_id (B-W2-3).
--   * Pre-picker rows live as status='requested' with sdk_agent_id IS
--     NULL until the picker dispatches. Start hook patches sdk_agent_id
--     and flips status to 'started' via update_sdk_agent_id_for_claimed_request.
--   * sdk_session_id stored only for forensic access — Start vs Stop hook
--     emit asymmetric session_ids; do not match on it (GAP #9).
--
-- Fix-pack (devil H-W2-7 / F6): ``depth`` column dropped — never written
-- non-zero in phase 6 (recursion is structurally blocked by build_agents
-- omitting "Task" from each definition's tool list, pitfall #2). SQLite
-- ALTER TABLE DROP COLUMN is painful post-deploy, so we drop it BEFORE
-- the column reaches production rather than carrying a forever-zero
-- field that future maintenance has to wonder about.
--
-- Fix-pack (code H1 / devil C-W2-4 / QA HIGH-3 / F1): ``attempts`` and
-- ``last_error`` columns added so the picker can mark a row as
-- ``'error'`` after N consecutive dispatch failures (claude CLI down,
-- model refused to invoke Task tool, etc.) instead of looping forever
-- on a permanently-stuck ``requested`` row.

CREATE TABLE IF NOT EXISTS subagent_jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sdk_agent_id      TEXT,                                              -- nullable for pre-picker rows
    sdk_session_id    TEXT,                                              -- subagent's own session_id (Stop hook); forensic only
    parent_session_id TEXT,                                              -- parent session_id (Start hook)
    agent_type        TEXT    NOT NULL,                                  -- 'general' | 'worker' | 'researcher'
    task_text         TEXT,                                              -- pre-picker prompt; NULL for native-Task spawns
    transcript_path   TEXT,                                              -- agent_transcript_path captured at Stop
    status            TEXT    NOT NULL DEFAULT 'started',
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    result_summary    TEXT,                                              -- first 500 chars of last assistant message
    cost_usd          REAL,                                              -- nullable; phase-9 accounting (GAP #11)
    callback_chat_id  INTEGER NOT NULL,                                  -- always OWNER_CHAT_ID in phase 6
    spawned_by_kind   TEXT    NOT NULL,                                  -- 'user' | 'scheduler' | 'tool'
    spawned_by_ref    TEXT,                                              -- schedule/turn id; nullable
    attempts          INTEGER NOT NULL DEFAULT 0,                        -- picker dispatch attempt counter (F1)
    last_error        TEXT,                                              -- last picker dispatch error message (F1)
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    started_at        TEXT,                                              -- on Start hook
    finished_at       TEXT                                               -- on Stop hook / recover_orphans
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_subagent_jobs_sdk_agent_id_uq
    ON subagent_jobs(sdk_agent_id) WHERE sdk_agent_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_subagent_jobs_status_started
    ON subagent_jobs(status, started_at);

CREATE INDEX IF NOT EXISTS idx_subagent_jobs_status_created
    ON subagent_jobs(status, created_at);

PRAGMA user_version = 4;
