# Phase 5 — Spike Findings (2026-04-17)

Empirical verification of the 400-line phase-5 detailed plan. Eight live
spikes (S-1..S-8) plus one offline fixture table (S-9) targeting the
questions the researcher raised against the design. Scripts live under
`/Users/agent2/Documents/0xone-assistant/spikes/phase5_*.py`; raw JSON
output alongside as `phase5_s<N>_report.json`.

## Verdict table

| ID | Question | Verdict | Action for implementation.md |
|----|----------|---------|-------------------------------|
| S-1 | One aiosqlite conn + asyncio.Lock absorbs scheduler+handler inserts under load? | **PASS** — p99 = 3.4 ms (two orders of magnitude under 100 ms budget) | Single conn + single lock is sufficient; no second connection. |
| S-2 | `TelegramAdapter.send_text` from a background task alongside polling is safe? | **PASS** — 100/100 calls OK, no exception, no deadlock, ~0.3 s wall | Dispatcher may call `adapter.send_text(OWNER, body)` directly. |
| S-3 | `zoneinfo.ZoneInfo` correctly marks spring-skip non-existent / fall-ambiguous? | **PASS** — round-trip test detects spring skip; fold semantics correct | Implement `is_existing_local_minute` + `is_ambiguous_local_minute` via fold round-trip (see §S-3 below). |
| S-4 | `fcntl.flock(LOCK_EX\|LOCK_NB)` releases on SIGKILL and raises clean `BlockingIOError` on second attempt? | **PASS** on macOS (Darwin) — `errno.EAGAIN`, released on SIGKILL and clean exit | Advisory flock approved. ALSO: second fd in **same process** also blocks (case 5) — double-start inside Daemon is guarded. |
| S-5 | `asyncio.Queue(maxsize=N)` blocks producer, `put_nowait(POISON)` works on full queue? | **PARTIAL PASS** — backpressure works; `put_nowait(POISON)` RAISES `QueueFull` on a full queue | Shutdown must use `await queue.put(POISON)` OR the `stop_event + wait_for(get, timeout)` consumer pattern. See §S-5 below. |
| S-6 | `IncomingMessage` has `meta` field? `"scheduler"` in Origin literal? Handler branches on origin? | **MIXED** — Origin accepts "scheduler" ✓; `meta` field MISSING ✗; handler does NOT currently branch on origin ✗ | Add `meta: dict[str, Any] \| None = None` (field #5). Handler branches are a new write. |
| S-7 | `ClaudeBridge.ask` preserves `system_notes` insertion order? | **PASS** — simple `for note in system_notes: ... .append(...)` loop, FIFO | Caller just constructs list as `[scheduler_note, url_note]`. No bridge code change. |
| S-8 | INSERT+UPDATE+commit inside one `async with lock` is atomic vs concurrent reader? | **PASS** — 808 reader samples, 0 violations of "trigger exists without last_fire_at" | No `BEGIN IMMEDIATE` needed for this invariant. Note caveat: multi-statement SELECTs on reader side have no snapshot guarantee — use single-statement JOIN if dispatcher needs consistent `(trigger, schedule)` read. |
| S-9 | Fixture table for cron parser tests | **READY** — 8 expressions over 24-hour window starting 2026-04-15T08:00:00Z (Wednesday) | Coder uses these fixtures directly in `test_scheduler_cron_semantics.py`. |

## Pipeline readiness: **GO to devil wave-2**.
No blocker found. One nuance (S-5 shutdown path) shapes the design but doesn't require re-planning.

---

## S-1 — aiosqlite contention

**Script:** `spikes/phase5_s1_aiosqlite_contention.py`
**Report:** `spikes/phase5_s1_report.json`

### Method

- 1 aiosqlite connection, 1 asyncio.Lock (simulating the shared lock in §1.11).
- Producer A: 100 `INSERT INTO triggers_stub` at 15 ms cadence (mimics a
  compressed scheduler tick — the real tick is 15 s but we compress
  the test to measure contention).
- Producer B: 100 `INSERT INTO conversations_stub(blob=4096 bytes)` at 50 ms cadence.

### Results

```
"triggers":      p50=0.98ms  p95=1.80ms  p99=3.43ms  max=3.43ms
"conversations": p50=0.81ms  p95=1.61ms  p99=4.67ms  max=4.67ms
"combined":      p50=0.84ms  p95=1.74ms  p99=3.43ms  max=4.67ms
"p99_under_100ms": true
```

### Verdict

p99 is **29× below the 100 ms pass threshold**. One aiosqlite connection
with a single asyncio.Lock (the existing `ConversationStore.lock`) is
sufficient for phase-5 workloads. Scheduler gets no dedicated connection.
Real production load will be dramatically lower (15 s tick ≠ 15 ms).

---

## S-2 — TelegramAdapter.send_text from background task

**Script:** `spikes/phase5_s2_adapter_send_text.py`
**Report:** `spikes/phase5_s2_report.json`

### Method

Monkey-patch `Bot.send_message` on a real `TelegramAdapter` instance
(no network traffic). Spawn `polling_like()` and `dispatcher_like()` as
concurrent coroutines; each calls `adapter.send_text(...)` 50 times.
Verify count, absence of exceptions, and clean session teardown.

### Results

```
"calls_observed": 100
"exceptions":     []
"wall_seconds":   0.314
"pass":           true
```

The call log shows tight interleaving: `poll-0, sched-0, poll-1, sched-1, ...` — confirming both tasks make progress concurrently. No deadlock, no
"session closed" error, no `aiogram` internal lock contention surfaced.

### Verdict

Dispatcher may call `adapter.send_text(OWNER, body)` directly. No
additional mutex or send-queue needed between polling and dispatcher.

### Caveat (not a blocker)

`aiogram` makes actual HTTP requests through `Bot.session`; we only
stubbed `send_message`, not `session`. Real rate-limit behaviour under
simultaneous polling + dispatcher calls is a Telegram-API concern, not
an `aiogram` bug. Mitigation: Telegram's 30-msg/second global limit and
our single-user scope mean scheduler-turns add ≤ ~2 msgs/min even in
worst case.

---

## S-3 — zoneinfo DST spring-skip + fall-ambiguity

**Script:** `spikes/phase5_s3_zoneinfo_dst.py`
**Report:** `spikes/phase5_s3_report.json`

### Method & results

For `Europe/Berlin` 2026-03-29 (spring) and 2026-10-25 (fall): build
`datetime(..., tzinfo=tz, fold=0/1)`, round-trip via UTC, compare.

```
spring_2_30_exists: False ✓   fall_2_30_exists:    True ✓
spring_1_30_exists: True  ✓   fall_2_30_ambiguous: True ✓
spring_3_30_exists: True  ✓   fall_1_30_ambiguous: False ✓
fall_2_30_utc_matches_fold0: ["2026-10-25T00:30:00+00:00"] (ONE UTC instant)
```

Fold-0 policy picks pre-transition occurrence (summer UTC+2 → wall 02:30);
post-transition is skipped. `is_due` iterates UTC minutes, so spring-skip
happens for free (no UTC minute projects to the skipped local minute).

### Helpers (drop into `cron.py`)

```python
def is_existing_local_minute(naked: datetime, tz: ZoneInfo) -> bool:
    for fold in (0, 1):
        aware = naked.replace(tzinfo=tz, fold=fold)
        back = aware.astimezone(ZoneInfo("UTC")).astimezone(tz).replace(tzinfo=None)
        if back == naked.replace(fold=0):
            return True
    return False

def is_ambiguous_local_minute(naked: datetime, tz: ZoneInfo) -> bool:
    a = naked.replace(tzinfo=tz, fold=0)
    b = naked.replace(tzinfo=tz, fold=1)
    return a.utcoffset() != b.utcoffset() and is_existing_local_minute(naked, tz)
```

---

## S-4 — fcntl.flock(LOCK_EX|LOCK_NB) on macOS

**Script:** `spikes/phase5_s4_flock_semantics.py`
**Report:** `spikes/phase5_s4_report.json`

Five cases on darwin:

```
case1 holder subprocess holds 5s, main tries LOCK_NB  → BlockingIOError errno=35 (EAGAIN) ✓
case2 SIGKILL holder, main retries                     → acquired ✓
case3 holder exits cleanly, main retries               → acquired ✓
case4 same proc, same fd, re-flock                     → ok (idempotent) ✓
case5 same proc, DIFFERENT fd on same path             → BlockingIOError ✓
```

Case 5 is a bonus: macOS treats per-file-description locking strictly
enough that a leaked pidfile fd from a prior `Daemon.start()` in the
same process would self-block. Good — protects against test-reloader
edge case.

### Implementation sketch (plan §1.13 verbatim ok)

- `fd = os.open(pid_path, os.O_RDWR | os.O_CREAT, 0o644)` once.
- `fcntl.flock(fd, LOCK_EX | LOCK_NB)` → on `BlockingIOError` log
  `daemon_already_running` + `sys.exit(0)`.
- `os.ftruncate(fd, 0)` + `os.write(fd, f"{os.getpid()}\n".encode())`.
- Keep `fd` on `self._pid_fd` until process exit.
- Close explicitly in `Daemon.stop()` (best-effort).

Caveats pre-acknowledged by plan §1.13: NFS/SMB/iCloud advisory no-op
(out of scope); Windows missing `fcntl` (out of scope — phase-0
pre-condition is macOS+Linux).

---

## S-5 — asyncio.Queue backpressure + poison-pill shutdown

**Script:** `spikes/phase5_s5_queue_backpressure.py`
**Report:** `spikes/phase5_s5_report.json`

### Method

Four cases:
- A: producer does 5 `put()` on a `Queue(maxsize=3)` — expect block after 3rd.
- B: fill queue to 3, then `put_nowait(POISON)` — expect `QueueFull`.
- C: fill queue to 3, then `await put(POISON)` in parallel — poison lands
  after consumer drains.
- D: alternative design: consumer with `stop_event` + `wait_for(get, timeout=0.05)`.

### Results

```
caseA_queue_full_after_3_puts      : True
caseA_producer_blocked             : True (task not done at t=0.05 s)
caseA_producer_finished            : True (after consumer drains)
caseB_put_nowait_poison_on_full    : "QueueFull"   ← RAISES
caseC_poison_task_blocked_on_full_q: True
caseC_drain_order                  : ["0","1","2"]   (FIFO)
caseC_final_item_is_poison         : True
caseD_stop_event_clean_exit        : True
```

### Verdict

Backpressure works. **Critical design note:** `put_nowait(POISON)` on a
full queue **raises `QueueFull`** — it does NOT pre-empt the queue. The
shutdown path in plan §5.5 must use one of:

- **Option A (recommended):** `await queue.put(POISON)` — blocks until a
  slot frees, then consumer sees poison and exits. Requires the consumer
  to keep draining (which it does — no deadlock).
- **Option B:** consumer uses `stop_event + wait_for(get, timeout=0.05)`;
  producer never sends poison; stop_event.set() wakes consumer via timeout.
  Simpler, no poison-pill needed. Slight CPU overhead from timer, negligible
  (20 timeouts/sec × 2 μs).

**Plan §5.5 recommendation**: use **Option B** — it avoids the awkward
blocking-put-poison case and matches the producer pattern (producer also
checks `stop_event` between ticks). Consumer sketch:

```python
async def run(self) -> None:
    while not self._stop.is_set():
        try:
            t = await asyncio.wait_for(self._queue.get(), timeout=0.5)
        except TimeoutError:
            continue
        try:
            await self._deliver(t)
        finally:
            self._inflight.discard(t.trigger_id)
```

Implementation.md must specify this pattern — otherwise coder may
implement `put_nowait(POISON)` and hit the `QueueFull` path at shutdown.

---

## S-6 — IncomingMessage shape + origin enum reality

**Script:** `spikes/phase5_s6_incoming_message_shape.py`
**Report:** `spikes/phase5_s6_report.json`

| # | name | type | default | status |
|---|------|------|---------|--------|
| 1 | `chat_id` | `int` | REQUIRED | unchanged |
| 2 | `text` | `str` | REQUIRED | unchanged |
| 3 | `message_id` | `int \| None` | `None` | unchanged |
| 4 | `origin` | `Origin = Literal['telegram','scheduler']` | `'telegram'` | ✓ already accepts `"scheduler"` |
| 5 | *(missing)* | — | — | **ADD** `meta: dict[str, Any] \| None = None` |

```
origin_literal_accepts_scheduler : true
meta_field_exists                : false   ← TypeError on IncomingMessage(..., meta={...})
handler_branches_on_origin       : false   ← no origin branch today (confirms wave-1 B4)
bridge_ask_has_system_notes_param: true    ← phase-3 URL detector already uses it
bridge_iterates_notes_in_order   : true    ← see S-7
```

Deltas required:

1. `adapters/base.py`: add 5th field `meta: dict[str, Any] | None = None`
   (+ `from typing import Any`).
2. `handlers/message.py::_run_turn`: new scheduler-note branch (see
   implementation.md §3.12).
3. `bridge/claude.py`: docstring only — merge order is already correct.

---

## S-7 — system_notes merge order

**Script:** `spikes/phase5_s7_system_notes_order.py`
**Report:** `spikes/phase5_s7_report.json`

Source merge block in `bridge/claude.py::ask::prompt_stream` (~lines 303-310):

```python
content_blocks: list[dict[str, str]] = [{"type": "text", "text": user_text}]
for note in system_notes:
    content_blocks.append({"type": "text", "text": f"[system-note: {note}]"})
```

FIFO by list iteration. Replay with `["NOTE_A_scheduler","NOTE_B_url"]`:

```
[0] {"text": "HELLO"}
[1] {"text": "[system-note: NOTE_A_scheduler]"}
[2] {"text": "[system-note: NOTE_B_url]"}
```

**Caller-controlled.** Handler builds `[scheduler_note, url_note]` in
that order. No bridge code change; docstring clarifies contract.

---

## S-8 — try_materialize_trigger atomicity

**Script:** `spikes/phase5_s8_try_materialize_atomicity.py`
**Report:** `spikes/phase5_s8_report.json`

Writer: 20 × `INSERT OR IGNORE` + `UPDATE last_fire_at` + `commit` inside
one `async with lock`. Reader loop on a separate connection polls
`triggers COUNT` and `schedules.last_fire_at` tightly, flagging any
sample where `trig_count > 0 AND last_fire_at IS NULL`.

```
reader_samples     : 808
violations_count   : 0       ← zero torn reads ✓
final_trigger_count: 20
final_last_fire_at : "2026-04-17T09:19:00Z"
```

**No `BEGIN IMMEDIATE` needed**. The lock+commit envelope is atomic
enough.

Documentation note: reader side used two separate SELECT statements.
SQLite gives statement-level consistency, not multi-statement snapshot.
If the dispatcher needs consistent `(trigger, schedule)` reads elsewhere,
use a single JOIN:

```sql
SELECT s.id, s.last_fire_at, MAX(t.scheduled_for)
FROM schedules s LEFT JOIN triggers t ON t.schedule_id = s.id
WHERE s.id = ? GROUP BY s.id
```

---

## S-9 — Cron fixture table (offline)

**Script artifact:** `spikes/phase5_s9_cron_fixtures.json`

Expected next-fire UTC minute-boundaries for each expression, over the
24-hour window starting `2026-04-15T08:00:00Z` (Wednesday). Coder uses
these directly in `tests/test_scheduler_cron_semantics.py`.

Note: 2026-04-15 is a **Wednesday**, so fixtures that require
weekends/specific-days produce 0 fires in this window. Coder may shift
the fixture window to cover those — plain-table reproducibility requires
only that the rule produces the expected set.

| Expression | Label | Total fires in 24 h | Sample |
|------------|-------|---------------------|--------|
| `0 9 * * *` | daily at 09:00 | 1 | 2026-04-15T09:00Z |
| `*/15 * * * *` | every 15 min | 96 | 08:00, 08:15, 08:30, ..., 07:45 next day |
| `0 9 * * 1-5` | weekdays 09:00 | 1 | 2026-04-15T09:00Z |
| `30 0 1,15 * *` | days 1+15, 00:30 | 0 | — (Apr 15 ≠ day 1; no 00:30 in window either since window starts 08:00) |
| `0 9 1 * *` | day-1 09:00 | 0 | — |
| `*/5 * * * *` | every 5 min | 288 | 08:00, 08:05, ... |
| `0 0 * * 0` | sundays midnight | 0 | — (no Sunday in window) |
| `15,45 * * * 6` | saturdays :15 and :45 | 0 | — (no Saturday in window) |

### Additional fixtures (over broader windows)

For completeness, implementation.md test file should also include:

- `0 9 * * *` over 2026-04-15T08:00Z — 2026-04-17T10:00Z: **2 fires**
  (`2026-04-15T09:00Z`, `2026-04-16T09:00Z`).
- `0 0 * * 0` over 2026-04-15T00:00Z — 2026-04-20T00:00Z: **1 fire**
  (`2026-04-19T00:00Z` = Sunday).
- `15,45 * * * 6` over 2026-04-18T00:00Z — 2026-04-19T00:00Z (Saturday):
  **48 fires** (minutes 15 and 45 of each hour × 24 hours).
- DST spring: `30 2 * * *` for Europe/Berlin, observing 2026-03-28 → 03-30:
  fires on 03-28T01:30Z, skipped on 03-29 (non-existent local 02:30),
  fires on 03-30T00:30Z. (One warn-log for the skip.)
- DST fall: `30 2 * * *` for Europe/Berlin, 2026-10-25: **1 UTC fire**
  (00:30Z = local 02:30 summer) — post-transition 02:30 winter is skipped.

---

## S-10 — `zoneinfo.ZoneInfo` authority probe (wave-2 fix-pack)

**Script:** `spikes/phase5_s10_zoneinfo_authority.py`
**Report:** `spikes/phase5_s10_report.json`

### Context

Wave-2 devil B-W2-4: CLI in §3.9 defines `_TZ_RE = r"^[A-Za-z_]+(/[A-Za-z_]+(/[A-Za-z_]+)?)?$"`
but never uses it — AND the regex would reject legitimate IANA names
that contain `+` (e.g. `Etc/GMT+3`, `Etc/GMT-12`). The fix is to drop
the regex entirely and rely on `ZoneInfo(name)` as authoritative.

### Method

Call `ZoneInfo(name)` on 9 candidate names spanning legitimate IANA
identifiers (with and without `/` separator), one path-injection
attempt, out-of-range Etc zones, empty string, and plausible-but-fake
names. Classify outcomes.

### Results (darwin, Python 3.12.13)

```
UTC                              → accepted
Europe/Berlin                    → accepted
Etc/GMT+3                        → accepted       ← regex would REJECT (has '+')
CST6CDT                          → accepted       ← single-segment IANA
America/Argentina/Buenos_Aires   → accepted       ← 3-segment IANA
../../etc/passwd                 → ValueError     ← NOT ZoneInfoNotFoundError
Etc/GMT+99                       → ZoneInfoNotFoundError
(empty string)                   → ValueError     ← NOT ZoneInfoNotFoundError
Europe/NotACity                  → ZoneInfoNotFoundError
```

### Verdict

**`zoneinfo` is authoritative.** Regex would LOSE legitimate names
(`Etc/GMT+3` is the canonical fixed-offset zone for UTC-3; `+` sign is
reversed per POSIX `Etc/` convention).

### Implementation delta

- Drop `_TZ_RE` from `tools/schedule/main.py`.
- In `cmd_add`: wrap `ZoneInfo(args.tz)` in `try/except (ZoneInfoNotFoundError, ValueError)`.
  Both error types occur in practice (ValueError on injection / empty string;
  ZoneInfoNotFoundError on fake names).
- In `bridge/hooks.py::_validate_schedule_argv`: for `--tz VALUE`, only
  enforce `len(VALUE) <= 64`, ASCII printable, no shell metachars (the
  latter is caught globally by the hook's existing shell-char deny list).
  Structural shape is CLI's job.

### Why this is safer than regex

A tightly-scoped regex cannot keep up with IANA additions (e.g. 2024's
`America/Ciudad_Juarez`). The stdlib resolver walks `TZPATH` / the
Python-bundled tzdata — authoritative by construction. Injection risk
is zero: `ZoneInfo` refuses non-normalized paths (`ValueError` above) and
never opens arbitrary files; it reads from `sys.prefix/share/zoneinfo`
or OS `/usr/share/zoneinfo` only.

---

## Summary of design changes triggered by spikes

| Spike | Plan text | Required change to implementation.md |
|-------|-----------|----------------------------------------|
| S-1 | §1.11 shared conn | No change; confirm in pitfalls. |
| S-2 | §5.4 dispatcher → send_text | No change; but wave-2: add `TelegramRetryAfter` retry inside adapter (G-W2-1). |
| S-3 | §2.2 DST handling | Provide concrete `is_existing_local_minute` recipe; document UTC-minute iteration as natural skip. |
| S-4 | §1.13 flock | No change; add case-5 same-process-same-path edge case to invariants (§16 invariant #1 already covers it). |
| S-5 | §5.5 shutdown | **Use `stop_event + wait_for(get, timeout)` pattern; DO NOT `put_nowait(POISON)`.** This is the main concrete guidance for the coder. |
| S-6 | §5.4 IncomingMessage meta | Concrete delta: add `meta: dict[str, Any] \| None = None`; add handler origin branch (new code). |
| S-7 | §1.6 note order | Caller responsibility; add unit-test asserting order. |
| S-8 | §5.3 atomicity | Confirmed; startup dispatcher JOIN read for consistency. |
| S-9 | §9.1 fixtures | **Wave-2 expanded to 22 valid + 5 invalid cases** (Feb-31, Feb-29 leap/non-leap, `*/7` non-even step, DOW=7 reject, 3-DOW list, biz-hours combo). Use `spikes/phase5_s9_cron_fixtures.json` directly. |
| S-10 | §3.9 tz validation | Drop `_TZ_RE`; `ZoneInfo(...)` + `(ZoneInfoNotFoundError, ValueError)` catch is sole authority. |
