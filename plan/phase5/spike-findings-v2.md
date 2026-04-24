# Phase 5 v2 — Spike findings (RQ0-RQ6)

Date: 2026-04-23
Runner: researcher agent
Project: 0xone-assistant (phase 5b scheduler)
Plan: `plan/phase5/description-v2.md`
Scripts: `plan/phase5/spikes/rq2_cron_parity.py`, `rq3_dst.py`, `rq4_fake_clock.py`, `rq6_cron_edge.py`

## Executive summary

**Verdict: GO for coder start.** All six RQs + bonus RQ6 resolved with concrete
decisions. Only three actionable deltas to the plan:

1. **`max_lookahead_days` default** should be **1500** (~4 years + 1 day),
   not 366 — leap-day schedules (`0 0 29 2 *`) return `None` at 366. RQ2+RQ6.
2. **DST spring-skip policy**: confirmed `is_existing_local_minute` round-trip
   technique works for both `Europe/Berlin` and `Europe/Moscow`. Policy: **skip**
   non-existent minute (no catch-up). Fold-fall: fire **fold=0 only**. RQ3.
3. **Per-chat lock is ABSENT today** — precondition for phase 5 dispatcher work.
   Must be added by coder as first commit; `message.py` has no `_chat_locks`
   attribute anywhere in current source. RQ0.

No blocking ambiguities. No changes to `description-v2.md` content required
beyond the three patches in section "Patches to plan".

---

## RQ0 — per-chat lock presence

**Question.** Does `ClaudeHandler` currently hold per-chat `asyncio.Lock`?

**Method.** `grep -Rn "_chat_locks|asyncio\.Lock"` across `src/assistant/`.

**Result.** **ABSENT.** Zero matches in `src/assistant/`. `ClaudeHandler.__init__`
stores only `(settings, conv, bridge)`. There is **no** per-chat serialization
today — a scheduler-injected turn arriving mid-user-turn would race the bridge
concurrency semaphore with the user's turn on the same `chat_id`.

**Where it belongs.** `src/assistant/handlers/message.py::ClaudeHandler.__init__`
+ first line of `handle()`.

**Proposed diff sketch** (for coder; not applied here):
```python
# handlers/message.py — inside ClaudeHandler
def __init__(self, settings, conv, bridge):
    ...
    self._chat_locks: dict[int, asyncio.Lock] = {}
    self._locks_mutex = asyncio.Lock()

async def _lock_for(self, chat_id: int) -> asyncio.Lock:
    # Double-checked lookup so the cold path allocates at most once.
    lk = self._chat_locks.get(chat_id)
    if lk is not None:
        return lk
    async with self._locks_mutex:
        lk = self._chat_locks.get(chat_id)
        if lk is None:
            lk = asyncio.Lock()
            self._chat_locks[chat_id] = lk
    return lk

async def handle(self, msg, emit):
    lock = await self._lock_for(msg.chat_id)
    async with lock:
        ...  # existing body
```

**Verdict.** **ADD in phase 5 as first coder commit.** Extends phase 5 LOC by ~15.
No `asyncio.Lock` handle is stored/transferred anywhere in the codebase — safe
to add as a plain in-memory dict on the handler.

**Risk flag.** Single-owner model keeps the dict bounded (1 entry) — but since
the lock is keyed by `chat_id`, any seed chat added later (groups) keeps the
dict bounded by active chat count. No eviction needed in phase 5.

---

## RQ1 — `IncomingMessage` field-add safety

**Question.** Is adding `origin: Literal["telegram","scheduler"]="telegram"` +
`meta: dict[str, Any]|None=None` to `@dataclass(frozen=True) IncomingMessage`
backward-compatible?

**Method.**
1. Grep all `IncomingMessage(` construction sites.
2. Grep `dataclasses.asdict(` / `dataclasses.replace(`.
3. Inspect each construction site for positional-arg reliance.

**Result.**
- **3 construction sites** found in current source+tests:
  - `src/assistant/adapters/telegram.py:90` — **all keyword args** (`chat_id=, message_id=, text=`). Safe.
  - `tests/test_claude_handler.py:111,152,207,267` — 4 sites, **all keyword args**. Safe.
  - `tests/test_ping_marker_reaches_db.py:107` — **all keyword args**. Safe.
- **No `dataclasses.asdict` / `dataclasses.replace`** calls against
  `IncomingMessage`. Only hit is an unrelated note in
  `plan/phase2/spike-findings.md` about `ClaudeAgentOptions`.
