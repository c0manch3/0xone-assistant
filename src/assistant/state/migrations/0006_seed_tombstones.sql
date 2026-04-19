-- 0006_seed_tombstones.sql — phase 8 Q10 (owner-deleted-seed marker)
--
-- When `tools/schedule/main.py rm <id>` deletes a row with a non-NULL
-- seed_key, we INSERT into this table so the next `Daemon.start()`
-- does NOT re-seed autonomously. The owner explicitly uses
-- `revive-seed <key>` to re-enable.
--
-- SF-F2 note: this file is NOT loaded via `conn.executescript(...)`.
-- `_apply_v6` in `src/assistant/state/db.py` executes each statement
-- individually with `await conn.execute(...)` so the whole migration
-- stays inside one `BEGIN IMMEDIATE` transaction (v2 blocker).

CREATE TABLE IF NOT EXISTS seed_tombstones (
    seed_key   TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

PRAGMA user_version = 6;
