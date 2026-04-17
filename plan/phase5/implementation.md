# Phase 5 — Implementation (spike-verified, 2026-04-17)

## Revision history

- **v2** (2026-04-17, wave-2 fix-pack): закрыты 5 blockers + 10 gaps
  devil wave-2. Ключевые изменения:
  - **B-W2-1**: runtime sweep `revert_stuck_sent` в `SchedulerLoop._tick`
    каждую 4-ю итерацию (§3.7).
  - **B-W2-2**: полный body `Daemon.stop()` (§3.16.4) — adapter.stop()
    ПОСЛЕ bg-drain.
  - **B-W2-3**: `asyncio.shield` вокруг `mark_pending_retry` на
    CancelledError (§3.5).
  - **B-W2-4**: `_TZ_RE` выброшен; `ZoneInfo` — единственный authority
    (spike S-10, §3.9, §3.10).
  - **B-W2-5**: allowlist запрещает дублирующие флаги (§3.10).
  - **G-W2-1**: `TelegramRetryAfter` catch в `adapter.send_text` (§3.11).
  - **G-W2-4**: `count_catchup_misses` с `COALESCE(last_fire_at, created_at)`
    и cap 4×catchup (§3.4).
  - **G-W2-6**: status-precondition SQL на все mark_* transitions (§3.4).
  - **G-W2-8**: HISTORY cap **отложен в phase 6** (см. §3.14, §13 detailed-plan).
  - **G-W2-9**: `_memlib` refactor **отложен в phase 6** (см. §0, §13).
  - **G-W2-10**: heartbeat `_last_tick_at` + `_scheduler_health_check_bg`
    task (§3.7, §3.16).
  - **G-W2-3**: fixture table расширена до 22+5 кейсов (см. spike S-9).
  - **N-W2-1**: подтверждено — `ConversationStore.lock` property существует
    (`state/conversations.py:26-27`).
  - **N-W2-2**: helper `_iso_utc_z(dt)` / `_from_iso_utc(s)` (§3.2).
  - **N-W2-4**: разделены cooldowns — `loop_crash_cooldown_s=86400`,
    `catchup_recap_cooldown_s=3600` (§3.8).
  - **N-W2-5**: pidfile mode `0o600` (§3.16.2).
  - **N-W2-6**: коммит 7 поделён на 7a/7b (см. §1).

- **v1** (2026-04-17): initial after spikes S-1..S-9.

Empirical backing (S-1..S-10):
  - `spikes/phase5_s1_aiosqlite_contention.py` — contention under load.
  - `spikes/phase5_s2_adapter_send_text.py` — adapter call from bg task.
  - `spikes/phase5_s3_zoneinfo_dst.py` — DST spring/fall semantics.
  - `spikes/phase5_s4_flock_semantics.py` — flock lifecycle on macOS.
  - `spikes/phase5_s5_queue_backpressure.py` — asyncio.Queue shutdown.
  - `spikes/phase5_s6_incoming_message_shape.py` — dataclass fields.
  - `spikes/phase5_s7_system_notes_order.py` — note merge order.
  - `spikes/phase5_s8_try_materialize_atomicity.py` — INSERT+UPDATE atomicity.
  - `spikes/phase5_s9_cron_fixtures.json` — 22 valid + 5 invalid fixtures.
  - `spikes/phase5_s10_zoneinfo_authority.py` — `ZoneInfo` is sole tz authority.

Companion docs (coder **must** read before starting):

- `plan/phase5/description.md` — 83-line summary.
- `plan/phase5/detailed-plan.md` — canonical spec с §1–§16.
- `plan/phase5/spike-findings.md` — empirical answers + verdict table.
- `plan/phase4/implementation.md` — style precedent.
- `plan/phase2/implementation.md` — IncomingMessage, per-chat lock,
  `TurnStore.sweep_pending` invariants.

**Auth:** OAuth via `claude` CLI (`~/.claude/`). Не вводим
`ANTHROPIC_API_KEY` нигде. Scheduler-turns используют тот же bridge и
ту же OAuth сессию.

---

## 0. Pitfall box (MUST READ)

Things the coder absolutely MUST NOT do:

1. **DO NOT** use `queue.put_nowait(POISON)` on a full queue for shutdown
   — it raises `QueueFull` (spike S-5). Use `stop_event + wait_for(get,
   timeout=0.5)` consumer pattern instead.
2. **DO NOT** call `fcntl.flock` without `LOCK_NB` — a leaked fd from a
   previous daemon start in the same process will deadlock. S-4 case 5
   confirmed macOS blocks same-process-different-fd.
3. **DO NOT** skip `_inflight` set exclusion in `revert_stuck_sent` — B2
   regression (premature revert while consumer is actively delivering).
4. **DO NOT** add a second aiosqlite connection for the scheduler. S-1
   proved contention is negligible (p99 3.4 ms).
5. **DO NOT** forget `asyncio.shield()` around `store.mark_pending_retry(...)`
   inside `_deliver`'s `except asyncio.CancelledError` branch — the DB
   UPDATE runs inside the cancel scope and will itself be cancelled,
   leaving the trigger stuck in `status='sent'`. **(wave-2 B-W2-3)**
6. **DO NOT** `INSERT OR IGNORE` then commit before the `UPDATE
   schedules.last_fire_at` — the plan-§5.3 sequence inside ONE
   `async with lock:` is load-bearing; S-8 verified atomicity.
7. **DO NOT** iterate schedule candidates minute-by-minute in the
   producer loop — use `is_due(expr, last_fire_at, now)`.
8. **DO NOT** bake cron-parser code into `tools/schedule/main.py` — one
   source of truth: `src/assistant/scheduler/cron.py`. CLI imports via
   `sys.path.append(str(_ROOT / "src"))` (**append, NOT insert(0)** —
   phase-4 lesson, tech-debt #4 `_memlib` uses the same pattern; full
   `_memlib` consolidation deferred to phase 6, wave-2 G-W2-9).
9. **DO NOT** store tz-aware datetimes in SQLite — plan §2.1 uses
   `ISO-8601 UTC strings with trailing 'Z'`. Use helper
   `_iso_utc_z(dt)` / `_from_iso_utc(s)` in §3.2.
10. **DO NOT** match on `datetime.weekday()` directly — Python uses
    Mon=0..Sun=6; cron uses Sun=0..Sat=6. Convert:
    `cron_dow = (dt.weekday() + 1) % 7`. DOW=7 is REJECTED at parse time.
11. **DO NOT** invoke `adapter.send_text` from inside `ClaudeHandler`
    — handler communicates via `emit(text)` callback only. Dispatcher
    accumulates emitted chunks and calls `adapter.send_text` once.
12. **DO NOT** pass `meta={}` to `IncomingMessage` until S-6 delta ships
    (field is not there today; TypeError if you try).
13. **DO NOT** let `TelegramRetryAfter` propagate out of `adapter.send_text`
    into the dispatcher's general `except Exception` — that burns a Claude
    turn for 3-second rate-limit. Adapter catches and retries internally
    (§3.11, wave-2 G-W2-1).
14. **DO NOT** rely on the `_TZ_RE` regex for tz validation. It was
    DROPPED in wave-2 (B-W2-4): stdlib `ZoneInfo(name)` is authoritative
    and rejects injection via `ValueError` (spike S-10).
15. **DO NOT** UPDATE trigger status without a `WHERE status IN (...)`
    precondition (wave-2 G-W2-6). If rowcount=0 → log skew, discard from
    `_inflight`, don't raise. SQL preconditions enforce state-machine
    invariants that Python state alone cannot.

---

## 1. Commit plan (8 commits after wave-2 split)

Each commit is a logical unit, tests-first where useful, all under 500
LOC diff (7a/7b split handles wave-2 N-W2-6). Coder must run
`just lint && uv run pytest -x` before each.

