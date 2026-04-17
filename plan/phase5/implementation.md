# Phase 5 — Implementation (spike-verified, 2026-04-17)

## Revision history

- **v1** (2026-04-17): initial after spikes S-1..S-9. Empirical answers
  backed by:
  - `spikes/phase5_s1_aiosqlite_contention.py` — contention under load.
  - `spikes/phase5_s2_adapter_send_text.py` — adapter call from bg task.
  - `spikes/phase5_s3_zoneinfo_dst.py` — DST spring/fall semantics.
  - `spikes/phase5_s4_flock_semantics.py` — flock lifecycle on macOS.
  - `spikes/phase5_s5_queue_backpressure.py` — asyncio.Queue shutdown.
  - `spikes/phase5_s6_incoming_message_shape.py` — dataclass fields.
  - `spikes/phase5_s7_system_notes_order.py` — note merge order.
  - `spikes/phase5_s8_try_materialize_atomicity.py` — INSERT+UPDATE atomicity.
  - `spikes/phase5_s9_cron_fixtures.json` — cron test-fixture table.

  **Key empirical findings that shape design:**
  - **S-5**: `queue.put_nowait(POISON)` RAISES `QueueFull` when queue is
    full. Shutdown path MUST use `stop_event + wait_for(get, timeout)`
    pattern — NOT poison-pill via `put_nowait`.
  - **S-6**: `IncomingMessage` has NO `meta` field today; `Origin` DOES
    already include `"scheduler"`; handler does NOT branch on origin.
  - **S-4, case 5**: macOS blocks even same-process-different-fd attempts
    on the same pidfile. Good: closes a subtle self-deadlock on re-entry.
  - **S-1**: p99 = 3.4 ms across 200 concurrent inserts. Single conn is fine.

Companion docs (coder **must** read before starting):

- `plan/phase5/description.md` — 83-line summary.
- `plan/phase5/detailed-plan.md` — 766-line canonical spec with §1–§16
  decisions, invariants, and deferred tech debt.
- `plan/phase5/spike-findings.md` — empirical answers + verdict table.
- `plan/phase4/implementation.md` — style precedent (commit plan,
  signature specs, test-first order).
- `plan/phase2/implementation.md` — IncomingMessage, per-chat lock,
  `TurnStore.sweep_pending` invariants.

**Auth:** OAuth via `claude` CLI (`~/.claude/`). Do not introduce
`ANTHROPIC_API_KEY` anywhere. Scheduler-turns use the same bridge and
same OAuth session.

---

## 0. Pitfall box (MUST READ)

Things the coder absolutely MUST NOT do:

1. **DO NOT** use `queue.put_nowait(POISON)` on a full queue for shutdown
   — it raises `QueueFull` (spike S-5). Use `stop_event + wait_for(get,
   timeout=0.5)` consumer pattern instead. If a poison pill IS chosen,
   use `await queue.put(POISON)` and accept the wait-for-slot behaviour.
2. **DO NOT** call `fcntl.flock` without `LOCK_NB` — a leaked fd from a
   previous daemon start in the same process (e.g. test reloader) will
   deadlock. S-4 case 5 confirmed macOS blocks same-process-different-fd.
3. **DO NOT** skip `_inflight` set exclusion in `revert_stuck_sent` — B2
   regression (premature revert while consumer is actively delivering).
4. **DO NOT** add a second aiosqlite connection for the scheduler. S-1
   proved contention is negligible (p99 3.4 ms).
