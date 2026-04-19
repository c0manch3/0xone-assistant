#!/usr/bin/env python3
"""Phase 8 R-6: migration `0005_schedule_seed_key.sql` shape verification.

Creates an in-memory SQLite file, seeds a `schedules` table that mirrors
the current phase-5 shape (v3 migration), inserts 10 rows, then applies
the proposed phase-8 migration (ALTER ADD COLUMN + partial UNIQUE INDEX
+ PRAGMA user_version).

Asserts:
  * ALTER ADD COLUMN succeeds with `seed_key` defaulting to NULL.
  * All pre-existing rows now have seed_key IS NULL.
  * Index `idx_schedules_seed_key` is partial (excludes NULL).
  * Two rows with NULL seed_key are allowed.
  * Two rows with same non-NULL seed_key raise IntegrityError.
  * `PRAGMA user_version` == 5.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

# Minimal v3 shape — mirrors `src/assistant/state/migrations/0003_scheduler.sql`.
V3_SCHEMA = """
CREATE TABLE schedules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cron         TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    tz           TEXT NOT NULL DEFAULT 'UTC',
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_fire_at TEXT
);
PRAGMA user_version = 3;
"""

V5_MIGRATION = """
ALTER TABLE schedules ADD COLUMN seed_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_schedules_seed_key
    ON schedules(seed_key) WHERE seed_key IS NOT NULL;
PRAGMA user_version = 5;
"""


def main() -> int:
    report: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="spike_alter_") as td:
        db_path = Path(td) / "test.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(V3_SCHEMA)
            for i in range(10):
                conn.execute(
                    "INSERT INTO schedules(cron, prompt, tz) VALUES (?, ?, ?)",
                    (f"{i} * * * *", f"prompt-{i}", "UTC"),
                )
            conn.commit()
            report["pre_migration_user_version"] = conn.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            report["pre_migration_row_count"] = conn.execute(
                "SELECT COUNT(*) FROM schedules"
            ).fetchone()[0]

            # Apply migration.
            conn.executescript(V5_MIGRATION)
            report["post_migration_user_version"] = conn.execute(
                "PRAGMA user_version"
            ).fetchone()[0]

            # Existing rows retain NULL in seed_key.
            rows_null = conn.execute(
                "SELECT COUNT(*) FROM schedules WHERE seed_key IS NULL"
            ).fetchone()[0]
            report["rows_with_null_seed_key"] = rows_null

            # Index metadata.
            idx_info = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='idx_schedules_seed_key'"
            ).fetchone()
            report["idx_sql"] = idx_info[0] if idx_info else None
            # Partial?
            report["idx_is_partial"] = (
                report["idx_sql"] is not None
                and "WHERE seed_key IS NOT NULL" in report["idx_sql"]
            )

            # Insert a row with a seed_key.
            conn.execute(
                "INSERT INTO schedules(cron, prompt, tz, seed_key) VALUES (?,?,?,?)",
                ("0 3 * * *", "vault backup", "UTC", "vault_auto_commit"),
            )
            conn.commit()

            # Duplicate non-NULL seed_key -> IntegrityError.
            try:
                conn.execute(
                    "INSERT INTO schedules(cron, prompt, tz, seed_key) VALUES (?,?,?,?)",
                    ("0 4 * * *", "dup", "UTC", "vault_auto_commit"),
                )
                conn.commit()
                report["duplicate_non_null_seed_key"] = "UNEXPECTED_ALLOWED"
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                report["duplicate_non_null_seed_key"] = (
                    f"expected_IntegrityError: {exc}"
                )

            # Additional rows with NULL seed_key still allowed.
            try:
                conn.execute(
                    "INSERT INTO schedules(cron, prompt, tz) VALUES (?,?,?)",
                    ("0 5 * * *", "other", "UTC"),
                )
                conn.commit()
                report["another_null_seed_key_allowed"] = True
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                report["another_null_seed_key_allowed"] = f"BLOCKED: {exc}"

            # Also test the v6 tombstone migration on top.
            conn.executescript(
                """
CREATE TABLE IF NOT EXISTS seed_tombstones (
    seed_key TEXT PRIMARY KEY,
    deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
PRAGMA user_version = 6;
"""
            )
            report["post_v6_user_version"] = conn.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO seed_tombstones(seed_key) VALUES (?)",
                ("vault_auto_commit",),
            )
            conn.commit()
            report["tombstone_lookup_exists"] = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM seed_tombstones WHERE seed_key=?)",
                ("vault_auto_commit",),
            ).fetchone()[0]
            # Idempotent re-insert.
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO seed_tombstones(seed_key) VALUES (?)",
                    ("vault_auto_commit",),
                )
                conn.commit()
                report["tombstone_insert_or_replace_ok"] = True
            except sqlite3.IntegrityError as exc:
                report["tombstone_insert_or_replace_ok"] = f"error: {exc}"
        finally:
            conn.close()

    out_path = Path(__file__).with_name("spike_sqlite_alter_table_report.json")
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