| # | Title | New files | Edit | LOC |
|---|-------|-----------|------|-----|
| 1 | schema v3 + migration 0003 | `migrations/0003_scheduler.sql`, `tests/test_db_migrations_v3.py` | `state/db.py` (+15) | ~90 |
| 2 | cron parser + `is_due` (+ S-3 DST helpers) + S-9 fixtures | `scheduler/__init__.py`, `scheduler/cron.py`, `tests/test_scheduler_cron_parser.py`, `tests/test_scheduler_cron_semantics.py` | — | ~650 |
| 3 | `SchedulerStore` + store tests (status-preconditions) | `scheduler/store.py`, `tests/test_scheduler_store.py` | — | ~400 |
| 4 | `SchedulerDispatcher` + `ScheduledTrigger` + shield-on-cancel tests | `scheduler/dispatcher.py`, `tests/test_scheduler_dispatcher.py`, `tests/test_scheduler_dispatcher_shutdown_cancel.py` | `adapters/base.py` (+4: `meta` field) | ~380 |
| 5 | `SchedulerLoop` + `SchedulerSettings` + heartbeat + runtime-revert sweep + loop tests | `scheduler/loop.py`, `tests/test_scheduler_loop.py`, `tests/test_scheduler_runtime_revert_sweep.py` | `config.py` (+45: `SchedulerSettings`) | ~470 |
| 6 | CLI `tools/schedule/main.py` + Bash hook allowlist + dup-flag deny + CLI tests | `tools/schedule/main.py`, `skills/scheduler/SKILL.md`, `tests/test_schedule_cli.py`, `tests/test_scheduler_bash_hook_allowlist.py` | `bridge/hooks.py` (+65: `_BASH_PROGRAMS` + validator + dup-flag check) | ~700 |
| 7a | Telegram adapter hardening + `TelegramRetryAfter` + E2E | `tests/test_telegram_adapter_send_text.py` | `adapters/telegram.py` (+30: retry loop) | ~170 |
| 7b | Daemon integration (flock, clean-slate, scheduler spawn, origin branch, heartbeat health-check) + docs ops | `tests/test_scheduler_recovery.py`, `tests/test_scheduler_origin_branch_e2e.py`, `docs/ops/launchd.plist.example`, `docs/ops/scheduler.service.example` | `main.py` (+145), `handlers/message.py` (+20), `bridge/claude.py` (+5 docstring), `bridge/system_prompt.md` (+5) | ~490 |

Total: ~3350 LOC. Phase-4 debt #7 (HISTORY cap) отложен в phase 6
(wave-2 G-W2-8 decision).

### Commit messages (suggested)

```
phase 5: schema v3 scheduler migration
phase 5: stdlib cron parser + is_due with DST semantics
phase 5: SchedulerStore + status-precondition SQL
phase 5: SchedulerDispatcher + ScheduledTrigger + shield-on-cancel
phase 5: SchedulerLoop + runtime revert sweep + heartbeat
phase 5: tools/schedule CLI + bash allowlist (dup-flag deny) + skill
phase 5: Telegram adapter — TelegramRetryAfter retry loop
phase 5: Daemon integration + flock + origin branch + health check
```

---

## 2. Test-first order

For these three commits, coder writes tests FIRST:

- **Commit 2** (`cron.py`): S-9 fixture table → `test_scheduler_cron_semantics.py`.
  Implement parser + `is_due` to make fixtures pass.
- **Commit 3** (`store.py`): invariants + status-precondition SQL
  → `test_scheduler_store.py`. Implement store.
- **Commit 4** (`dispatcher.py`): delivery state-machine transitions +
  shutdown-cancel shield → `test_scheduler_dispatcher.py` +
  `test_scheduler_dispatcher_shutdown_cancel.py`. Implement dispatcher.

For commits 1, 5, 6, 7a, 7b: implementation and tests together (migrations
and CLI are mostly mechanical; loop integrates all pieces).

---

## 3. Per-file signature specs

### 3.1 `src/assistant/state/migrations/0003_scheduler.sql` (commit 1)

Verbatim SQL from detailed-plan §2.1.

```sql
-- 0003_scheduler.sql — phase 5 (scheduler daemon + triggers ledger)

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
```

### 3.2 `src/assistant/state/db.py` edits (commit 1) + tiny ISO helper (updated wave-2)

Insert after `_apply_v2` (~line 63):

```python
async def _apply_v3(conn: aiosqlite.Connection) -> None:
    sql = (_MIGRATIONS_DIR / "0003_scheduler.sql").read_text(encoding="utf-8")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.executescript(sql)
        await conn.execute("PRAGMA user_version = 3")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
```

Bump `SCHEMA_VERSION = 3`. Wire into `apply_schema`:

```python
    if current < 3:
        await _apply_v3(conn)
        current = 3
```

**ISO UTC round-trip helpers (wave-2 N-W2-2)** — add to
`src/assistant/scheduler/__init__.py` as module-level helpers. Single
source for every `scheduled_for ↔ datetime` conversion:

```python
from datetime import UTC, datetime

def iso_utc_z(dt: datetime) -> str:
    """Serialise UTC datetime to `YYYY-MM-DDTHH:MM:SSZ`.
    Caller guarantees `dt.tzinfo is UTC` (assert in debug builds)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

def from_iso_utc(s: str) -> datetime:
    """Parse `YYYY-MM-DDTHH:MM:SSZ` → tz-aware UTC datetime.
    Accepts trailing `Z` explicitly (Python's fromisoformat on 3.11+
    handles `Z`, but we strip+add UTC for consistency across 3.10-3.12)."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(UTC)
```

Used by `SchedulerStore.try_materialize_trigger`, `cron.is_due`, and
CLI `cmd_add`/`cmd_list` JSON serialisation. Documented in §16 invariants.

### 3.3 `src/assistant/scheduler/cron.py` (commit 2)

Public API (unchanged shape vs v1; body unchanged). Signatures:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


class CronParseError(ValueError):
    """Malformed 5-field cron expression."""


@dataclass(frozen=True, slots=True)
class CronExpr:
    minute: frozenset[int]
    hour: frozenset[int]
    day_of_month: frozenset[int]
    month: frozenset[int]
    day_of_week: frozenset[int]  # subset of 0..6 (Sun=0..Sat=6). DOW=7 REJECTED.
    raw: str


def parse_cron(expr: str) -> CronExpr:
    """Parse 5-field POSIX cron into CronExpr.

    Supported: *, integer literal, comma list, range a-b, step */n, a-b/n.
    NOT supported: @reboot, @daily, letter names (JAN, MON).
    DOW range is 0-6; 7 is INVALID (raise CronParseError).
    """


def matches_local(expr: CronExpr, dt_local: datetime) -> bool:
    """Match per POSIX: if BOTH dom and dow are non-*, match on OR."""


def is_existing_local_minute(naked: datetime, tz: ZoneInfo) -> bool:
    """DST spring-skip detector via round-trip (S-3)."""


def is_ambiguous_local_minute(naked: datetime, tz: ZoneInfo) -> bool:
    """DST fall-back detector via fold compare (S-3)."""


def is_due(
    expr: CronExpr,
    last_fire_at: datetime | None,
    now: datetime,
    tz: ZoneInfo,
    catchup_window_s: int = 3600,
) -> datetime | None:
    """Return newest UTC minute-boundary `t` that (a) matches expr in tz,
    (b) last_fire_at < t <= now, (c) fold==0 (skip DST fall duplicate),
    (d) now - t <= catchup_window_s. Iterate UTC minutes from
    max(last_fire_at+1min, now - catchup_window_s) up to now, newest first."""
```

Test files (commit 2):

- `tests/test_scheduler_cron_parser.py` — 30+ cases: `*`, lists, ranges,
  steps, combos, errors (field count, out-of-range, `*/0`, letter names).
  **Wave-2: add DOW=7 reject** + `0 9 * *` (only 4 fields) reject.
- `tests/test_scheduler_cron_semantics.py` — 30+ cases from S-9 fixture
  file (22 valid + 5 invalid; see
  `spikes/phase5_s9_cron_fixtures.json`). Load JSON, iterate, assert
  `is_due(...)` picks the LATEST fire in `(window_start, window_end]`.

### 3.4 `src/assistant/scheduler/store.py` (commit 3) — (updated wave-2)

```python
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import aiosqlite

from assistant.scheduler import iso_utc_z