5. **DO NOT** reuse `tools/memory/_memlib` sys.path insert at position 0
   — `conftest.py` (phase-4 lesson, tech-debt #4) explicitly uses
   `.append` to avoid shadowing. Scheduler CLI follows the same rule.
6. **DO NOT** `INSERT OR IGNORE` then commit before the `UPDATE
   schedules.last_fire_at` — the plan-§5.3 sequence inside ONE
   `async with lock:` is load-bearing; S-8 verified atomicity.
7. **DO NOT** iterate schedule candidates minute-by-minute in the
   producer loop — use `is_due(expr, last_fire_at, now)` and let it
   return the single latest matching UTC minute.
8. **DO NOT** bake cron-parser code into `tools/schedule/main.py` — one
   source of truth: `src/assistant/scheduler/cron.py`. CLI imports via
   `sys.path.insert(<project_root>/src)` (pattern from phase-4 `_memlib`).
9. **DO NOT** store tz-aware datetimes in SQLite — plan §2.1 uses
   `ISO-8601 UTC strings with trailing 'Z'`. Converting back and forth
   through `datetime.fromisoformat` requires stripping the `Z`.
10. **DO NOT** match on `datetime.weekday()` directly — Python uses
    Mon=0..Sun=6; cron uses Sun=0..Sat=6. Convert:
    `cron_dow = (dt.weekday() + 1) % 7`.
11. **DO NOT** invoke `adapter.send_text` from inside `ClaudeHandler`
    — handler communicates via `emit(text)` callback only. Dispatcher
    accumulates emitted chunks and calls `adapter.send_text` once.
12. **DO NOT** pass `meta={}` to `IncomingMessage` until S-6 delta ships
    (field is not there today; TypeError if you try — see spike output).

---

## 1. Commit plan (7 commits)

Each commit is a logical unit, tests-first where useful, all under 500
LOC diff. Coder must run `just lint && uv run pytest -x` before each.

| # | Title | New files | Edit | LOC |
|---|-------|-----------|------|-----|
| 1 | schema v3 + migration 0003 | `migrations/0003_scheduler.sql`, `tests/test_db_migrations_v3.py` | `state/db.py` (+15) | ~90 |
| 2 | cron parser + `is_due` (+ S-3 DST helpers) | `scheduler/__init__.py`, `scheduler/cron.py`, `tests/test_scheduler_cron_parser.py`, `tests/test_scheduler_cron_semantics.py` | — | ~650 |
| 3 | `SchedulerStore` + store tests | `scheduler/store.py`, `tests/test_scheduler_store.py` | — | ~370 |
| 4 | `SchedulerDispatcher` + `ScheduledTrigger` + tests | `scheduler/dispatcher.py`, `tests/test_scheduler_dispatcher.py` | `adapters/base.py` (+4: `meta` field) | ~340 |
| 5 | `SchedulerLoop` + `SchedulerSettings` + loop tests | `scheduler/loop.py`, `tests/test_scheduler_loop.py` | `config.py` (+40: `SchedulerSettings`) | ~420 |
| 6 | CLI `tools/schedule/main.py` + Bash hook allowlist + CLI tests | `tools/schedule/main.py`, `skills/scheduler/SKILL.md`, `tests/test_schedule_cli.py`, `tests/test_scheduler_bash_hook_allowlist.py` | `bridge/hooks.py` (+50: `_BASH_PROGRAMS` entry + validator) | ~690 |
| 7 | Daemon integration (flock, clean-slate, scheduler spawn, origin branch) + E2E tests + phase-4 debt #7 (history total snippet cap) | `tests/test_scheduler_recovery.py`, `tests/test_scheduler_origin_branch_e2e.py`, `docs/ops/launchd.plist.example`, `docs/ops/scheduler.service.example` | `main.py` (+120), `handlers/message.py` (+20), `bridge/claude.py` (+5 docstring), `bridge/history.py` (+20: total cap), `bridge/system_prompt.md` (+5) | ~550 |

Total: ~3100 LOC across code + tests. Matches plan §6 LOC estimate
(~2670 production + ~245 edits + test LOC).

### Commit messages (suggested)

```
phase 5: schema v3 scheduler migration
phase 5: stdlib cron parser + is_due with DST semantics
phase 5: SchedulerStore aiosqlite wrapper + tests
phase 5: SchedulerDispatcher + ScheduledTrigger + IncomingMessage.meta
phase 5: SchedulerLoop tick-loop + SchedulerSettings
phase 5: tools/schedule CLI + bash allowlist + scheduler skill
phase 5: Daemon integration + flock + origin branch + phase-4 #7 cap
```

---

## 2. Test-first order

For these three commits, coder writes tests FIRST against the S-9
fixture table (if applicable), then implements:

- **Commit 2** (`cron.py`): S-9 fixture table → `test_scheduler_cron_semantics.py`.
  Implement parser + `is_due` to make fixtures pass.
- **Commit 3** (`store.py`): invariants (unique, last_fire_at-on-insert-only)
  → `test_scheduler_store.py`. Implement store.
- **Commit 4** (`dispatcher.py`): delivery state-machine transitions
  (pending → sent → acked; pending → pending+attempts on error;
  inflight-set exclusion) → `test_scheduler_dispatcher.py`. Implement
  dispatcher.

For commits 1, 5, 6, 7: implementation and tests together (migrations
and CLI are mostly mechanical; loop integrates all pieces).

---

## 3. Per-file signature specs

### 3.1 `src/assistant/state/migrations/0003_scheduler.sql` (commit 1)

Verbatim SQL from detailed-plan §2.1. No Python. Minor hardening:

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

### 3.2 `src/assistant/state/db.py` edits (commit 1)

Insert after the existing `_apply_v2` function (around line 63):

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

And bump the `SCHEMA_VERSION` constant at top: `SCHEMA_VERSION = 3`.

Wire into `apply_schema`:

```python
    if current < 3:
        await _apply_v3(conn)
        current = 3
```

### 3.3 `src/assistant/scheduler/cron.py` (commit 2)

Public API:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


class CronParseError(ValueError):
    """Malformed 5-field cron expression."""


@dataclass(frozen=True, slots=True)
class CronExpr:
    minute: frozenset[int]     # subset of 0..59
    hour: frozenset[int]       # subset of 0..23
    day_of_month: frozenset[int]  # subset of 1..31
    month: frozenset[int]      # subset of 1..12
    day_of_week: frozenset[int]  # subset of 0..6 (Sun=0..Sat=6)
    raw: str                   # original expression


def parse_cron(expr: str) -> CronExpr:
    """Parse 5-field POSIX cron into a CronExpr.

    Supported syntax per field:
      - `*` (all values)
      - integer literal (e.g. `9`)
      - comma list (e.g. `1,15,30`)
      - range (e.g. `1-5`)
      - step (e.g. `*/15`, `0-30/5`)

    NOT supported: `@reboot`, `@daily`, letter names (JAN, MON).
    Raises CronParseError on any violation.
    """
    ...


def matches_local(expr: CronExpr, dt_local: datetime) -> bool:
    """True iff dt_local (tzinfo-aware or naive) matches expr.

    Day-of-week uses cron convention Sun=0..Sat=6 (converted from
    Python weekday()). Day-of-month AND day-of-week semantics per POSIX:
    if either is non-*, match on the OR of both (NOT AND).
    """
    ...


def is_existing_local_minute(naked: datetime, tz: ZoneInfo) -> bool:
    """See spike-findings S-3 — DST spring-skip detector via round-trip."""
    ...


def is_ambiguous_local_minute(naked: datetime, tz: ZoneInfo) -> bool:
    """See spike-findings S-3 — DST fall-back detector via fold compare."""
    ...


def is_due(
    expr: CronExpr,
    last_fire_at: datetime | None,
    now: datetime,
    tz: ZoneInfo,
    catchup_window_s: int = 3600,
) -> datetime | None:
    """Return the UTC minute-boundary t such that:
      - last_fire_at < t <= now
      - t.astimezone(tz) matches expr
      - t.astimezone(tz).fold == 0 (skip DST fall-back duplicate)
      - now - t <= catchup_window_s

    If multiple t match (missed ticks), return the LATEST. Else None.
    If the latest match is older than catchup_window_s, return None
    (caller logs `scheduler_catchup_miss`).

    Implementation: iterate UTC minutes from max(last_fire_at, now -
    catchup_window_s) + 1 min up to now (inclusive), newest first.
    First match wins. Bounded by catchup_window_s // 60 candidates
    (default 60), O(1) fast path when last_fire_at == now // minute.
    """
    ...
```

Test files (commit 2):

- `tests/test_scheduler_cron_parser.py` — 30+ cases:
  * bare: `* * * * *` → all-* sets; `0 9 * * *` → minute={0}, hour={9}, ...
  * lists: `1,2,3 * * * *` → minute={1,2,3}
  * ranges: `0 9-17 * * *` → hour={9..17}
  * steps: `*/15 * * * *` → minute={0,15,30,45}; `0 */4 * * *` → hour={0,4,8,12,16,20}
  * combos: `0,30 */2 * * 1-5` (weekday half-hourly)
  * errors: wrong field count, out-of-range (`60 * * * *`), bad step (`*/0`),
    letter names rejected (`* * * * MON`).
- `tests/test_scheduler_cron_semantics.py` — 30+ cases using S-9 fixtures:
  * first-ever fire (`last_fire_at=None` → match if now's minute matches).
  * tick inside same minute → None (no double-fire).
  * tick at next minute → fires once.
  * gap 1 hour, 1 day → latest match returned.
  * DST spring: `30 2 * * *` Europe/Berlin 2026-03-29 02:00–04:00 UTC
    window → None (no UTC minute projects to existing 02:30 local).
  * DST fall: `30 2 * * *` Europe/Berlin 2026-10-25 iter UTC 00:00–02:00
    → single match at `2026-10-25T00:30:00Z` (fold=0).
  * catchup edge: `now - t == 3599` → fires; `3601` → None + (test hooks
    a logger to assert `scheduler_catchup_miss`).
  * `last_fire_at` invariant: returns t1, next call with `last_fire_at=t1`
    and `now=t1+1min` returns t1+1min only if expr matches t1+1min.

### 3.4 `src/assistant/scheduler/store.py` (commit 3)

```python
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import aiosqlite

from assistant.scheduler.cron import CronExpr


class SchedulerStore:
    """Thin aiosqlite wrapper over schedules + triggers.

    Shares connection AND asyncio.Lock with ConversationStore per plan
    §1.11 (spike S-1 confirmed no contention).
    """

    def __init__(self, conn: aiosqlite.Connection, lock: asyncio.Lock) -> None:
        self._conn = conn
        self._lock = lock

    # --- schedules CRUD ---

    async def insert_schedule(
        self, *, cron: str, prompt: str, tz: str = "UTC"
    ) -> int: ...

    async def count_enabled(self) -> int: ...

    async def list_schedules(self, *, enabled_only: bool = False) -> list[dict[str, Any]]: ...

    async def set_enabled(self, schedule_id: int, enabled: bool) -> bool:
        """Returns True iff row existed."""

    async def delete_schedule(self, schedule_id: int) -> bool:
        """Soft-delete (enabled=0). Returns True iff row existed."""

    async def get_schedule(self, schedule_id: int) -> dict[str, Any] | None: ...

    # --- due materialization ---

    async def iter_enabled_schedules(self) -> list[dict[str, Any]]:
        """Read snapshot used by SchedulerLoop each tick. Small (≤ 64)."""

    async def try_materialize_trigger(
        self, schedule_id: int, prompt: str, scheduled_for: datetime
    ) -> int | None:
        """INSERT OR IGNORE + UPDATE last_fire_at atomically (plan §5.3).

        Returns trigger_id if INSERT took (new trigger); None if UNIQUE
        violated (already materialized). last_fire_at updates ONLY on
        success (rowcount==1). S-8 confirmed atomicity under reader load.
        """

    # --- dispatcher transitions ---

    async def mark_sent(self, trigger_id: int) -> None: ...
    async def mark_acked(self, trigger_id: int) -> None: ...
    async def mark_pending_retry(
        self, trigger_id: int, last_error: str
    ) -> int:
        """Bump attempts, set status='pending', stamp last_error.
        Returns new attempts count.
        """
    async def mark_dead(self, trigger_id: int, last_error: str) -> None: ...
    async def mark_dropped(self, trigger_id: int, reason: str) -> None: ...

    # --- recovery ---

    async def clean_slate_sent(self) -> int:
        """Boot-only: revert ALL status='sent' → 'pending' (plan §8.2).
        Returns rowcount. Call BEFORE dispatcher starts accepting.
        """

    async def revert_stuck_sent(
        self, timeout_s: int, exclude_ids: set[int]
    ) -> int:
        """Runtime sweep: revert 'sent' rows older than timeout_s that
        are NOT in exclude_ids (the dispatcher's _inflight set). Plan §1.4.

        Excludes empty set means: revert nothing (safe default).
        """

    async def recent_triggers(
        self, *, schedule_id: int | None = None, limit: int = 20
    ) -> list[dict[str, Any]]: ...
```

Tests (`tests/test_scheduler_store.py`): 15+ cases. Use pytest fixture
that spins an in-memory aiosqlite conn with v3 migration applied.
Cover: insert/list/delete CRUD; soft-delete; unique-violation idempotency;
`last_fire_at` NOT-advanced on unique-violation; clean_slate_sent does
not affect 'pending'/'acked'; revert_stuck_sent respects exclude_ids AND
timeout; mark_dead is terminal (no subsequent transitions succeed —
check via attempt to mark_acked after mark_dead).

### 3.5 `src/assistant/scheduler/dispatcher.py` (commit 4)

```python
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from assistant.adapters.base import IncomingMessage, MessengerAdapter
from assistant.config import Settings
from assistant.handlers.message import ClaudeHandler
from assistant.logger import get_logger
from assistant.scheduler.store import SchedulerStore

log = get_logger("scheduler.dispatcher")

_OWNER_NOT_SET = -1


@dataclass(frozen=True, slots=True)
class ScheduledTrigger:
    """Boundary dataclass between SchedulerLoop (producer) and
    SchedulerDispatcher (consumer). Phase-8 may serialize this over UDS."""
    trigger_id: int
    schedule_id: int
    prompt: str
    scheduled_for: datetime   # tz-aware UTC
    attempt: int              # 1-based


class SchedulerDispatcher:
    """Consumer side: drains queue, delivers triggers as handler turns.

    Delivery state machine (plan §5.4):
      pending → sent → acked (happy path)
      pending → (error) → pending+attempts (retry)
      pending → dropped (schedule disabled mid-flight)
      pending → (attempts>=5) → dead + one-shot telegram
    """

    def __init__(
        self,
        *,
        queue: asyncio.Queue[ScheduledTrigger | None],
        store: SchedulerStore,
        handler: ClaudeHandler,
        adapter: MessengerAdapter,
        owner_chat_id: int,
        settings: Settings,
    ) -> None:
        self._queue = queue
        self._store = store
        self._handler = handler
        self._adapter = adapter
        self._owner = owner_chat_id
        self._settings = settings
        self._stop = asyncio.Event()
        self._inflight: set[int] = set()
        self._recent_acked: deque[int] = deque(maxlen=256)  # LRU dedup

    def inflight(self) -> set[int]:
        """Read-only view for SchedulerLoop revert sweep (plan §1.4)."""
        return self._inflight.copy()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Drain loop. Exits when stop() fires AND queue is drained.

        Uses `wait_for(get, timeout=0.5)` pattern (S-5) — poison pill
        via put_nowait is unsafe on a full queue.
        """
        log.info("scheduler_dispatcher_started")
        while not self._stop.is_set():
            try:
                t = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            if t is None:
                # Optional explicit poison via await put(None); we honour it.
                break
            await self._deliver(t)
        log.info("scheduler_dispatcher_stopped")

    async def _deliver(self, t: ScheduledTrigger) -> None: ...

    async def _deliver_with_handler(
        self, t: ScheduledTrigger
    ) -> str: ...
```

Required test-file `tests/test_scheduler_dispatcher.py` cases (8+):

1. Happy path: queue.put → consumer delivers → adapter.send_text called
   with collected emit chunks → store.mark_acked + lru contains id.
2. Handler raises ClaudeBridgeError → store.mark_pending_retry(attempts=1);
   id removed from _inflight; no adapter.send_text call.
3. `attempts >= 5` on next retry → store.mark_dead + dead-notify stub
   called (mock the notify marker).
4. Schedule enabled=0 between queue-put and delivery → store.mark_dropped;
   no handler call.
5. Duplicate trigger_id in _recent_acked → skipped (not re-delivered);
   store.mark_acked still called (idempotency); _inflight cleanly
   discards.
6. `inflight()` reflects the in-progress trigger id; cleared after
   finally.
7. Handler emits no text → skip adapter.send_text; still mark_acked
   (no-op reply is semantically OK for scheduler-only turns).
8. Shutdown: stop_event set → consumer exits within `timeout` after
   final in-flight deliver completes; does NOT cancel mid-deliver
   (plan §5.5 explicit).

### 3.6 `src/assistant/adapters/base.py` edit (commit 4)

Insert in `IncomingMessage` — add 5th field:

```python
from typing import Any, Literal

@dataclass(frozen=True, slots=True)
class IncomingMessage:
    chat_id: int
    text: str
    message_id: int | None = None
    origin: Origin = "telegram"
    meta: dict[str, Any] | None = None   # phase-5: scheduler carries trigger_id here
```

Plus `from typing import Any`. Dataclass is `frozen=True slots=True` —
adding a field is backward-compatible (all existing construction sites
omit `meta`).

### 3.7 `src/assistant/scheduler/loop.py` (commit 5)

```python
from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant.config import Settings
from assistant.logger import get_logger
from assistant.scheduler.cron import CronExpr, is_due, parse_cron, CronParseError
from assistant.scheduler.dispatcher import ScheduledTrigger, SchedulerDispatcher
from assistant.scheduler.store import SchedulerStore

log = get_logger("scheduler.loop")


class SchedulerLoop:
    """Producer side: ticks every `tick_interval_s`, materializes due
    triggers, puts them into the dispatcher queue.

    Outer try/except routes fatal errors to Telegram (plan §8.4, GAP #15).
    """

    def __init__(
        self,
        *,
        queue: asyncio.Queue[ScheduledTrigger | None],
        store: SchedulerStore,
        dispatcher: SchedulerDispatcher,
        settings: Settings,
        notify_fn: "NotifyFn | None" = None,
    ) -> None: ...

    def stop(self) -> None: ...

    async def run(self) -> None:
        """Outer-loop: while not stop, tick + sleep. Catches inner
        exceptions (log warn, continue); top-level exception logs fatal
        and emits one-shot Telegram notify (cooldown 24h via marker-file)."""

    async def _tick(self) -> None:
        """One iteration: read enabled schedules, filter by is_due,
        try_materialize_trigger, put on queue (awaited → backpressure)."""

    async def count_catchup_misses(self) -> int:
        """Startup helper (plan §8.3 GAP #16): for each enabled schedule
        with last_fire_at older than now - catchup_window_s, count the
        number of cron matches in [last_fire_at, now - catchup_window_s]
        window. Used in daemon startup to send one recap message if sum > 0.
        """
```

`NotifyFn` is `Callable[[str], Awaitable[None]]` — passed in from Daemon
(closure over `_adapter.send_text(owner_chat_id, msg)` with marker-file
rate-limit). Same pattern as `_bootstrap_notify_failure` (phase 3).

Tests `tests/test_scheduler_loop.py` (10+ cases):

1. Single schedule `*/5 * * * *`, initial tick at T → materialize 1 trigger.
2. Three consecutive ticks within same minute → 1 trigger (idempotency).
3. Long gap (last_fire_at = 1h ago, `*/5`, now = now) → 1 trigger at
   latest match (catchup).
4. Long gap (6 h, catchup_window_s=3600) → None + `scheduler_catchup_miss`
   log for that schedule.
5. Queue full (maxsize=3, 5 schedules all due simultaneously) →
   producer awaits on put, drained as consumer pulls; no loss.
6. Disabled schedule → skipped (never returns from is_due).
7. Malformed cron in DB → log `scheduler_cron_parse_failed schedule_id=N`;
   schedule skipped; loop continues (does not crash other schedules).
8. Parser raises `CronParseError` → caught by `_tick`, warn log.
9. Outermost exception → `notify_fn` called; loop re-raises for Daemon
   to observe (but Daemon already logs + continues via `_spawn_bg`'s
   done-callback).
10. Fake clock advance (monkeypatch `datetime.now`), run `n` ticks,
    assert N triggers materialized for a `*/1 * * * *` schedule.

### 3.8 `src/assistant/config.py` edit (commit 5)

Insert `SchedulerSettings` nested class after `MemorySettings`:

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
    sent_revert_timeout_s: int = 360   # claude.timeout(300)+60
    dispatcher_queue_size: int = 64
    max_schedules: int = 64
    missed_notify_cooldown_s: int = 86400  # 24h
```

Add field to `Settings`:

```python
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
```

### 3.9 `tools/schedule/main.py` (commit 6)

Stdlib-only argparse CLI. Pattern identical to `tools/memory/main.py`:

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
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
# GAP #18: single source for cron parser.
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
_TZ_RE = re.compile(r"^[A-Za-z_]+(/[A-Za-z_]+(/[A-Za-z_]+)?)?$")


# Data-dir resolution (mirror config.py; installer-pattern, no pydantic):
def _data_dir() -> Path: ...

def _db_path() -> Path: ...

def _connect() -> sqlite3.Connection: ...

def _ok(data: dict[str, Any]) -> int: ...
def _fail(code: int, error: str, **extra: Any) -> int: ...


def cmd_add(args) -> int:
    # Validate cron
    try:
        _ = parse_cron(args.cron)
    except CronParseError as exc:
        return _fail(EXIT_VAL, f"cron parse: {exc}")
    # Validate tz
    try:
        ZoneInfo(args.tz)
    except ZoneInfoNotFoundError:
        return _fail(EXIT_VAL, f"unknown tz: {args.tz!r}")
    # Validate prompt
    if not args.prompt.strip():
        return _fail(EXIT_VAL, "prompt must be non-empty")
    if len(args.prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        return _fail(EXIT_VAL, f"prompt exceeds {_MAX_PROMPT_BYTES} bytes")
    for ch in args.prompt:
        if ord(ch) < 0x20 and ch not in "\t\n":
            return _fail(EXIT_VAL, f"control char U+{ord(ch):04X} not allowed in prompt")
    # Cap check (GAP #11)
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
        return _ok({"id": cur.lastrowid, "cron": args.cron, "prompt": args.prompt, "tz": args.tz})
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

Exit-code / JSON-shape reference — plan §3.2/§3.4. Use `sqlite3` with
`PRAGMA busy_timeout=5000` and `PRAGMA journal_mode=WAL`. Do NOT touch
`triggers` table in the CLI — history is read-only from CLI perspective.

### 3.10 `src/assistant/bridge/hooks.py` edit (commit 6)

Add `_validate_schedule_cli` as a helper under `_validate_python_invocation`,
and route from the match statement. Exact diff:

1. Add subcommand enum near top of module:

```python
_SCHEDULE_SUBCMDS: frozenset[str] = frozenset({
    "add", "list", "rm", "enable", "disable", "history",
})
```

2. Edit `_validate_python_invocation`: after the existing
   script-path-allowlist check, add sub-validator for `tools/schedule/main.py`:

```python
    if script == "tools/schedule/main.py":
        return _validate_schedule_argv(argv[2:])
    if script == "tools/memory/main.py":
        return _validate_memory_argv(argv[2:])  # phase-4 precedent (if extracted)
    return None
```

3. Add new validator:

```python
def _validate_schedule_argv(args: list[str]) -> str | None:
    if not args:
        return "schedule CLI requires a subcommand"
    sub = args[0]
    if sub not in _SCHEDULE_SUBCMDS:
        return f"schedule subcommand {sub!r} not allowed"
    # Flag-shape validation per subcommand.
    remaining = args[1:]
    if sub == "add":
        # Expect exactly: --cron "<expr>" --prompt "<text>" [--tz "<iana>"]
        # Validate argv shape (no positional); defer cron parse to CLI.
        allowed_flags = {"--cron", "--prompt", "--tz"}
        i = 0
        while i < len(remaining):
            tok = remaining[i]
            if tok not in allowed_flags:
                return f"schedule add: flag {tok!r} not allowed"
            if i + 1 >= len(remaining):
                return f"schedule add: flag {tok} requires a value"
            val = remaining[i + 1]
            if tok == "--prompt" and len(val.encode("utf-8")) > 2048:
                return "schedule add: --prompt exceeds 2048 bytes"
            i += 2
        return None
    if sub == "list":
        for tok in remaining:
            if tok != "--enabled-only":
                return f"schedule list: unknown flag {tok!r}"
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
        # --schedule-id N (opt), --limit N (opt)
        allowed = {"--schedule-id", "--limit"}
        i = 0
        while i < len(remaining):
            tok = remaining[i]
            if tok not in allowed:
                return f"schedule history: flag {tok!r} not allowed"
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

Tests `tests/test_scheduler_bash_hook_allowlist.py` (10+ cases using
`check_bash_command`):

```python
# allow
"python tools/schedule/main.py add --cron \"0 9 * * *\" --prompt \"ping\""
"python tools/schedule/main.py list"
"python tools/schedule/main.py list --enabled-only"
"python tools/schedule/main.py rm 42"
"python tools/schedule/main.py history --schedule-id 1 --limit 10"

# deny
"python tools/schedule/main.py add --cron \"0 9 * * *\" --prompt \"$(cat /etc/passwd)\""  # $(
"python tools/schedule/main.py add --cron \"0 9 * * *\" --prompt \"`whoami`\""              # backtick
"python tools/schedule/main.py add --cron a --prompt x | ls"                                 # pipe
"python tools/schedule/main.py add --cron a --prompt x ; rm -rf /"                           # ;
"python tools/schedule/main.py rm abc"                                                       # non-int
"python tools/schedule/main.py unknown"                                                      # bad sub
"python tools/schedule/main.py add --tz z --cron a"                                          # missing --prompt (not verified here; EXIT 3 at CLI)
"python tools/schedule/main.py add --cron \"0 9 * * *\" --prompt \"" + "x" * 2049 + "\""     # prompt too long
```

### 3.11 `skills/scheduler/SKILL.md` (commit 6)

```yaml
---
name: scheduler
description: "Cron-расписания. Используй когда владелец говорит 'напомни каждый день', 'раз в неделю'. CLI python tools/schedule/main.py, 5-field POSIX cron (m h dom mon dow)."
allowed-tools: [Bash]
---
```

Body (Russian prose; ≤ 3 allowed-tools per phase-4 B-CRIT-3 guidance):

- Subcommand reference with bash examples.
- Cron primer (minute hour day-of-month month day-of-week).
- Examples with `crontab.guru` link.
- `--tz "Europe/Moscow"` override guidance.
- **Boundary rules (critical):**
  * Prompt is a snapshot, not a template — changes to vault won't
    update it; to change prompt, `rm` and `add` fresh.
  * DST fall-back days: cron fires ONCE (first occurrence of local
    hour); plan §S-3 verified.
  * One-off reminders (single fire) — prefer writing to memory vault
    instead; scheduler is for RECURRING jobs only.
  * CLI has 64-schedule cap; ask owner to `rm` before adding more.

### 3.12 `src/assistant/handlers/message.py` edit (commit 7)

Locate the URL-detector block in `_run_turn` (lines ~168-184 in current
file). Replace:

```python
        urls = _detect_urls(msg.text)
        system_notes: list[str] | None = None
        if urls:
            system_notes = [...]
```

with:

```python
        # Phase 5 (B4): scheduler-origin branch. Scheduler-note first,
        # URL-note second (spike S-7 confirmed ClaudeBridge preserves order).
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
            log.info(
                "url_detected",
                chat_id=msg.chat_id,
                turn_id=turn_id,
                urls=urls,
            )

        system_notes = notes or None
```

Test `tests/test_scheduler_origin_branch_e2e.py`:
- Build `IncomingMessage(chat_id=OWNER, text="summary", origin="scheduler",
  meta={"trigger_id": 42})`.
- Monkey-patch `ClaudeBridge.ask` to capture `system_notes` arg.
- Run `handler.handle(msg, emit)` with a stub emit.
- Assert `system_notes[0]` contains `"id=42"` and the text "scheduler".
- Variant with URL in text: assert `system_notes` = [scheduler_note, url_note]
  (exactly 2, in this order).

### 3.13 `src/assistant/bridge/claude.py` edit (commit 7, docstring only)

Add to `ask()` docstring after the existing `system_notes` paragraph:

```
Phase 5 convention: when `origin="scheduler"` is the turn trigger,
callers put the scheduler-note FIRST and URL-note SECOND. The bridge
merges in order; see spike-findings S-7 for the verification.
```

### 3.14 `src/assistant/bridge/history.py` edit (commit 7)

Address phase-4 tech-debt #7 (history snippet total cap). Add new
module-level constant + wire into `history_to_user_envelopes`:

```python
# Phase 5 (phase-4 debt #7): total snippet cap to prevent context-window
# blow-out when scheduler-injected turns accumulate many tool_results.
HISTORY_MAX_SNIPPET_TOTAL_BYTES = 16384
```

In `_render_tool_summary`, accept a `total_budget_remaining` ref
(mutable; e.g. `list[int]` with one slot) and stop appending when budget
is exhausted; leave a trailing marker `"...(history truncated for size)"`.

Simpler API: compute per-turn allowance as
`budget = HISTORY_MAX_SNIPPET_TOTAL_BYTES // max(1, len(turn_ids))` and
cap each turn's snippet at that. Document the trade-off.

Tests: extend `test_bridge_history_replay_snippet.py` with a case where
history contains 20 turns each with a 1 KB tool_result — total output
fits under 16 KB budget, last turns get smaller budgets, marker present.

### 3.15 `src/assistant/bridge/system_prompt.md` edit (commit 7)

Append a paragraph at end:

```
### Scheduler-initiated turns

Если turn помечен как `origin="scheduler"` (видишь это в системном
system-note первого сообщения), владелец не активен. Не задавай уточняющих
вопросов. Работай проактивно: выполни задачу, запиши результат в vault
если нужно, и завершай. Ответ попадёт владельцу в Telegram напрямую.
```

### 3.16 `src/assistant/main.py` edit (commit 7)

Major edits. Concrete insertion points (current file is 452 lines):

**3.16.1 Imports** — add at top:

```python
import fcntl

from assistant.scheduler.dispatcher import SchedulerDispatcher
from assistant.scheduler.loop import SchedulerLoop
from assistant.scheduler.store import SchedulerStore
```

**3.16.2 pidfile mutex** — new method on `Daemon` (insert before
`_ensure_vault`, around line 314):

```python
    def _acquire_pid_lock_or_exit(self) -> None:
        """Advisory flock on `<data_dir>/run/daemon.pid` (plan §1.13, S-4).

        On BlockingIOError: log `daemon_already_running` and exit 0.
        On success: write PID, keep fd on `self._pid_fd`.
        """
        pid_dir = self._settings.data_dir / "run"
        pid_dir.mkdir(parents=True, exist_ok=True)
        pid_path = pid_dir / "daemon.pid"
        fd = os.open(str(pid_path), os.O_RDWR | os.O_CREAT, 0o644)
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

Add `self._pid_fd: int | None = None` to `__init__`.

In `Daemon.stop()`, BEFORE the final `self._log.info("daemon_stopped")`,
close it:

```python
        if self._pid_fd is not None:
            try:
                os.close(self._pid_fd)  # flock released
            except OSError:
                pass
            self._pid_fd = None
```

**3.16.3 `Daemon.start()` edits** — replace lines 362-399 (approx) with
the sequence in plan §8.1. Concrete substitutions:

- As the FIRST statement of `start()`, call `self._acquire_pid_lock_or_exit()`.
- After `await apply_schema(self._conn)`, insert `SchedulerStore`
  construction and `clean_slate_sent` + `count_catchup_misses` calls.
- After `self._adapter.start()`, construct dispatcher + loop and
  `_spawn_bg` both.
- Add one-shot recap `send_text` as fire-and-forget.

Exact snippet (replaces lines after `await apply_schema(...)`):

```python
        conv = ConversationStore(self._conn)
        turns = TurnStore(self._conn, lock=conv.lock)
        sched_store = SchedulerStore(self._conn, lock=conv.lock)

        # Turn-sweep — safe because flock guarantees we are the sole daemon.
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

        # Scheduler wiring (plan §8.1): dispatcher first so loop sees its
        # inflight() view; then loop.
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
            notify_fn=self._scheduler_notify,
        )
        self._scheduler_dispatcher = dispatcher
        self._scheduler_loop = loop_
        self._spawn_bg(dispatcher.run(), name="scheduler_dispatcher")
        self._spawn_bg(loop_.run(), name="scheduler_loop")

        self._spawn_bg(self._sweep_run_dirs(), name="sweep_run_dirs")
        self._spawn_bg(self._bootstrap_skill_creator_bg(), name="skill_creator_bootstrap")

        # Catchup recap (plan §8.3, GAP #16).
        missed = await loop_.count_catchup_misses()
        if missed > 0:
            self._spawn_bg(self._scheduler_catchup_recap(missed), name="scheduler_catchup_recap")

        self._log.info("daemon_started", owner=self._settings.owner_chat_id)
```

**3.16.4 Stop order** — update `Daemon.stop()` to call
`self._scheduler_loop.stop()` and `self._scheduler_dispatcher.stop()`
BEFORE the bg-tasks drain (so the drain can complete within 5 s).

**3.16.5 Notify helpers** — new methods:

```python
    async def _scheduler_notify(self, msg: str) -> None:
        """Cooldown-gated Telegram notify for scheduler loop fatals (GAP #15)."""
        marker = self._settings.data_dir / "run" / ".scheduler_notified"
        now = time.time()
        cooldown = self._settings.scheduler.missed_notify_cooldown_s
        # Marker-file pattern mirroring _bootstrap_notify_failure.
        ...

    async def _scheduler_catchup_recap(self, missed: int) -> None:
        """GAP #16: one Russian-language Telegram message; cooldown 24h."""
        marker = self._settings.data_dir / "run" / ".scheduler_catchup_recap"
        ...
```

### 3.17 `docs/ops/launchd.plist.example` + `scheduler.service.example` (commit 7)

Plain XML / ini templates. Non-executable. Comment headers explain:

- `launchd.plist.example`: WatchPaths (for auto-restart on config
  change), `KeepAlive`, `ExitTimeout` settings. Phase-8 will adopt.
- `scheduler.service.example`: systemd unit with `Restart=on-failure`,
  `WorkingDirectory`, `ExecStart=/usr/bin/env python -m assistant.main`.

Reference in plan §8.5 (phase 8). Not wired in phase 5.

---

## 4. Daemon integration tests (commit 7)

`tests/test_scheduler_recovery.py` (6+ cases):

1. `_acquire_pid_lock_or_exit` — subprocess holds lock; main `Daemon.start()`
   exits 0 and logs `daemon_already_running`.
2. SIGKILL the subprocess → second start acquires lock.
3. `clean_slate_sent`: seed 3 rows with status='sent', 1 with status='pending',
   1 with status='acked'. Call `store.clean_slate_sent()`. Assert:
   rowcount=3, sent rows now pending+attempts++, others untouched.
4. `revert_stuck_sent`: seed row status='sent' age=400s, _inflight={}, timeout=360
   → reverts. Same row with `_inflight={its_id}` → untouched.
5. LRU dedup across restart: simulate dispatcher-restart by constructing
   a fresh dispatcher (empty LRU) and replaying the same ScheduledTrigger
   → `_deliver` calls handler again (known at-least-once limitation).
   Confirms plan §15 scepsis is honest.
6. Boot-time flock: file pre-exists, perms 0o644, not held → start
   succeeds; file content is daemon PID.

`tests/test_scheduler_origin_branch_e2e.py` — §3.12 above.

`tests/test_db_migrations_v3.py`:
- Apply v2, then v3; query `PRAGMA user_version` → 3.
- Insert a schedule + trigger; verify FK cascade on `DELETE FROM schedules`.
- Verify UNIQUE(schedule_id, scheduled_for) via double-INSERT → second
  raises `IntegrityError`.

---

## 5. Pre-flight test audit

Commit 7 touches `IncomingMessage` dataclass. Audit existing tests that
construct `IncomingMessage` to confirm they don't break on the new
optional field:

```bash
grep -Rn "IncomingMessage(" tests/ src/
```

Expected: all existing callsites either pass keyword args (safe) or the
4 positional args. Adding `meta: ... = None` is backward-compatible.

Commit 7 also changes `handlers/message.py::_run_turn`. Existing
`test_handler_chat_lock.py` and `test_handler_meta_propagation.py` must
still pass. Run them after the diff:

```bash
uv run pytest tests/test_handler_chat_lock.py tests/test_handler_meta_propagation.py -x
```

---

## 6. Full test order (after each commit)

After commits 1-7, the full matrix is:

```bash
uv run pytest -x                                 # all 414+ existing + new
just lint                                        # ruff check/format + mypy strict
```

E2E manual-smoke per plan §Критерии готовности:

1. `python tools/schedule/main.py add --cron "*/5 * * * *" --prompt "ping"`
   → wait ≤ 300 s → Telegram message arrives.
2. Restart Daemon between sent/acked: use a test-only delay in dispatcher
   (`SCHEDULER_DEBUG_PRE_ACK_DELAY_S=30` env) and SIGTERM daemon mid-delivery;
   on restart, clean-slate reverts, re-delivers, LRU checks prevent
   third duplicate.
3. `rm 1` then `history --schedule-id 1` → schedule enabled=0, triggers
   still visible.
4. `add --cron "invalid" --prompt x` → exit 3 with JSON error.
5. Bash hook rejects `add --cron ... --prompt "$(cat /etc/passwd)"` with
   deny reason (`pretool_decision` log line).
6. Concurrent user-turn + scheduler-trigger → second waits on per-chat lock.

---

## 7. Risk matrix impact from spikes

| Plan §14 risk | Spike outcome | Adjustment |
|---------------|---------------|------------|
| #1 dup delivery | S-8 confirmed atomicity; LRU+clean-slate hold | no change |
| #3 DST spring/fall | S-3 passed; precise helper in §3.3 | no change |
| #7 dispatcher crash → inflight inconsistent | S-5 showed stop_event pattern | use stop_event, not poison-pill |
| #8 queue full backpressure | S-5 confirmed await-block | no change |
| #11 scheduler-loop fatal | S-5 timeout-get pattern works for shutdown | `notify_fn` callback from Daemon |
| #13 two daemons race | S-4 flock clean on macOS+Linux | no change |
| #16 `last_fire_at` ambiguous | S-8 atomicity ✓ + is_due spec ✓ | no change |

No new risks surfaced by spikes. One operational note: S-5 `put_nowait`
gotcha is recorded in §0 pitfall #1.

---

## 8. Open questions for devil wave-2

1. **Stop order during shutdown.** Plan §8.5 says `_bg_tasks` drain has
   5 s timeout. Dispatcher may be mid-`_deliver` (Claude-turn up to 300 s).
   Should dispatcher **cancel** in-flight delivery on stop (lose ack),
   or block until turn completes (exceed 5 s drain)? Current design
   cancels via task-cancellation after 5 s. Devil wave-2: validate.
2. **Adapter-close race on shutdown.** After `TelegramAdapter.stop()` the
   dispatcher may still try `adapter.send_text`. S-2 did NOT cover "send
   after adapter.stop()". Safe path: dispatcher.stop() runs BEFORE
   adapter.stop() in `Daemon.stop()`. Code needs that ordering (it is
   already the order in the plan but worth flagging).
3. **`count_catchup_misses` correctness under last_fire_at=NULL.**
   Schedule added 3 h ago but never fired (no tick crossed the boundary
   yet) — is the gap counted as a miss? Current spec says no; but if
   a schedule was added with `--cron "0 9 * * *"` yesterday and daemon
   was down for 26 h, the "9am yesterday" miss should count. Decide:
   cap backward-scan to `SCHEDULER_CATCHUP_WINDOW_S * 4` (4 hours
   default) to bound the scan even for never-fired schedules.
4. **LRU size vs unique trigger_id lifetime.** 256 slots × one user's
   trigger stream → cold-start after 256 triggers collides the LRU.
   Is that acceptable? (It is, since we still have DB-status sentinel;
   LRU is an extra safety net.) Devil wave-2: re-confirm.
5. **`schedules.tz` injection: is IANA regex enough?** CLI validates via
   `ZoneInfo(args.tz)` raising. But a malicious IANA-looking name (e.g.
   `../../etc/passwd`) would simply raise `ZoneInfoNotFoundError` — no
   FS read. S-6 / §3.9 regex `^[A-Za-z_]+(/[A-Za-z_]+(/[A-Za-z_]+)?)?$`
   is a backup. Devil wave-2: agree or tighten?

---

## 9. Known gotchas (summary)

All itemised in §0 pitfall box. Repeated here for quick scanning:

1. `put_nowait(POISON)` raises `QueueFull` — use `stop_event + wait_for`.
2. `fcntl.flock` without `LOCK_NB` — deadlocks in same process on
   leaked fd.
3. `revert_stuck_sent` MUST exclude `_inflight` (B2).
4. Single aiosqlite connection is sufficient (S-1 p99 3.4 ms).
5. CLI `_memlib` / `_schedlib` sys.path: `.append`, not `.insert(0)`.
6. `INSERT OR IGNORE` + `UPDATE last_fire_at` in ONE lock block.
7. Use `is_due` — don't iterate minutes in the producer.
8. One source of cron parser — `src/assistant/scheduler/cron.py`.
9. Store tz-aware datetimes as ISO-8601 `...Z` strings.
10. Cron DOW is Sun=0; Python weekday is Mon=0 — convert.
11. Handler communicates via `emit` only; dispatcher calls send_text.
12. `IncomingMessage.meta` doesn't exist pre-§3.6 edit — TypeError.

---

## 10. References

- Spike reports: `spikes/phase5_s{1..8}_report.json`,
  `spikes/phase5_s9_cron_fixtures.json`.
- PEP 495 fold semantics: https://peps.python.org/pep-0495/
- POSIX cron 5-field spec: `man 5 crontab` (verify with `crontab.guru`).
- aiosqlite WAL busy_timeout behaviour: phase-2 `state/db.py` + tests.
- `claude-agent-sdk` 0.1.59 system-notes merging: `bridge/claude.py::ask`
  (unchanged from phase 3; see spike S-7).