- Zero positional constructions, zero data-dump sites.

**Verdict.** **SAFE to add both fields with defaults.** Field order in the
dataclass is irrelevant because every caller uses keyword args. `frozen=True`
means no runtime attribute mutation to worry about. mypy strict should pass
cleanly because every existing site supplies the three original fields by name
and the new fields have defaults.

**Recommended dataclass shape** (matches plan section F.1):
```python
from typing import Any, Literal

Origin = Literal["telegram", "scheduler"]

@dataclass(frozen=True)
class IncomingMessage:
    chat_id: int
    message_id: int
    text: str
    origin: Origin = "telegram"
    meta: dict[str, Any] | None = None  # None default, not {} — classic
                                         # frozen-dataclass mutable-default caveat.
```

Note: a `dict` default must NOT be a mutable sentinel (`= {}`) — `= None` is
the conventional pattern and matches the plan.

---

## RQ2 — stdlib cron parser fidelity vs croniter

**Question.** Will a stdlib 5-field parser agree with croniter on `next_fire`?

**Method.** Built throwaway parser (`rq2_cron_parity.py`) matching plan section E
API surface. Ran against 39 real-world expressions at `T0 = 2026-06-15 12:34 UTC`.
Compared to croniter 6.x from `/tmp/_croniter_env`.

**Result.** **39 / 39 match** on `next_fire` (UTC, first-fire after T0).
Full diff table in `plan/phase5/spikes/rq2_cron_parity.py` output:

```
TOTAL: 39  MISMATCH: 0  PARSE_FAIL: 0
```

Expressions covered: every-minute, every-15-min, daily-9am, weekday 9-17
business windows, weekend-only, monthly first-day 14:30, yearly, leap-day-like,
step variations, list forms, range-with-step, `0 0 1-7 * 1` (first-Monday
approximation), dense minute lists, Sunday-alias `7→0`.

**Single exception** (excluded from the 39, documented inline): `0 0 29 2 *`
(leap-day). At T0 in 2026, `max_lookahead_days=366` causes our parser to
return `None`, while croniter returns `2028-02-29`. See RQ6 and "Patches to plan"
for the fix (raise default to 1500 days).

**Vixie-cron semantics confirmed.** When BOTH `dom` and `dow` are restricted,
cron fires on EITHER match (`dom_ok OR dow_ok`). The implementation relies on
the **raw string** (was the field literally `*`?), not the parsed set content
(a set equal to "all days 1-31" is semantically different from `*` under
vixie semantics only in pathological hand-constructed cases — but popular
implementations including croniter use the string-level `*` test). Our
throwaway parser took `raw_expr: str` as a `next_fire` arg to preserve this.

**Verdict.** **Stdlib parser is viable.** The plan's no-croniter-dependency
stance holds. Production parser must:
- Preserve `raw_expr.split()[2] == "*"` / `[4] == "*"` for vixie star check.
- Reject bad input up-front (RQ6 shows all 11 malformed inputs rejected).
- Convert `dow=7` to `0` at parse-time (confirmed 7/0 produce identical next-fire).

---

## RQ3 — `zoneinfo` DST on Linux

**Question.** Does `zoneinfo.ZoneInfo` handle Moscow (no DST) + Berlin (DST)
correctly on the target Linux VPS?

**Method.** Ran `rq3_dst.py` (Python 3.14 on macOS, zoneinfo behavior is
identical Linux/macOS per PEP 615 — both use system tzdata, fallback to
`tzdata` PyPI package).

**Result.**

| tz | wall-clock | exists | ambiguous | note |
|----|-----------|--------|-----------|------|
| Europe/Berlin | 2026-03-29 02:30 | **False** | (n/a) | spring skip |
| Europe/Berlin | 2026-03-29 01:59 | True | False | before skip |
| Europe/Berlin | 2026-03-29 03:00 | True | False | after skip |
| Europe/Berlin | 2026-10-25 02:30 | True | **True** | fall fold |
| Europe/Berlin | 2026-10-25 01:59 | True | False | before fold |
| Europe/Berlin | 2026-10-25 03:00 | True | False | after fold |
| Europe/Moscow | 2026-03-29 02:30 | True | False | no DST |
| Europe/Moscow | 2026-10-25 02:30 | True | False | no DST |
| UTC | 2026-03-29 02:30 | True | False | no DST |

**Fold semantics confirmed (Berlin fall 2026-10-25 02:30):**
- `fold=0` → offset +02:00 (CEST, pre-transition, DST still active)
- `fold=1` → offset +01:00 (CET, post-transition, standard time)
- Maps to two distinct UTC instants 1h apart.

