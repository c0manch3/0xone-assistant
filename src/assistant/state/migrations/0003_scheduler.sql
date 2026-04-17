-- 0003_scheduler.sql — phase 5 (scheduler daemon + triggers ledger)
--
-- Introduces two tables:
--
-- * `schedules` — one row per user-authored cron job. Soft-delete via
--   `enabled=0`; `last_fire_at` holds the `scheduled_for` value of the most
--   recently materialised trigger (invariant: updated ONLY when a new
--   trigger row is inserted — see `SchedulerStore.try_materialize_trigger`).
-- * `triggers` — one row per materialised (cron-match) delivery attempt.
--   `status` state-machine: pending → sent → acked | pending (retry) | dead.
--   `dropped` is a terminal state for schedules disabled between materialisation
--   and delivery.
--
-- UNIQUE(schedule_id, scheduled_for) is load-bearing: the producer uses
-- `INSERT OR IGNORE` so re-firing the same minute boundary is a no-op.
-- FK ON DELETE CASCADE: a hard-deleted schedule drops its history.

CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cron          TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    tz            TEXT NOT NULL DEFAULT 'UTC',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_fire_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON schedules(enabled);

CREATE TABLE IF NOT EXISTS triggers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id   INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    prompt        TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    sent_at       TEXT,
    acked_at      TEXT,
    UNIQUE(schedule_id, scheduled_for)
);
CREATE INDEX IF NOT EXISTS idx_triggers_status_time
    ON triggers(status, scheduled_for);

PRAGMA user_version = 3;
