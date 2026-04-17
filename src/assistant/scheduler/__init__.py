"""Scheduler package — phase 5.

Public helpers:
  * `iso_utc_z(dt)` / `from_iso_utc(s)` — single source of truth for the
    `scheduled_for` / `last_fire_at` ISO-8601 UTC string format used across
    `SchedulerStore`, `cron.is_due`, and the `tools/schedule` CLI
    (detailed-plan §16 invariant: `ISO-8601 UTC with trailing 'Z'`).

Sub-modules are imported lazily by callers to keep package import cheap
(e.g. CLI only needs `cron`, not the aiosqlite-bound `store`).
"""

from __future__ import annotations

from datetime import UTC, datetime


def iso_utc_z(dt: datetime) -> str:
    """Serialise a UTC datetime to `YYYY-MM-DDTHH:MM:SSZ`.

    Caller guarantees `dt` is UTC (naive is accepted as UTC for ergonomics,
    but the producer contract insists on `tz-aware UTC` — we normalise
    defensively rather than raising, since malformed callers would otherwise
    silently write naive strings).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso_utc(s: str) -> datetime:
    """Parse a `YYYY-MM-DDTHH:MM:SSZ` string to a tz-aware UTC datetime.

    Python 3.11+ `fromisoformat` accepts `Z` directly, but we strip it and
    re-apply `+00:00` so the behaviour is identical on 3.10-3.12 targets
    (the project pins 3.12 today; defensive parsing costs nothing).
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(UTC)