class SchedulerStore:
    """aiosqlite wrapper. Shares conn AND asyncio.Lock with ConversationStore
    per plan §1.11 (S-1 confirms p99 3.4 ms)."""

    def __init__(self, conn: aiosqlite.Connection, lock: asyncio.Lock) -> None:
        self._conn = conn
        self._lock = lock

    # --- schedules CRUD ---
    async def insert_schedule(self, *, cron: str, prompt: str, tz: str = "UTC") -> int: ...
    async def count_enabled(self) -> int: ...
    async def list_schedules(self, *, enabled_only: bool = False) -> list[dict[str, Any]]: ...
    async def set_enabled(self, schedule_id: int, enabled: bool) -> bool: ...
    async def delete_schedule(self, schedule_id: int) -> bool: ...
    async def get_schedule(self, schedule_id: int) -> dict[str, Any] | None: ...
    async def iter_enabled_schedules(self) -> list[dict[str, Any]]: ...

    # --- due materialization ---
    async def try_materialize_trigger(
        self, schedule_id: int, prompt: str, scheduled_for: datetime
    ) -> int | None:
        """INSERT OR IGNORE + UPDATE last_fire_at atomically (plan §5.3).
        Returns trigger_id on INSERT; None on UNIQUE violation. S-8 atomic."""

    # --- dispatcher transitions — WAVE-2: status-precondition in WHERE ---

    async def mark_sent(self, trigger_id: int) -> bool:
        """UPDATE triggers SET status='sent', sent_at=?
           WHERE id=? AND status='pending'.
        Returns True iff rowcount==1. On False: log `scheduler_trigger_state_skew
        trigger_id=X expected=pending actual=<query>`, caller discards from
        _inflight and returns."""

    async def mark_acked(self, trigger_id: int) -> bool:
        """UPDATE ... WHERE id=? AND status='sent'. Returns True on success."""

    async def mark_pending_retry(
        self, trigger_id: int, last_error: str
    ) -> int:
        """UPDATE ... SET status='pending', attempts=attempts+1, last_error=?
           WHERE id=? AND status='sent'.
        Returns NEW attempts value (via SELECT after UPDATE inside same lock).
        If rowcount=0 (already reverted by sweep or never advanced to sent):
        log skew and return current attempts from SELECT; do NOT raise."""

    async def mark_dead(self, trigger_id: int, last_error: str) -> bool:
        """UPDATE ... WHERE id=? AND status='pending' AND attempts>=:threshold.
        (Only the pending→dead terminal.) Returns rowcount==1."""

    async def mark_dropped(self, trigger_id: int, reason: str) -> bool:
        """UPDATE ... WHERE id=? AND status IN ('pending','sent')."""

    # --- recovery ---
    async def clean_slate_sent(self) -> int:
        """Boot-only: revert ALL status='sent' → 'pending'. Call BEFORE
        dispatcher starts accepting (plan §8.2)."""

    async def revert_stuck_sent(
        self, timeout_s: int, exclude_ids: set[int]
    ) -> int:
        """Runtime sweep: revert 'sent' rows older than timeout_s that are
        NOT in exclude_ids (dispatcher's _inflight view). Plan §1.4."""

    async def recent_triggers(
        self, *, schedule_id: int | None = None, limit: int = 20
    ) -> list[dict[str, Any]]: ...

    async def count_catchup_misses(
        self,
        *,
        now: datetime,
        catchup_window_s: int = 3600,
        tz_default: str = "UTC",
    ) -> int:
        """Startup helper. For each enabled schedule, count minute-boundaries t
        such that:
          lower = COALESCE(last_fire_at, created_at)
          lower = max(lower, now - catchup_window_s * 4)   -- cap to 4×catchup
          upper = now - catchup_window_s
          lower < t <= upper AND cron matches t in schedule.tz.

        Capping lower bound (wave-2 G-W2-4) avoids counting weeks of missed
        fires on a schedule added 1 month ago but never run. Callers sum the
        result across all schedules and emit at most one recap message."""
```

Tests (`tests/test_scheduler_store.py`, 20+ cases):

- CRUD (insert/list/delete/soft-delete).
- Unique-violation idempotency.
- `last_fire_at` NOT advanced on UNIQUE violation.
- `clean_slate_sent` affects only 'sent' (not 'pending'/'acked'/'dead').
- `revert_stuck_sent` respects exclude_ids AND timeout.
- **Wave-2 G-W2-6 status-preconditions**:
  - `mark_sent` on a `dead` trigger → returns False, skew log captured.
  - `mark_acked` on a `pending` trigger → returns False, skew log.
  - `mark_pending_retry` on already-`pending` trigger → returns current
    attempts, skew log, does NOT increment twice.
  - `mark_dead` on a `dead` trigger → returns False (idempotent).
- **Wave-2 G-W2-4 catchup semantics**:
  - schedule created 10 h ago, `last_fire_at=NULL`, cron `*/30 * * * *`,
    `catchup_window_s=3600` → lower cap = now-4h; count = fires in
    (now-4h, now-1h] that match `*/30`. Expect 6 (every 30 min × 3h).
  - schedule with `last_fire_at=now-2h` under same conds → lower = now-2h;
    count = fires in (now-2h, now-1h] = 2.

### 3.5 `src/assistant/scheduler/dispatcher.py` (commit 4) — (updated wave-2)

Class signature — state:

```python
class SchedulerDispatcher:
    def __init__(self, *, queue, store, handler, adapter,
                 owner_chat_id: int, settings: Settings) -> None:
        # store queue/store/handler/adapter/settings/_owner
        self._stop = asyncio.Event()
        self._inflight: set[int] = set()
        self._recent_acked: deque[int] = deque(maxlen=256)
        self._last_tick_at: float = 0.0   # wave-2 G-W2-10 heartbeat

    def inflight(self) -> set[int]: return self._inflight.copy()
    def last_tick_at(self) -> float: return self._last_tick_at
    def stop(self) -> None: self._stop.set()

    async def run(self) -> None:
        """Drain loop. `wait_for(get, timeout=0.5)` pattern (S-5). Updates
        `_last_tick_at` both on timeout AND after delivery. Exits when
        _stop is set and queue drained, or when None (poison) is received."""

    async def _deliver(self, t: ScheduledTrigger) -> None: ...
    async def _deliver_with_handler(self, t: ScheduledTrigger) -> str: ...
    async def _notify_dead(self, trigger_id: int, err: str) -> None: ...
```

`ScheduledTrigger`:

```python
@dataclass(frozen=True, slots=True)
class ScheduledTrigger:
    trigger_id: int
    schedule_id: int
    prompt: str
    scheduled_for: datetime   # tz-aware UTC
    attempt: int              # 1-based
```

`_deliver` body — flow (pseudocode, wave-2 corrections annotated):

```
_inflight.add(t.trigger_id); try:
    if t.trigger_id in _recent_acked: log dup-skip; return
    sched = await store.get_schedule(t.schedule_id)
    if sched is None or not sched["enabled"]:
        await store.mark_dropped(t.trigger_id, "schedule_disabled"); return
    if not await store.mark_sent(t.trigger_id):           # wave-2 G-W2-6
        log scheduler_mark_sent_skew; return
    try:
        joined = await _deliver_with_handler(t)
        if joined: await adapter.send_text(owner, joined)   # retry inside adapter (G-W2-1)
        if await store.mark_acked(t.trigger_id):
            _recent_acked.append(t.trigger_id)
        else:
            log scheduler_mark_acked_skew
    except asyncio.CancelledError:
        # WAVE-2 B-W2-3: shield the DB UPDATE. Without shield it is cancelled
        # inside the scope and trigger stays 'sent' forever.
        await asyncio.shield(
            store.mark_pending_retry(t.trigger_id, last_error="shutdown_cancelled")
        )
        raise
    except Exception as exc:
        attempts = await store.mark_pending_retry(t.trigger_id, last_error=repr(exc)[:512])
        log scheduler_delivery_failed attempts=attempts
        if attempts >= dead_attempts_threshold:
            if await store.mark_dead(t.trigger_id, last_error=repr(exc)[:512]):
                await _notify_dead(t.trigger_id, repr(exc))
