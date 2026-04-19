-- 0005_schedule_seed_key.sql — phase 8 (idempotent vault_auto_commit seed)
--
-- Adds `seed_key TEXT` column (NULLable) to schedules and a partial
-- UNIQUE INDEX that ignores NULLs. Pre-existing rows keep `seed_key
-- IS NULL` and are unaffected; new default-seeded rows carry a stable
-- key (e.g. 'vault_auto_commit') that the unique index prevents from
-- duplicating on Daemon restart.
--
-- Partial index (WHERE seed_key IS NOT NULL) is SQLite 3.8+; confirmed
-- supported by aiosqlite bundle (spike R-6).
--
-- SF-F2 note: this file is NOT loaded via `conn.executescript(...)`.
-- `_apply_v5` in `src/assistant/state/db.py` executes each statement
-- individually with `await conn.execute(...)` so the whole migration
-- stays inside one `BEGIN IMMEDIATE` transaction (v2 blocker).

ALTER TABLE schedules ADD COLUMN seed_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_schedules_seed_key
    ON schedules(seed_key) WHERE seed_key IS NOT NULL;

PRAGMA user_version = 5;