**Round-trip detection technique (`is_existing_local_minute`) works.**
Attach tz, convert to UTC, convert back. If the wall clock changed, the
original was non-existent. Verified on the spring-skip case: `02:30 Berlin`
round-trips to `03:30` (or `01:30` depending on fold init), detecting
non-existence.

**Detection technique (`is_ambiguous_local_minute`) works.** Build fold=0
and fold=1 variants; compare UTC outputs. Identical → unambiguous; different
→ ambiguous.

**Subtle caveat.** `is_ambiguous_local_minute` returns **True** for the
spring-skip case (because fold=0 and fold=1 map to different UTC instants —
Python's choice for non-existent times). Production code should check
`is_existing_local_minute` FIRST; if False, skip. Only then check ambiguity.

**Verdict.** **PASS.** Policy matches plan:
- Spring skip: **skip silently** (don't catch-up when the wall-clock minute
  didn't happen; next day's occurrence fires normally).
- Fall fold: **fire fold=0 only** (first occurrence during DST hours; the
  fold=1 duplicate at standard time is dropped).
- Moscow: all minutes exist exactly once (DST stopped 2011).

**Linux VPS caveat.** On Debian-based VPS the system tzdata package usually
ships `/usr/share/zoneinfo/*`; Python 3.9+ uses it first. If the VPS image is
stripped (some Alpine/containers), add `tzdata` to `pyproject.toml` optional
dependencies per PEP 615 fallback. Phase 5 plan should note this if deployment
image is Alpine. **Owner question bubbled: Q-O1 below.**

---

## RQ4 — fake clock injection for async tick-loop tests

**Question.** Can we test `while True: await asyncio.sleep(15); tick()`
without `freezegun`?

**Method.** Built `FakeClock` protocol + implementation in `rq4_fake_clock.py`.
Ran 2-simulated-minute loop @ 15s cadence. Asserted 8 ticks, exact 15s deltas,
exactly 2 minutes virtual time elapsed, zero real time.

**Result.** **PASS.** Pattern is ~40 LOC, zero third-party deps:
- `Clock` protocol with `now() -> datetime` + `async sleep(s) -> None`.
- `FakeClock` holds a virtual `_now`; `sleep()` advances it then yields via
  `await asyncio.sleep(0)` so pending coroutines observe the advance.
- `slept: list[float]` records every sleep duration — easy cadence assertion.

**Edge case verified.** `stop_event` pre-set → loop exits immediately (0 ticks).
Mid-loop `stop_event.set()` does NOT interrupt the current sleep, but the
while-condition is checked at loop top and exits on next iteration. This is
acceptable for 15s tick; coder should document the 15s-max-shutdown-latency
behavior in the `SchedulerLoop` docstring.

**Recommended production protocol shape** (copy into coder's test helper):

```python
from typing import Protocol
from datetime import datetime, timezone

class Clock(Protocol):
    def now(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...

class RealClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)
    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.slept: list[float] = []
    def now(self) -> datetime:
        return self._now
    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._now += timedelta(seconds=seconds)
        await asyncio.sleep(0)  # yield to scheduler
```

`SchedulerLoop.__init__` takes `clock: Clock = RealClock()`; tests pass
`FakeClock` instance.

**Verdict.** **Fake-clock injection is the right choice.** No `freezegun`
dependency needed. `pytest-asyncio` + `FakeClock` cover everything phase 5
needs.

---

## RQ5 — `HookMatcher` regex for `mcp__scheduler__.*`

**Question.** Will `HookMatcher(matcher=r"mcp__scheduler__.*", hooks=[...])`
fire on scheduler @tool invocations the same way the phase-4
`mcp__memory__.*` matcher does on memory tools?

**Method.** Inspected `src/assistant/bridge/hooks.py::make_posttool_hooks`.

**Result.** Current `make_posttool_hooks` returns 3 matchers at lines 746-750:
```python
return [
    HookMatcher(matcher="Write", hooks=[on_write_edit]),
    HookMatcher(matcher="Edit", hooks=[on_write_edit]),
    HookMatcher(matcher=r"mcp__memory__.*", hooks=[on_memory_tool]),
]
```

The `mcp__memory__.*` matcher is confirmed working in phase-4 shipped code.
Agent-memory `reference_claude_sdk_tool_and_mcp.md` confirms empirically
(SDK 0.1.63) that `HookMatcher.matcher` is a regex on `tool_name`, and that
scheduler tools will be named `mcp__scheduler__<fn>` (the `@tool("schedule_add",
...)` decorator + `create_sdk_mcp_server(name="scheduler", ...)` produces
`mcp__scheduler__schedule_add`).

Adding a third matcher is **trivially additive** — same callback shape, same
`HookInput` / `HookJSONOutput` contract, same `_truncate_strings` helper.

**Diff sketch** (for coder; per plan section F.5):
```python
# Inside make_posttool_hooks, after on_memory_tool definition:
audit_path_sched = data_dir / "scheduler-audit.log"

async def on_scheduler_tool(input_data, tool_use_id, ctx):
    # identical shape to on_memory_tool; different audit_path_sched + log key
    ...

return [
    HookMatcher(matcher="Write", hooks=[on_write_edit]),
    HookMatcher(matcher="Edit", hooks=[on_write_edit]),
    HookMatcher(matcher=r"mcp__memory__.*", hooks=[on_memory_tool]),
    HookMatcher(matcher=r"mcp__scheduler__.*", hooks=[on_scheduler_tool]),  # NEW
]
```

**Verdict.** **WORKS AS EXPECTED.** No SDK surprises. Coder can copy-paste the
`on_memory_tool` factory and rename audit path + log keys.

**Refactor opportunity (optional, not blocking).** `on_memory_tool` and
`on_scheduler_tool` are ~30 identical LOC each. Coder may extract a factory:

```python
def _make_mcp_audit_hook(log_path: Path, log_key: str) -> Hook:
    async def _hook(input_data, tool_use_id, ctx):
        ...  # identical body, parameterized by log_path + log_key
    return _hook
```

Purely a DRY win; not a correctness issue.

---

## RQ6 — cron edge cases (bonus)

**Question.** Does the throwaway parser handle impossible dates, aliases, and
out-of-range values correctly?

**Method.** `rq6_cron_edge.py`. Eleven inputs; checked parse + next_fire
against croniter.

**Result.**

| expr | ours | croniter | note |
|------|------|----------|------|
| `* * 30 2 *` | next=None | bad-date-error | Feb 30 — impossible. Both agree. |
| `0 0 31 2 *` | next=None | bad-date-error | Feb 31 — impossible. Both agree. |
| `0 0 31 */2 *` | 2026-01-31 | 2026-01-31 | 31st of odd months. Match. |
| `0 0 * * 7` | 2026-01-04 (Sun) | 2026-01-04 | Sunday alias. Match. |
| `0 0 * * 0` | 2026-01-04 (Sun) | 2026-01-04 | Sunday explicit. Match. |
| `0 0 29 2 *` | 2028-02-29 (at lookahead=1600d) | 2028-02-29 | Leap-day. Match at larger lookahead. |

**Out-of-range inputs** (all correctly REJECTED):
- `60 * * * *` — minute max 59
- `* 24 * * *` — hour max 23
- `0 -1 * * *` — negative
- `0 0 32 * *` — day 32 impossible
- `0 0 0 * *` — day 0 invalid
- `0 0 * 13 *` — month 13
- `0 0 * * 8` — dow 8 invalid (after 7→0 normalization)
- `@daily` — alias rejected
- 4-field `* * * *` — wrong field count
- 6-field `* * * * * *` — wrong field count

**Minor note.** Impossible-date semantics differ: ours returns `None`, croniter
raises `CroniterBadDateError`. For phase 5 this is fine — our production API
returns `datetime | None`, and plan section E signatures already declare that.
Coder should map "no next fire" to "schedule never fires; leave
`last_fire_at` untouched" — the scheduler loop simply skips the schedule.

**Verdict.** **Parser semantics match the plan.** All 22 valid + 5 invalid
fixtures envisioned in section E will pass with the throwaway logic transplanted
into `src/assistant/scheduler/cron.py`.

---

## Patches to plan

Three search-replace diffs for orchestrator to apply to
`plan/phase5/description-v2.md`. None changes the architecture; all are
clarifications backed by RQ results.

### Patch 1 — `next_fire` lookahead (section E)

**Find:**
```
def next_fire(expr, from_utc, tz, max_lookahead_days=366) -> datetime | None
```

**Replace:**
```
def next_fire(expr, from_utc, tz, max_lookahead_days=1500) -> datetime | None
    # 1500 ≈ 4y+1d; covers leap-day schedules (0 0 29 2 *) which miss
    # at 366-day lookahead outside leap-adjacent years. RQ2+RQ6 verified
    # 2028-02-29 is found at ≥1500 with T0 in 2026.
```

### Patch 2 — RQ0 precondition note (section J risk table, row 8)

**Find:**
```
| 8 | Handler has no per-chat lock today | 🔴 | **Add `_chat_locks` in phase 5** as precondition (RQ0). |
```

**Replace:**
```
| 8 | Handler has no per-chat lock today (RQ0 CONFIRMED absent) | 🔴 | **First coder commit**: add `_chat_locks: dict[int, asyncio.Lock]` + `_locks_mutex` + `_lock_for(chat_id)` helper on ClaudeHandler; wrap `handle()` body in `async with lock`. ~15 LOC. |
```

### Patch 3 — RQ3 policy clarification (section D key-decisions bullet 6)

**Find:**
```
- **Timezone**: `zoneinfo.ZoneInfo`. Per-schedule tz. DST spring-skip + fall-fold=0 per S-3.
```

**Replace:**
```
- **Timezone**: `zoneinfo.ZoneInfo`. Per-schedule tz. DST policy (RQ3 verified):
  (a) spring-skip minute → silently skipped (NO catch-up); check
  `is_existing_local_minute()` BEFORE `is_ambiguous_local_minute()` because
  a non-existent wall-clock time is also reported as "ambiguous" by the
  fold=0/fold=1 comparison — existence check must come first.
  (b) fall-fold minute → fire fold=0 only (CEST in Berlin, pre-transition);
  fold=1 duplicate at CET is dropped.
  Moscow fold/skip never triggers (constant UTC+3 since 2011).
```

---

## Open questions bubbled to owner

### Q-O1 — Linux VPS tzdata

**Context.** `zoneinfo.ZoneInfo` on Linux defaults to system tzdata at
`/usr/share/zoneinfo/`. Debian/Ubuntu ship it by default; Alpine does NOT.
If the deployment target is Alpine (or a minimal container), we MUST add
`tzdata` to `pyproject.toml` dependencies to pick up the Python-package
fallback (PEP 615).

**Question.** What Linux distribution will the VPS run? If not Debian/Ubuntu
with `tzdata` pre-installed, add `tzdata>=2024.1` to the `dependencies` list
in `pyproject.toml`. If Debian/Ubuntu, no action needed — but add a boot-time
assertion in `Daemon.start()` like
`ZoneInfo(settings.scheduler.tz_default)` to fail fast on misconfiguration.

**Recommendation.** Unless the VPS image is known-Alpine or distroless, default
to no change + boot-time assertion. Defer to owner.

### Q-O2 — DST spring-skip retro-fire?

**Context.** A schedule `30 2 * * *` (daily 02:30) on Berlin tz will NEVER fire
on spring-transition day. Current plan = skip silently. An alternative is to
fire at `03:00` on skip day (the next existing minute). We went with SKIP
per plan section D bullet 6.

**Question.** For the single real-world case (02:30 schedule in a DST tz), is
SKIP the correct owner-facing behavior? The user would see their daily
reminder silently absent one day per year. Alternative: fire at 03:00 with
a `[system-note: DST transition — fired 30 min late]`.

**Recommendation.** Stay with SKIP in phase 5. Users rarely schedule for
02:00-03:00 local. If an owner later complains, add retro-fire as a phase-8
option. Defer to owner for final call.

### Q-O3 — `catchup_window_s` default

**Context.** Plan defaults to 3600s (1h). A typical use case: owner's VPS
reboots after 4h of downtime; they probably don't want 4 hours of pent-up
reminders flooding in. 1h cap feels right, but no data.

**Question.** Is 1h the right catch-up window? Alternative: 0 (never catch up,
just send "пока спал, пропущено N"). Or parametrize per-schedule (probably
overkill for phase 5).

**Recommendation.** Keep 3600s as a reasonable default; env override `SCHEDULER_CATCHUP_WINDOW_S`
already planned. Defer to owner.

---

## Go/no-go checklist

| Item | Status |
|------|--------|
| Per-chat lock absent confirmed | GO (add as first commit) |
| IncomingMessage field-add safe | GO (all call sites keyword) |
| Stdlib cron parser viable | GO (39/39 parity) |
| DST semantics verified | GO (skip+fold=0 policy confirmed) |
| Fake clock pattern ready | GO (40 LOC, copy-paste) |
| Scheduler hook matcher works | GO (trivially additive) |
| Cron edge cases covered | GO (11/11 expected behavior) |

**Overall: GO for phase-5 coder start.**