finally:
    _inflight.discard(t.trigger_id)
```

`_deliver_with_handler` builds `IncomingMessage(chat_id=owner, text=t.prompt,
origin="scheduler", meta={"trigger_id": t.trigger_id, "schedule_id":
t.schedule_id})`, accumulates emit chunks, returns joined stripped string.

`_notify_dead` one-shot `adapter.send_text(owner, "scheduler trigger {id}
marked dead after {N} attempts. last error: {err[:200]}")`. Wrapped in
try/except so dead-notify failure doesn't crash dispatcher.

### 3.5.1 `tests/test_scheduler_dispatcher.py` cases (8+)

1. Happy path: queue.put → consumer delivers → adapter.send_text → mark_acked + LRU contains id.
2. Handler raises → mark_pending_retry(attempts=1); id removed from _inflight.
3. `attempts >= 5` on next retry → mark_dead + dead-notify called.
4. Schedule enabled=0 between put and delivery → mark_dropped; no handler call.
5. Duplicate trigger_id in _recent_acked → skipped; _inflight cleanly discards.
6. `inflight()` reflects in-progress trigger id; cleared after finally.
7. Handler emits no text → skip adapter.send_text; still mark_acked.
8. **Wave-2 G-W2-6 mark_sent skew**: pre-seed trigger status='dead',
   put it in queue → dispatcher receives, `mark_sent` returns False,
   logs skew, discards from _inflight, no `send_text` call.

### 3.5.2 `tests/test_scheduler_dispatcher_shutdown_cancel.py` (wave-2 B-W2-3)

```python
async def test_cancelled_deliver_shielded_mark_pending():
    """Simulate: dispatcher._deliver blocked inside handler.handle, task
    is cancelled → mark_pending_retry must run to completion (shielded)."""
    # Seed trigger status='pending'. Put in queue. Patch handler.handle
    # to `await asyncio.Event().wait()` (never returns). Start dispatcher.
    # After small delay, cancel the dispatcher task. Join with shield=False.
    # Assert: trigger row has status='pending', attempts=1,
    #         last_error='shutdown_cancelled'.
```

### 3.6 `src/assistant/adapters/base.py` edit (commit 4)

```python
from typing import Any

@dataclass(frozen=True, slots=True)
class IncomingMessage:
    chat_id: int
    text: str
    message_id: int | None = None
    origin: Origin = "telegram"
    meta: dict[str, Any] | None = None   # phase-5: scheduler carries trigger_id here
```

Plus `from typing import Any`. Backward-compatible (all existing sites
omit `meta`).

### 3.7 `src/assistant/scheduler/loop.py` (commit 5) — (updated wave-2)

Class shape:

```python
class SchedulerLoop:
    _SWEEP_EVERY_N_TICKS = 4   # wave-2 B-W2-1: sweep @ 60 s (15 s × 4)

    def __init__(self, *, queue, store, dispatcher, settings, notify_fn=None):
        # store all + self._stop = asyncio.Event()
        # self._tick_count = 0; self._last_tick_at: float = 0.0

    def stop(self) -> None: self._stop.set()
    def last_tick_at(self) -> float: return self._last_tick_at

    async def run(self) -> None: ...
    async def _tick(self) -> None: ...
    async def count_catchup_misses(self) -> int: ...
```

`run()` — outer try/except (GAP #15 notify); inner per-tick try/except
catches warn-log+continue; after each tick update `_last_tick_at =
asyncio.get_running_loop().time()`; sleep via `wait_for(self._stop.wait(),
timeout=tick_interval_s)` so stop wakes it immediately. On fatal
`_notify(f"scheduler loop crashed: {exc!r}")` then re-raise.

`_tick()` — one iteration:

1. `self._tick_count += 1`; `now = datetime.now(UTC)`.
2. `schedules = await store.iter_enabled_schedules()`.
3. For each row:
   - `parse_cron(row["cron"])` → `continue` + warn `scheduler_cron_parse_failed` on error.
   - `ZoneInfo(row["tz"])` → `continue` + warn `scheduler_tz_invalid` on error.
   - `last = from_iso_utc(row["last_fire_at"])` if set else None.
   - `t = is_due(expr, last, now, tz, catchup_window_s=settings.scheduler.catchup_window_s)`.
   - If `t is None`: skip.
   - `trigger_id = await store.try_materialize_trigger(row["id"], row["prompt"], t)`;
     if None: skip (raced). Else `await queue.put(ScheduledTrigger(trigger_id, row["id"], row["prompt"], t, attempt=1))`.
4. **Wave-2 B-W2-1 revert sweep** every `_SWEEP_EVERY_N_TICKS`:
   `reverted = await store.revert_stuck_sent(timeout_s=settings.scheduler.sent_revert_timeout_s, exclude_ids=dispatcher.inflight())`;
   if reverted: `log.info("scheduler_revert_stuck_sent", count=reverted)`.

`count_catchup_misses()` wraps `store.count_catchup_misses(now=datetime.now(UTC),
catchup_window_s=settings.scheduler.catchup_window_s,
tz_default=settings.scheduler.tz_default)`. Called once at boot
(§3.16.3).

### 3.7.1 Tests `tests/test_scheduler_loop.py` (10+ cases)

1. Single schedule `*/5 * * * *`, initial tick → 1 trigger materialized.
2. Three consecutive ticks within same minute → 1 trigger.
3. Long gap (1h, `*/5`) → 1 trigger at latest match (catchup).
4. Long gap (6h, `catchup_window_s=3600`) → None + `scheduler_catchup_miss` log.
5. Queue full (maxsize=3, 5 schedules all due) → producer awaits on put.
6. Disabled schedule → skipped.
7. Malformed cron in DB → log `scheduler_cron_parse_failed`; loop continues.
8. Invalid tz in DB → log `scheduler_tz_invalid`; loop continues.
9. Outermost exception → `notify_fn` called; loop re-raises.
10. Fake clock advance, run N ticks, assert N triggers for `*/1 * * * *`.

### 3.7.2 `tests/test_scheduler_runtime_revert_sweep.py` (wave-2 B-W2-1)

```python
async def test_runtime_sweep_reverts_stuck_sent_not_in_inflight():
    """
    Seed: trigger status='sent', sent_at = now - 10min (> 360s timeout).
          dispatcher.inflight() returns empty set.
    Act:  drive loop for >= _SWEEP_EVERY_N_TICKS ticks (use fake clock).
    Assert:
      - trigger reverted to 'pending', attempts incremented.
      - re-materialized on next producer tick.
      - dispatcher eventually delivers (end-to-end).
    """

async def test_runtime_sweep_skips_inflight():
    """
    Seed: trigger status='sent', sent_at = now - 10min.
          dispatcher.inflight() returns {that_id}.
    Act:  drive loop for >= _SWEEP_EVERY_N_TICKS ticks.
    Assert: trigger STILL status='sent' (protected by exclude_ids).
    """
```

### 3.8 `src/assistant/config.py` edit (commit 5) — (updated wave-2)

```python
class SchedulerSettings(BaseSettings):
    """Scheduler knobs (phase 5)."""

    model_config = SettingsConfigDict(
        env_prefix="SCHEDULER_",
        env_file=(str(_user_env_file()), ".env"),
        extra="ignore",
    )

    enabled: bool = True
    tick_interval_s: int = 15
    tz_default: str = "UTC"
    catchup_window_s: int = 3600
    dead_attempts_threshold: int = 5
    sent_revert_timeout_s: int = 360
    dispatcher_queue_size: int = 64
    max_schedules: int = 64
    # WAVE-2 N-W2-4: split single cooldown into two distinct concerns.
    loop_crash_cooldown_s: int = 86400     # 24h for `scheduler_loop_fatal` notify.
    catchup_recap_cooldown_s: int = 3600   # 1h for "missed N reminders" recap.
    # WAVE-2 G-W2-10: heartbeat staleness multiplier.
    heartbeat_stale_multiplier: int = 10   # tick_interval_s × 10 = 150s default.
```

Add to `Settings`:

```python
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
```

### 3.9 `tools/schedule/main.py` (commit 6) — (updated wave-2)

Stdlib-only argparse CLI.

```python
"""schedule CLI — 0xone-assistant scheduler management.

Stdlib-only. Exits:
  0  ok
  2  usage
  3  validation / cap-reached
  4  I/O
  7  not-found
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
# WAVE-2 G-W2-9: use .append, NOT .insert(0) — avoid shadowing (phase-4 lesson).
# Full `_memlib`-style consolidation deferred to phase 6 (detailed-plan §13 item 4).
if str(_ROOT / "src") not in sys.path:
    sys.path.append(str(_ROOT / "src"))

from assistant.scheduler.cron import CronParseError, parse_cron   # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VAL = 3
EXIT_IO = 4
EXIT_NOT_FOUND = 7

_DEFAULT_MAX = 64
_MAX_PROMPT_BYTES = 2048
# WAVE-2 B-W2-4: `_TZ_RE` REMOVED. `ZoneInfo(args.tz)` is the sole authority
# (spike S-10: accepts Etc/GMT+3, CST6CDT; rejects ../../etc/passwd via
# ValueError, Europe/NotACity via ZoneInfoNotFoundError).

def _data_dir() -> Path: ...
def _db_path() -> Path: ...
def _connect() -> sqlite3.Connection: ...
def _ok(data: dict[str, Any]) -> int: ...
def _fail(code: int, error: str, **extra: Any) -> int: ...


def cmd_add(args) -> int:
    # 1. Validate cron.
    try:
        parse_cron(args.cron)
    except CronParseError as exc:
        return _fail(EXIT_VAL, f"cron parse: {exc}")

    # 2. Validate tz — stdlib is sole authority (wave-2 B-W2-4, spike S-10).
    try:
        ZoneInfo(args.tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # Both error types occur: ZoneInfoNotFoundError on fake names,
        # ValueError on injection (../../etc/passwd) and empty string.
        return _fail(EXIT_VAL, f"unknown tz: {args.tz!r} ({exc})")

    # 3. Validate prompt.
    if not args.prompt.strip():
        return _fail(EXIT_VAL, "prompt must be non-empty")
    if len(args.prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        return _fail(EXIT_VAL, f"prompt exceeds {_MAX_PROMPT_BYTES} bytes")
    for ch in args.prompt:
        if ord(ch) < 0x20 and ch not in "\t\n":
            return _fail(EXIT_VAL, f"control char U+{ord(ch):04X} not allowed")

    # 4. Cap check (GAP #11).
    cap = int(os.environ.get("SCHEDULER_MAX_SCHEDULES") or _DEFAULT_MAX)
    conn = _connect()
    try:
        cur = conn.execute("SELECT COUNT(*) FROM schedules WHERE enabled=1")
        (n,) = cur.fetchone()
        if n >= cap:
            return _fail(EXIT_VAL, "scheduler_schedule_cap_reached", cap=cap)
        cur = conn.execute(
            "INSERT INTO schedules(cron, prompt, tz, enabled) VALUES (?, ?, ?, 1)",
            (args.cron, args.prompt, args.tz),
        )
        conn.commit()
        return _ok({"id": cur.lastrowid, "cron": args.cron,
                    "prompt": args.prompt, "tz": args.tz})
    finally:
        conn.close()


def cmd_list(args) -> int: ...
def cmd_rm(args) -> int: ...
def cmd_enable(args) -> int: ...
def cmd_disable(args) -> int: ...
def cmd_history(args) -> int: ...

def _build_parser() -> argparse.ArgumentParser: ...

def main(argv: list[str] | None = None) -> int:
    p = _build_parser()
    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
```

Use `sqlite3` with `PRAGMA busy_timeout=5000` and `PRAGMA journal_mode=WAL`.
CLI never writes to `triggers`.

### 3.10 `src/assistant/bridge/hooks.py` edit (commit 6) — (updated wave-2)

Add `_validate_schedule_argv`. Wave-2 additions:

- Duplicate-flag detection (B-W2-5): track `seen_flags: set[str]` in
  each flag-parsing loop; return deny on dup.
- Positional count enforcement (rm/enable/disable): max 1.
- `--tz VALUE` — only enforce `len(VALUE) ≤ 64`; structural check is
  CLI's job (wave-2 B-W2-4, spike S-10).

```python
_SCHEDULE_SUBCMDS: frozenset[str] = frozenset({
    "add", "list", "rm", "enable", "disable", "history",
})


def _validate_schedule_argv(args: list[str]) -> str | None:
    if not args:
        return "schedule CLI requires a subcommand"
    sub = args[0]
    if sub not in _SCHEDULE_SUBCMDS:
        return f"schedule subcommand {sub!r} not allowed"

    remaining = args[1:]

    if sub == "add":
        allowed_flags = {"--cron", "--prompt", "--tz"}
        seen: set[str] = set()
        i = 0
        while i < len(remaining):
            tok = remaining[i]
            if tok not in allowed_flags:
                return f"schedule add: flag {tok!r} not allowed"
            if tok in seen:
                return f"schedule add: duplicate flag {tok!r}"
            seen.add(tok)
            if i + 1 >= len(remaining):
                return f"schedule add: flag {tok} requires a value"
            val = remaining[i + 1]
            if tok == "--prompt" and len(val.encode("utf-8")) > 2048:
                return "schedule add: --prompt exceeds 2048 bytes"
            if tok == "--tz" and len(val) > 64:
                return "schedule add: --tz exceeds 64 chars"
            i += 2
        return None

    if sub == "list":
        seen = set()
        for tok in remaining:
            if tok != "--enabled-only":
                return f"schedule list: unknown flag {tok!r}"
            if tok in seen:
                return f"schedule list: duplicate flag {tok!r}"
            seen.add(tok)
        return None

    if sub in ("rm", "enable", "disable"):
        if len(remaining) != 1:
            return f"schedule {sub}: exactly one positional ID required"
        try:
            int(remaining[0])
        except ValueError:
            return f"schedule {sub}: ID must be integer"
        return None

    if sub == "history":
        allowed = {"--schedule-id", "--limit"}
        seen = set()
        i = 0
        while i < len(remaining):
            tok = remaining[i]
            if tok not in allowed:
                return f"schedule history: flag {tok!r} not allowed"
            if tok in seen:
                return f"schedule history: duplicate flag {tok!r}"
            seen.add(tok)
            if i + 1 >= len(remaining):
                return f"schedule history: flag {tok} needs a value"
            try:
                int(remaining[i + 1])
            except ValueError:
                return f"schedule history: {tok} requires integer"
            i += 2
        return None

    return f"schedule subcommand {sub!r} missing validator"
```

Tests `tests/test_scheduler_bash_hook_allowlist.py` (15+ cases):

```python
# allow
'python tools/schedule/main.py add --cron "0 9 * * *" --prompt "ping"'
'python tools/schedule/main.py add --cron "0 9 * * *" --prompt "p" --tz "Europe/Berlin"'
'python tools/schedule/main.py add --cron "0 9 * * *" --prompt "p" --tz "Etc/GMT+3"'  # wave-2 B-W2-4
'python tools/schedule/main.py list'
'python tools/schedule/main.py list --enabled-only'
'python tools/schedule/main.py rm 42'
'python tools/schedule/main.py history --schedule-id 1 --limit 10'

# deny — shell metachars (handled by existing bash guard)
'python tools/schedule/main.py add --cron "0 9 * * *" --prompt "$(cat /etc/passwd)"'
'python tools/schedule/main.py add --cron "0 9 * * *" --prompt "`whoami`"'
'python tools/schedule/main.py add --cron a --prompt x | ls'
'python tools/schedule/main.py add --cron a --prompt x ; rm -rf /'

# deny — validator
'python tools/schedule/main.py rm abc'
'python tools/schedule/main.py unknown'
# WAVE-2 B-W2-5 — dup flag
'python tools/schedule/main.py add --cron "0 9 * * *" --cron "0 10 * * *" --prompt x'
'python tools/schedule/main.py add --prompt "x" --prompt "y"'
# WAVE-2 B-W2-5 — positional count
'python tools/schedule/main.py rm 1 2 3'
# prompt length
'python tools/schedule/main.py add --cron "0 9 * * *" --prompt "' + "x" * 2049 + '"'
# --tz too long
'python tools/schedule/main.py add --cron "0 9 * * *" --prompt p --tz "' + "x" * 65 + '"'
```

### 3.11 `src/assistant/adapters/telegram.py` edit (commit 7a) — (new wave-2 G-W2-1)

Wrap `send_text` with `TelegramRetryAfter` retry loop. Benefits ALL callers
(phase-2 user reply AND scheduler dispatch). Placement: inside adapter
(cleanest — the retry policy is a Telegram concern, not a dispatcher one).

```python
from aiogram.exceptions import TelegramRetryAfter

TELEGRAM_RETRY_AFTER_MAX_ATTEMPTS = 2   # after first hit, 2 retries → 3 tries total

async def send_text(self, chat_id: int, text: str) -> None:
    """Send text to Telegram. Handles TelegramRetryAfter by sleeping
    `retry_after + 1` and retrying up to MAX_ATTEMPTS times per chunk.

    WAVE-2 G-W2-1: do NOT let TelegramRetryAfter propagate — the scheduler
    dispatcher's `except Exception` would otherwise burn a whole Claude
    turn for a 3-second rate-limit window.
    """
    for part in split_for_telegram(text):
        for attempt in range(TELEGRAM_RETRY_AFTER_MAX_ATTEMPTS + 1):
            try:
                await self._bot.send_message(chat_id=chat_id, text=part)
                break
            except TelegramRetryAfter as exc:
                if attempt >= TELEGRAM_RETRY_AFTER_MAX_ATTEMPTS:
                    log.warning(
                        "telegram_retry_after_exhausted",
                        chat_id=chat_id,
                        retry_after=exc.retry_after,
                        attempts=attempt + 1,
                    )
                    raise
                log.info(
                    "telegram_retry_after",
                    chat_id=chat_id,
                    retry_after=exc.retry_after,
                    attempt=attempt + 1,
                )
                await asyncio.sleep(exc.retry_after + 1)
```

Tests `tests/test_telegram_adapter_send_text.py`:

- Monkey-patch `Bot.send_message` to raise `TelegramRetryAfter(retry_after=3)`
  on first call, succeed on second → assert one logical `send_text`
  completes, sleep observed ≥ 3s (use fake clock or patched `asyncio.sleep`).
- Raise `TelegramRetryAfter(1)` on 3 consecutive calls → fourth attempt
  gated by MAX_ATTEMPTS=2 → `TelegramRetryAfter` propagates out; log line
  `telegram_retry_after_exhausted` captured.
- Normal send (no RetryAfter) → unchanged path, one `send_message` call.

### 3.12 `src/assistant/handlers/message.py` edit (commit 7b)

Phase-5 scheduler-origin branch + URL-note (S-7 FIFO order):

```python
notes: list[str] = []
if msg.origin == "scheduler":
    trigger_id = None
    if msg.meta is not None:
        trigger_id = msg.meta.get("trigger_id")
    notes.append(
        f"autonomous turn from scheduler id={trigger_id}; "
        "owner is not active; do not ask clarifying questions, "
        "answer proactively and finish."
    )
    log.info(
        "scheduler_turn_started",
        turn_id=turn_id,
        chat_id=msg.chat_id,
        trigger_id=trigger_id,
    )

urls = _detect_urls(msg.text)
if urls:
    notes.append(
        f"user message contains URL(s): {urls!r}. "
        "If the user appears to want a skill installed, run "
        "`python tools/skill-installer/main.py preview <URL>` "
        "first; otherwise reply as usual."
    )
    log.info("url_detected", chat_id=msg.chat_id, turn_id=turn_id, urls=urls)

system_notes = notes or None
```

Test `tests/test_scheduler_origin_branch_e2e.py`:
- `IncomingMessage(chat_id=OWNER, text="summary", origin="scheduler", meta={"trigger_id": 42})`.
- Monkey-patch `ClaudeBridge.ask` to capture `system_notes`.
- Run `handler.handle(msg, emit)` with stub emit.
- Assert `system_notes[0]` contains `"id=42"` and the text "scheduler".
- Variant with URL: assert `system_notes == [scheduler_note, url_note]`.

### 3.13 `src/assistant/bridge/claude.py` edit (commit 7b, docstring only)

Add to `ask()` docstring:

```
Phase 5 convention: when `origin="scheduler"` is the turn trigger,
callers put the scheduler-note FIRST and URL-note SECOND. The bridge
merges in order; see spike-findings S-7 for verification.
```

### 3.14 `src/assistant/bridge/history.py` — DEFERRED to phase 6 (wave-2 G-W2-8)

Phase-4 tech-debt #7 (history total snippet cap) is **deferred to phase 6**.

Rationale: correctly threading a mutable budget through `_render_tool_summary`
requires changes across `history_to_user_envelopes` and every call-site
(3+) plus cross-turn test scenarios. Phase 5 scheduler-injected turns use
the SAME history-envelope pipeline as user-turns (B4 branch is system-note
only, does NOT alter history composition), so the OOM risk does not increase
over phase 4.

Decision recorded in `detailed-plan.md` §13 item 8.

### 3.15 `src/assistant/bridge/system_prompt.md` edit (commit 7b)

Append:

```
### Scheduler-initiated turns

Если turn помечен как `origin="scheduler"` (видишь это в системном
system-note первого сообщения), владелец не активен. Не задавай уточняющих
вопросов. Работай проактивно: выполни задачу, запиши результат в vault
если нужно, и завершай. Ответ попадёт владельцу в Telegram напрямую.
```

### 3.16 `src/assistant/main.py` edit (commit 7b) — (updated wave-2)

**3.16.1 Imports** (top of file):

```python
import fcntl
import time

from assistant.scheduler.dispatcher import SchedulerDispatcher
from assistant.scheduler.loop import SchedulerLoop
from assistant.scheduler.store import SchedulerStore
```

**3.16.2 pidfile mutex** — new method (wave-2 N-W2-5: mode `0o600`):

```python
    def _acquire_pid_lock_or_exit(self) -> None:
        """Advisory flock on `<data_dir>/run/daemon.pid` (plan §1.13, S-4).
        On BlockingIOError: log `daemon_already_running` and exit 0.
        On success: write PID, keep fd on `self._pid_fd`."""
        pid_dir = self._settings.data_dir / "run"
        pid_dir.mkdir(parents=True, exist_ok=True)
        pid_path = pid_dir / "daemon.pid"
        # WAVE-2 N-W2-5: 0o600 — file may contain pid info only, still
        # user-readable by convention in /run/ but unreadable to others.
        fd = os.open(str(pid_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._log.warning("daemon_already_running", pid_path=str(pid_path))
            os.close(fd)
            sys.exit(0)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._pid_fd = fd
```

Add to `__init__`: `self._pid_fd: int | None = None`,
`self._scheduler_loop: SchedulerLoop | None = None`,
`self._scheduler_dispatcher: SchedulerDispatcher | None = None`.

**3.16.3 `Daemon.start()` edits** — replace the block after
`await apply_schema(self._conn)`:

```python
        conv = ConversationStore(self._conn)
        # WAVE-2 N-W2-1 VERIFIED: ConversationStore exposes `.lock`
        # property (state/conversations.py:26-27).
        turns = TurnStore(self._conn, lock=conv.lock)
        sched_store = SchedulerStore(self._conn, lock=conv.lock)

        # Turn-sweep — safe because flock guarantees single daemon.
        swept = await turns.sweep_pending()
        if swept:
            self._log.warning("startup_swept_pending_turns", count=swept)

        # Clean-slate: all status='sent' → 'pending'. Runs BEFORE dispatcher
        # starts accepting (so _inflight is still empty).
        reverted = await sched_store.clean_slate_sent()
        if reverted:
            self._log.info("scheduler_clean_slate_revert", count=reverted)

        # ... existing gh-missing warning ...

        bridge = ClaudeBridge(self._settings)
        self._adapter = TelegramAdapter(self._settings)
        handler = ClaudeHandler(self._settings, conv, turns, bridge)
        self._adapter.set_handler(handler)
        await self._adapter.start()

        queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=self._settings.scheduler.dispatcher_queue_size
        )
        dispatcher = SchedulerDispatcher(
            queue=queue,
            store=sched_store,
            handler=handler,
            adapter=self._adapter,
            owner_chat_id=self._settings.owner_chat_id,
            settings=self._settings,
        )
        loop_ = SchedulerLoop(
            queue=queue,
            store=sched_store,
            dispatcher=dispatcher,
            settings=self._settings,
            notify_fn=self._scheduler_loop_notify,
        )
        self._scheduler_dispatcher = dispatcher
        self._scheduler_loop = loop_
        self._spawn_bg(dispatcher.run(), name="scheduler_dispatcher")
        self._spawn_bg(loop_.run(), name="scheduler_loop")

        # WAVE-2 G-W2-10: health-check task (60s cadence).
        self._spawn_bg(self._scheduler_health_check_bg(), name="scheduler_health")

        self._spawn_bg(self._sweep_run_dirs(), name="sweep_run_dirs")
        self._spawn_bg(self._bootstrap_skill_creator_bg(), name="skill_creator_bootstrap")

        # Catchup recap (GAP #16, wave-2 G-W2-4 corrected).
        missed = await loop_.count_catchup_misses()
        if missed > 0:
            self._spawn_bg(self._scheduler_catchup_recap(missed), name="scheduler_catchup_recap")

        self._log.info("daemon_started", owner=self._settings.owner_chat_id)
```

**3.16.4 `Daemon.stop()` — FULL CORRECTED BODY (wave-2 B-W2-2)**

Current order (src/assistant/main.py:401-436) calls `adapter.stop()`
FIRST, then drains bg-tasks. That's wrong: a scheduler-dispatcher mid-
`_deliver` would `await adapter.send_text(...)` AFTER adapter session
closed → session-closed error → dispatcher marks pending_retry →
another turn wasted on next boot.

Correct order: signal scheduler, drain bg-tasks (so in-flight delivery
finishes, including final `send_text`), THEN stop adapter, THEN DB, THEN
pidfile.

Replace lines 401-436 with the following (paste verbatim):

```python
    async def stop(self) -> None:
        self._log.info("daemon_stopping")

        # WAVE-2 B-W2-2 step 1: signal scheduler FIRST so drain can progress.
        # stop() on both loop and dispatcher is non-blocking (sets events).
        if self._scheduler_loop is not None:
            try:
                self._scheduler_loop.stop()
            except Exception:
                self._log.warning("stop_step_failed", step="scheduler_loop", exc_info=True)
        if self._scheduler_dispatcher is not None:
            try:
                self._scheduler_dispatcher.stop()
            except Exception:
                self._log.warning("stop_step_failed", step="scheduler_dispatcher", exc_info=True)

        # WAVE-2 B-W2-2 step 2: drain bg-tasks. In-flight scheduler
        # delivery completes HERE — including its final adapter.send_text.
        # Timeout shields us from hung tasks; cancelled tasks hit the
        # dispatcher's CancelledError branch (shielded mark_pending_retry,
        # wave-2 B-W2-3) so state never leaks.
        if self._bg_tasks:
            self._log.info("daemon_draining_bg_tasks", count=len(self._bg_tasks))
            pending = list(self._bg_tasks)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=_STOP_DRAIN_TIMEOUT_S,
                )
            except TimeoutError:
                self._log.warning(
                    "daemon_bg_drain_timeout",
                    count=len([t for t in pending if not t.done()]),
                )
                for t in pending:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        # WAVE-2 B-W2-2 step 3: stop adapter. Must run AFTER bg-tasks drain
        # so in-flight scheduler delivery can send its Telegram reply.
        if self._adapter is not None:
            try:
                await self._adapter.stop()
            except Exception:
                self._log.warning("stop_step_failed", step="adapter", exc_info=True)

        # Step 4: close DB.
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                self._log.warning("stop_step_failed", step="db", exc_info=True)

        # Step 5: release pidfile flock (fd close).
        if self._pid_fd is not None:
            try:
                os.close(self._pid_fd)
            except OSError:
                pass
            self._pid_fd = None

        self._log.info("daemon_stopped")
```

**3.16.5 Notify helpers** (wave-2 N-W2-4 split cooldowns):

```python
    async def _scheduler_loop_notify(self, msg: str) -> None:
        """Cooldown-gated Telegram notify for scheduler_loop_fatal (GAP #15).
        Uses `loop_crash_cooldown_s` (default 24h) via marker-file."""
        marker = self._settings.data_dir / "run" / ".scheduler_loop_notified"
        await self._notify_with_marker(
            marker,
            cooldown_s=self._settings.scheduler.loop_crash_cooldown_s,
            msg=msg,
        )

    async def _scheduler_catchup_recap(self, missed: int) -> None:
        """One Russian-language Telegram message (GAP #16).
        Uses `catchup_recap_cooldown_s` (default 1h)."""
        marker = self._settings.data_dir / "run" / ".scheduler_catchup_recap"
        await self._notify_with_marker(
            marker,
            cooldown_s=self._settings.scheduler.catchup_recap_cooldown_s,
            msg=f"пока система спала, пропущено {missed} напоминаний.",
        )

    async def _notify_with_marker(
        self, marker: Path, cooldown_s: int, msg: str, *, bypass: bool = False
    ) -> None:
        """Marker-file cooldown pattern (mirrors _bootstrap_notify_failure).
        `bypass=True`: ignore cooldown (used by health-check on stale heartbeat
        — wave-2 G-W2-10)."""
        if not bypass and marker.exists():
            try:
                age = time.time() - marker.stat().st_mtime
                if age < cooldown_s:
                    return
            except OSError:
                pass
        try:
            await self._adapter.send_text(self._settings.owner_chat_id, msg)
            marker.touch()
        except Exception:
            self._log.warning("scheduler_notify_failed", exc_info=True)

    async def _scheduler_health_check_bg(self) -> None:
        """WAVE-2 G-W2-10: detect silent scheduler-loop death.
        Re-notify BYPASSING cooldown (the user must know NOW)."""
        check_interval_s = 60.0
        stale_mult = self._settings.scheduler.heartbeat_stale_multiplier
        tick_s = self._settings.scheduler.tick_interval_s
        stale_threshold_s = tick_s * stale_mult   # 15 × 10 = 150s default

        marker = self._settings.data_dir / "run" / ".scheduler_loop_notified"
        while True:
            try:
                await asyncio.sleep(check_interval_s)
                if self._scheduler_loop is None:
                    continue
                now_loop = asyncio.get_running_loop().time()
                last = self._scheduler_loop.last_tick_at()
                # last == 0.0 before first tick; tolerate startup.
                if last == 0.0:
                    continue
                age = now_loop - last
                if age > stale_threshold_s:
                    self._log.error(
                        "scheduler_heartbeat_stale",
                        age_s=age,
                        threshold_s=stale_threshold_s,
                    )
                    await self._notify_with_marker(
                        marker,
                        cooldown_s=self._settings.scheduler.loop_crash_cooldown_s,
                        msg=f"scheduler loop heartbeat stale ({int(age)}s since last tick)",
                        bypass=True,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.warning("scheduler_health_check_failed", exc_info=True)
```

### 3.17 `docs/ops/launchd.plist.example` + `scheduler.service.example` (commit 7b)

Templates for phase-8 out-of-process adoption:

- `launchd.plist.example`: `KeepAlive`, `ExitTimeout`, WatchPaths.
- `scheduler.service.example`: systemd `Restart=on-failure`,
  `WorkingDirectory`, `ExecStart=/usr/bin/env python -m assistant.main`.

Non-executable. Not wired in phase 5.

---

## 4. Daemon integration tests (commit 7b)

`tests/test_scheduler_recovery.py` (6+ cases):

1. `_acquire_pid_lock_or_exit` — subprocess holds lock; main exits 0.
2. SIGKILL subprocess → second start acquires lock.
3. `clean_slate_sent`: seed 3 sent + 1 pending + 1 acked. Call. Assert:
   rowcount=3, sent rows now pending+attempts++, others untouched.
4. `revert_stuck_sent`: seed sent age=400s, inflight={} → reverts.
   Same row with inflight={its_id} → untouched.
5. LRU dedup across restart: fresh dispatcher (empty LRU) replay same
   ScheduledTrigger → `_deliver` calls handler again (at-least-once).
6. Boot-time flock file exists (mode=0o600, wave-2 N-W2-5), not held →
   start succeeds; file content is daemon PID.

`tests/test_scheduler_origin_branch_e2e.py` — §3.12 above.

`tests/test_db_migrations_v3.py`:
- Apply v2 then v3; `PRAGMA user_version` → 3.
- Insert schedule + trigger; verify FK cascade on DELETE FROM schedules.
- UNIQUE(schedule_id, scheduled_for) via double-INSERT → `IntegrityError`.

---

## 5. Pre-flight test audit

Commit 4 touches `IncomingMessage`. Audit:

```bash
grep -Rn "IncomingMessage(" tests/ src/
```

Expected: all existing callsites use keyword args or 4 positional.
Adding `meta=None` is backward-compatible.

Commit 7b changes `handlers/message.py::_run_turn`. Run:

```bash
uv run pytest tests/test_handler_chat_lock.py tests/test_handler_meta_propagation.py -x
```

---

## 6. Full test order (after each commit)

```bash
uv run pytest -x
just lint
```

E2E manual-smoke per plan §Критерии готовности:

1. `python tools/schedule/main.py add --cron "*/5 * * * *" --prompt "ping"`
   → wait ≤ 300 s → Telegram message arrives.
2. Restart Daemon between sent/acked via `SCHEDULER_DEBUG_PRE_ACK_DELAY_S=30`
   + SIGTERM mid-delivery; clean-slate + LRU prevent third duplicate.
3. `rm 1` then `history --schedule-id 1` → enabled=0, triggers visible.
4. `add --cron "invalid" --prompt x` → exit 3 JSON error.
5. `add ... --tz "Etc/GMT+3" ...` → **wave-2 B-W2-4**: accepted (previously
   would have been rejected by `_TZ_RE`).
6. `add ... --tz "../../etc/passwd"` → exit 3 with `ValueError` in message.
7. Bash hook rejects `--prompt "$(cat /etc/passwd)"` AND
   `--cron X --cron Y` (wave-2 B-W2-5).
8. Concurrent user-turn + scheduler-trigger → serialised via per-chat lock.
9. Kill scheduler-loop (test hook: raise in `_tick`) → heartbeat
   health-check fires within 150s (wave-2 G-W2-10).

---

## 7. Risk matrix impact from spikes

| Plan §14 risk | Spike outcome | Adjustment |
|---------------|---------------|------------|
| #1 dup delivery | S-8 atomicity; LRU+clean-slate; wave-2 SQL precond (G-W2-6) | tighter |
| #3 DST spring/fall | S-3; precise helper | no change |
| #7 dispatcher crash → inflight inconsistent | S-5 stop_event; wave-2 B-W2-3 shield | tighter |
| #8 queue full backpressure | S-5 await-block | no change |
| #11 scheduler-loop fatal | wave-2 G-W2-10 heartbeat + health-check | tighter |
| #13 two daemons race | S-4 flock | no change |
| #16 `last_fire_at` ambiguous | S-8 + is_due; wave-2 G-W2-4 `count_catchup_misses` cap | tighter |
| — (new) rate-limit burns turn | wave-2 G-W2-1 adapter retry | covered |

---

## 8. Open questions for devil wave-3 (minor)

1. **LRU size vs unique trigger_id lifetime.** 256 slots × one user's
   trigger stream → cold-start after 256 triggers collides the LRU.
   DB-status sentinel still protects; LRU is an extra net. Confirm OK.
2. **`count_catchup_misses` 4×window cap.** Hard-coded multiplier (4).
   A schedule added 30 days ago with `* * * * *` would see `4 × 3600 /
   60 = 240` missed fires recap-reported. Excessive? Alternative: cap
   to the cron's own derived period (e.g. daily schedule → 1 miss) —
   but that requires is_due-style compute. Current 4×-cap is a pragmatic
   ceiling; revisit if user reports noise.
3. **Heartbeat health-check uses event-loop time**, not wall time.
   A suspended laptop pauses event loop → `last_tick_at` and `now_loop`
   pause together; no false-positive on wake. Good. Confirm.

---

## 9. Known gotchas (summary)

All itemised in §0 pitfall box. Quick scan:

1. `put_nowait(POISON)` raises `QueueFull` — use `stop_event + wait_for`.
2. `fcntl.flock` without `LOCK_NB` — deadlocks.
3. `revert_stuck_sent` MUST exclude `_inflight` (B2).
4. Single aiosqlite connection is sufficient (S-1).
5. **`asyncio.shield` the `mark_pending_retry` on `CancelledError`** (wave-2).
6. `INSERT OR IGNORE` + `UPDATE last_fire_at` in ONE lock block.
7. Use `is_due` — don't iterate minutes in the producer.
8. One cron parser — `src/assistant/scheduler/cron.py`.
9. Store tz-aware datetimes as ISO-8601 `...Z` strings via helpers in §3.2.
10. Cron DOW Sun=0; Python weekday Mon=0 — convert. DOW=7 REJECTED.
11. Handler communicates via `emit` only; dispatcher calls send_text.
12. `IncomingMessage.meta` doesn't exist pre-§3.6 edit.
13. `TelegramRetryAfter` caught inside adapter (wave-2).
14. `_TZ_RE` is DROPPED — `ZoneInfo` is the sole tz authority (wave-2).
15. `mark_*` SQL MUST include `WHERE status IN (...)` precondition (wave-2).

---

## 10. References

- Spike reports: `spikes/phase5_s{1..10}_report.json`,
  `spikes/phase5_s9_cron_fixtures.json`.
- PEP 495 fold semantics: https://peps.python.org/pep-0495/
- POSIX cron 5-field spec: `man 5 crontab`.
- aiosqlite WAL busy_timeout: phase-2 `state/db.py` + tests.
- `claude-agent-sdk` 0.1.59 system-notes merging: `bridge/claude.py::ask`
  (unchanged from phase 3; see spike S-7).
- aiogram `TelegramRetryAfter`: exception carries `retry_after: int`
  attribute (seconds). Raised by Telegram Bot API on 429. See
  `aiogram.exceptions.TelegramRetryAfter`.

---

## 11. Phase-5 wave-2 traceability

| Wave-2 item | Section(s) | Type |
|---|---|---|
| B-W2-1 runtime revert sweep | §3.7, §3.7.2 | test + code |
| B-W2-2 stop() order | §3.16.4 | code |
| B-W2-3 shield CancelledError | §0 #5, §3.5, §3.5.2 | code + test |
| B-W2-4 drop `_TZ_RE` | §0 #14, §3.9, §3.10, spike S-10 | code + spike |
| B-W2-5 dup-flag Bash allowlist | §3.10 | code + test |
| G-W2-1 TelegramRetryAfter | §0 #13, §3.11 | code + test |
| G-W2-4 count_catchup_misses cap | §3.4 | code + test |
| G-W2-6 status-precondition SQL | §0 #15, §3.4, §3.5.1 | code + test |
| G-W2-8 HISTORY cap | §3.14, detailed-plan §13 | defer |
| G-W2-9 `_memlib` refactor | §0 #8, §3.9, detailed-plan §13 | defer |
| G-W2-10 heartbeat health-check | §3.7, §3.8, §3.16.5 | code |
| G-W2-3 fixture expansion | spike S-9, §3.3 | spike |
| N-W2-1 ConversationStore.lock | §3.16.3 | verify |
| N-W2-2 ISO helpers | §3.2 | code |
| N-W2-4 split cooldowns | §3.8, §3.16.5 | code |
| N-W2-5 pidfile mode 0o600 | §3.16.2 | code |
| N-W2-6 commit split | §1 commit table (7 → 7a + 7b) | ops |
