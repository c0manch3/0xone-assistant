---
phase: 5b
title: scheduler @tool MCP server — cron + asyncio.Queue + at-least-once delivery
date: 2026-04-24
status: shipped (owner smoke pending after VPS redeploy)
sdk_pin: claude-agent-sdk>=0.1.59,<0.2
auth: OAuth via claude CLI (no ANTHROPIC_API_KEY)
---

# Phase 5b — Summary

Phase 5b добавляет третий MCP server — `scheduler` — 6 @tool функций
над SQLite-хранилищем, в-процессовый tick-loop + dispatcher с
at-least-once семантикой доставки, per-chat lock в `ClaudeHandler`
(новая feature), и end-to-end scheduler-turn proactive delivery через
`IncomingMessage(origin="scheduler")` в тот же `ClaudeHandler.handle(...)`
пайплайн.

516 tests passing (+29 от post-fix-pack baseline после phase 4's 387).
ruff + mypy --strict clean (31 source files). Production code ~1830 LOC
(6 scheduler files + 2 tools_sdk) + ~2100 LOC tests в 24 файлах + runbook
+ systemd unit in deploy/.

Phase 5 split на 5a (VPS migration, предыдущий commit) + 5b (этот commit).

---

## 1. Что shipped

### 1.1 `src/assistant/tools_sdk/scheduler.py` (~427 LOC) + `_scheduler_core.py` (~168 LOC)

- **`SCHEDULER_SERVER`** = `create_sdk_mcp_server(name="scheduler", version="0.1.0", tools=[...6 @tool...])`.
- **`SCHEDULER_TOOL_NAMES`** кортеж из 6 `mcp__scheduler__*` имён.
- **6 `@tool` handlers** с mixed input-schema policy:
  - **JSON Schema form (optional fields → explicit `required: [...]`):**
    `schedule_add(cron, prompt, tz?)` (required=[cron,prompt]),
    `schedule_list(enabled_only?)` (required=[]),
    `schedule_history(schedule_id?, limit?)` (required=[]).
  - **Flat-dict form (все поля required):**
    `schedule_rm({path: int, confirmed: bool})`,
    `schedule_enable({id: int})`,
    `schedule_disable({id: int})`.
- **`configure_scheduler(data_dir, owner_chat_id, settings)`** одноразовая идемпотент.
- **`_scheduler_core.tool_error(msg, code)`** (локальный envelope helper, byte-identical клон как в phase-3 installer + phase-4 memory — code-review H4 accepted debt; объединим когда будет 4-й MCP server в phase 8).
- **`validate_cron_prompt(prompt)`** — CR-3 layer 1: reject control chars (code 3) + `[system-note:|[system:` anywhere (code 10, **не только** в начале строки — fix-pack H2) + sentinel-tag fragments + **Cyrillic/Greek homoglyph fold** (fix-pack H3 — `[sуstеm-nоtе:` теперь ловится после NFKC+fold).
- **`wrap_untrusted_prompt(prompt)`** — reuses `_memory_core.wrap_untrusted` с тегом `untrusted-scheduler-prompt`. Используется в `schedule_list` output (fix-pack H1: spec §B.2 was violated до fix-pack'а).
- **`wrap_scheduler_prompt(body)`** (dispatch-time) — генерит hex(6) nonce + ZWSP-scrub любого литерального `</scheduler-prompt-*>` в body + wrap в `<scheduler-prompt-{nonce}>...</scheduler-prompt-{nonce}>`. CR-3 layers 2+3.

### 1.2 `src/assistant/scheduler/` package (~1195 LOC)

- **`store.py` (~511 LOC)** — `SchedulerStore(conn)` с internal `_tx_lock: asyncio.Lock` (CR-2 fix: не импортит lock снаружи). Методы: `add_schedule`, `list_schedules`, `try_materialize_trigger(schedule_id, prompt_snapshot, scheduled_for)` (UNIQUE-gated idempotent), `mark_sent(id)` (**с `AND status='pending'` guard** — fix-pack C-1), `mark_acked`, `mark_dropped`, `mark_dead`, `revert_to_pending`, `clean_slate_sent` (boot-time only), `sweep_expired_sent(now, timeout_s)` (**CR2.1** — tick-time, не только boot), `count_catchup_misses`, `reclaim_pending_not_queued(inflight)` (**фильтрует по `scheduled_for` НЕ `created_at`** — H2.3, + после fix-pack только строки с `last_error LIKE 'queue saturated%'`), `get_schedule_history`, `note_queue_saturation` (scoped `WHERE status IN ('pending','sent')` — fix-pack C-3 не stomp'ит terminal).

- **`cron.py` (~292 LOC)** — stdlib POSIX 5-field parser. `parse_cron(s) → CronExpr` (`* , - /` поддержка; Sun=0 или 7; отвергает aliases/extensions). `is_due(expr, last_fire_at, now_utc, tz, catchup_window_s)`, `next_fire(expr, from_utc, tz, max_lookahead_days=1500)` (**1500 для leap-year Feb-29** — RQ2). `is_existing_local_minute` + `is_ambiguous_local_minute` (**existence check BEFORE ambiguity** — RQ3). DST spring-skip: silent skip. DST fall-fold: fire на fold=0 только.

- **`loop.py` (~208 LOC)** — `SchedulerLoop` producer. Tick every `tick_interval_s=15`: (1) **CR2.1 `store.sweep_expired_sent(now, sent_revert_timeout_s)`** ПЕРВЫМ; (2) scan enabled schedules; (3) для каждой due cron-минуты — `try_materialize_trigger`; (4) на новый trigger — `put_nowait(ScheduledTrigger)` + `mark_sent`. На `asyncio.QueueFull` → `note_queue_saturation(last_error="queue saturated@{ts}")` + continue (фиксируется на следующем tick'е). Outermost try/except на `_tick_once` — bubble up ClaudeHandler-level errors to supervisor.

- **`dispatcher.py` (~204 LOC)** — `SchedulerDispatcher` consumer. `wait_for(queue.get, timeout=0.5)` + stop_event. LRU dedup 256 slots. Для каждого trigger:
  1. Re-check `schedules.enabled` (disabled → `mark_dropped`).
  2. **CR2.2 читает `trig.prompt` (снапшот из triggers)**, не `schedules.prompt` (mutable).
  3. Wrap prompt через `wrap_scheduler_prompt` + marker prefix `"scheduled prompt from id=N:\n<scheduler-prompt-{nonce}>{prompt}</...>"`.
  4. `IncomingMessage(origin="scheduler", meta={trigger_id, schedule_id})` → `handler.handle(msg, emit)` (per-chat lock сериализует с user-turn).
  5. Post-handler: accumulated text → `adapter.send_text(owner_chat_id, ...)`. **Empty-output check (fix-pack H4): если final is empty/whitespace-only → revert_to_pending, НЕ mark_acked.**
  6. Success → `mark_acked`. Exception → `revert_to_pending(attempts+=1)`. Attempts ≥ 5 → `mark_dead` + one-shot owner notify.

- **`__init__.py`** — namespace + дocstring объясняет почему нет re-exports (циклический import risk).

### 1.3 `src/assistant/state/db.py` — migration 0003

Inline `_apply_0003` + `SCHEMA_VERSION=3`. Добавляет `schedules` + `triggers` таблицы с `UNIQUE(schedule_id, scheduled_for)` + `INDEX(status, scheduled_for)`. WAL mode + `busy_timeout=5000` унаследованы. Применяется идемпотентно если SCHEMA_VERSION уже 3.

### 1.4 `src/assistant/main.py` — Daemon wiring

После `configure_memory(...)` и перед `ClaudeBridge`:
- `configure_scheduler(data_dir, owner_chat_id, settings.scheduler)`.
- `SchedulerStore(self._conn)` (без внешнего lock — CR-2).
- `reverted = clean_slate_sent()` + log.
- `boot_type = classify_boot(marker_path, max_age_s=60)` — reads marker mtime (M2.6).
- **`unlink_clean_exit_marker(marker_path)`** сразу после classify_boot (M2.7).
- `missed = count_catchup_misses(now, catchup_window_s)`.
- После `adapter.start()`: queue + `SchedulerDispatcher` + `SchedulerLoop` через `_spawn_bg_supervised`.
- **Supervisor (fix-pack C-2):** в `except asyncio.CancelledError` — `task.cancel()` + `await asyncio.shield(asyncio.wait({task}, timeout=5.0))` прежде чем re-raise. Гарантирует clean SQLite conn close.
- `Daemon.stop()` → atomic `write_clean_exit_marker` (tmp → rename → **chmod 0o600** — fix-pack F15).

### 1.5 `src/assistant/handlers/message.py` — per-chat lock + scheduler origin branch

- **Новая feature (wave-1 CR-1):** `_chat_locks: dict[int, asyncio.Lock]` + `_locks_mutex: asyncio.Lock` + `_lock_for(chat_id)` helper. `handle()` wrapping via `_handle_locked()`.
- **Scheduler origin branch:** если `msg.origin == "scheduler"` → compose `scheduler_note = f"autonomous turn from scheduler id={trigger_id}; owner is not active at this moment; answer proactively and concisely"` + (optional url_note) в список `system_notes=` → passed в bridge.ask.
- **Fix-pack C1 (critical):** внутри `_handle_locked`, на `ClaudeBridgeError`: если `msg.origin == "scheduler"` → re-raise (так dispatcher увидит exception и revert_to_pending). User-origin сохраняет apology-chunk behavior.

### 1.6 `src/assistant/bridge/claude.py` — wiring + system_notes

- `from assistant.tools_sdk.scheduler import SCHEDULER_SERVER, SCHEDULER_TOOL_NAMES`.
- `allowed_tools=[..., *INSTALLER_TOOL_NAMES, *MEMORY_TOOL_NAMES, *SCHEDULER_TOOL_NAMES]`.
- `mcp_servers={"installer": ..., "memory": ..., "scheduler": SCHEDULER_SERVER}`.
- **`ask()` gained `system_notes: list[str] | None = None`** kw-only param. Joined as `[system-note: N]\n\n[system-note: M]` string-concat and appended to `user_text_for_envelope` (H-7 — string-concat safer than `list[dict]` content envelopes; no SDK spike needed).

### 1.7 `src/assistant/bridge/hooks.py` — scheduler audit hook

Третий `HookMatcher(r"mcp__scheduler__.*", hooks=[on_scheduler_tool])` в `make_posttool_hooks`. `on_scheduler_tool` пишет JSONL в `<data_dir>/scheduler-audit.log` (0o600, без rotation — phase-9 debt). Использует общий `_truncate_strings` helper (2 KiB per string value).

### 1.8 `src/assistant/bridge/system_prompt.md` — scheduler blurb (~6 lines)

```
## Scheduler
You have a scheduler via `mcp__scheduler__*` tools...
```

### 1.9 `src/assistant/adapters/base.py` — IncomingMessage fields

```python
@dataclass(frozen=True)
class IncomingMessage:
    chat_id: int
    message_id: int
    text: str
    origin: Literal["telegram", "scheduler"] = "telegram"
    meta: dict[str, Any] | None = None
```

RQ1 verified: все 3 call sites Kwargs-only — backward compatible.

### 1.10 `src/assistant/config.py` — SchedulerSettings nested

```python
class SchedulerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCHEDULER_", ...)
    enabled: bool = True
    tick_interval_s: int = 15
    tz_default: str = "UTC"
    catchup_window_s: int = 3600
    dead_attempts_threshold: int = 5
    sent_revert_timeout_s: int = 360   # > claude.timeout (300)
    dispatcher_queue_size: int = 64
    max_schedules: int = 64
    missed_notify_cooldown_s: int = 86400   # currently unused — phase 5c
    reclaim_older_than_s: int = 30          # fix-pack F2 threshold
```

Env vars: 11 `SCHEDULER_*`. Задокументированы в `.env.example` (fix-pack F11).

### 1.11 `skills/scheduler/SKILL.md` (~90 LOC, `allowed-tools: []`)

Prompt-only guidance. Cron primer (5-field POSIX, Sun=0/7), examples (`0 9 * * *`, `*/15 * * * *`, etc.), когда вызывать `schedule_add` vs `schedule_list` vs `schedule_rm`. DST quirks documented. Note про homograph warning.

### 1.12 `deploy/systemd/` (committed в фазе 5a)

`0xone-assistant.service` + `README.md` install recipe. Включает `TimeoutStopSec=30s` (devops fix — гарантия marker-write до SIGKILL).

### 1.13 `plan/phase5/runbook.md` (120+ LOC)

8 секций: data layout, env reference, SQL diagnostic queries, DR recipes (`clean_slate` behavior, `sqlite3 .backup`, manual reindex), known quirks (DST fold/spring-skip, 15s tick latency, catchup window), troubleshooting tree для "bot не ответил на scheduled fire".

---

## 2. Architectural decisions frozen

### @tool input schema — mixed (per phase-4 RQ7 + phase-5 RQ1)

`schedule_add` / `schedule_list` / `schedule_history` имеют optional поля → JSON Schema form с `required: [...]`. `schedule_rm` / `schedule_enable` / `schedule_disable` — все поля required → flat-dict form. `memory_delete` pattern из phase 4 unchanged.

### at-least-once, not exactly-once

`UNIQUE(schedule_id, scheduled_for)` + LRU dedup 256 slots. Exactly-once через Telegram невозможно (нет 2PC). Model prompt text сам себя объясняет когда видит duplicate fire (edge case, rare).

### Shared `assistant.db` с migration `0003`

Phase-4 memory ушло в отдельный `memory-index.db` из-за FTS5 profile. Scheduler — low-throughput rows (1-2/мин), identical profile с conversations/turns. Spike S-1 подтвердил p99=3.4ms под contention. Один меньше DB чтобы бекапить.

### In-process `asyncio.Queue`, not UDS

Phase 8 out-of-process split через `ScheduledTrigger` dataclass как wire format (JSON-line через UDS). В phase 5 — single-process, Queue maxsize=64 absorbs bursts.

### Policy B auto-reindex для memory (carry-over from phase 4)

Scheduler не меняет этот path. Но `classify_boot()` + `.last_clean_exit` marker логика новая в phase 5.

### Tick 15s, catchup 3600s drop+recap, IANA tz only

Owner decisions (frozen в Q&A). RQ2 proved `max_lookahead_days=1500` нужно для leap-year Feb-29.

---

## 3. Pipeline mechanics — 11-step workflow

Standard pipeline plus one deviation (coder continuation): agent hung mid-session, second coder agent picked up commit 4-14.

### 3.1 Devil wave 1 — 3 CRITICAL caught pre-coder

- **CR-1** per-chat lock не существовал (grep 0 matches) — plan framed as "verify", pushed to NEW feature (~15 LOC + test) at first coder commit.
- **CR-2** `SchedulerStore(self._conn, lock=store.lock)` упал бы AttributeError на boot. Fix: internal `_tx_lock`.
- **CR-3** scheduler prompts bypass untrusted wrap на firing path. 3-layer defense added.

### 3.2 RQ0-RQ6 live spikes

- RQ0 per-chat lock verify (грустное подтверждение — absent).
- RQ1 IncomingMessage field-add safety — ✓ all call sites kwarg-only.
- RQ2 stdlib cron parser vs croniter — 39/39 parity + discovery `max_lookahead_days=1500` для `0 0 29 2 *`.
- RQ3 zoneinfo DST — existence-check order matters, documented.
- RQ4 FakeClock protocol 40 LOC (no freezegun dep).
- RQ5 HookMatcher regex trivially additive.
- RQ6 (bonus) cron edge cases — Feb-31, `*/0`, etc., all reject cleanly.

### 3.3 Devil wave 2 — 3 CRITICAL on patched plan

- CR2.1 `sent_revert_timeout_s=360` dead config → sweep_expired_sent method.
- CR2.2 dispatcher should read `triggers.prompt` (snapshot), не `schedules.prompt` (mutable).
- CR2.3 `COUNT(*) + INSERT` cap advisory — doc'd, не race-critical.

### 3.4 Researcher fix-pack — 1447-line implementation-v2.md

Description-v2.md патчен inline (8 edits). implementation-v2.md создан. 13 HIGH integrations (queue put_nowait, supervision, clean-exit marker, DST order, FakeClock).

### 3.5 Coder

First coder session stopped после commits 1-3 (declared scope overwhelming). Second coder continuation completed commits 4-14 successfully, plus fixing phase-1..4 test regressions caused by schema 3. Final: 487 passed / 1 pre-existing failure / 3 skipped; ruff + mypy strict clean.

### 3.6 Parallel reviewers — 4 выявили +12 items

- **code-review** — 2 CRITICAL (mark_sent race, reclaim re-queue) + 1 HIGH (dead 200-row SELECT) + 3 minor.
- **qa-engineer** — 0 CRITICAL + 4 HIGH (schedule_list unwrapped — spec §B.2 violation; embedded [system-note:; Cyrillic bypass; empty-output silent ack) + 6 MEDIUM. **487 tests independently verified**.
- **devil wave 3** — 2 CRITICAL (ClaudeBridgeError swallowed; supervisor CancelledError race) + 2 HIGH.
- **devops** — Needs-polish (no blockers) — systemd TimeoutStopSec missing, assistant.db backup undocumented, 10 env vars not in .env.example, no runbook.

### 3.7 Fix-pack — 15 items, +29 tests → 516 passed

Все 4 CRITICAL (code-review C-1/C-2 + devil C1/C2) + 6 HIGH (dead code, schedule_list wrap, anchored regex, homoglyph, empty-output, terminal-row stomp) + 5 ops (env docs, runbook, systemd unit + TimeoutStopSec, marker chmod) applied. Post-fix: 516 passed, 1 pre-existing failure, 3 skipped.

---

## 4. Owner E2E smoke — pending

Pre-requisite: deploy commits 5a+5b на VPS через `git pull && uv sync && systemctl --user restart 0xone-assistant`.

- **AC#1 schedule_add** — `запланируй напомнить "test ping" через каждые 2 минуты` → ожидаем `mcp__scheduler__schedule_add(cron="*/2 * * * *", prompt="test ping")` → id=1 saved.
- **AC#2 cron fire** — в течение ≤120s приходит proactive Telegram message.
- **AC#3 schedule_list** — `покажи расписания` → model invokes `schedule_list` → shows id=1 с wrapped prompt (untrusted-scheduler-prompt sentinel).
- **AC#4 schedule_rm** — `удали расписание 1` → `schedule_rm(id=1, confirmed=true)` → enabled=0 → no further fires.
- **AC#5 schedule_history** — `что срабатывало за последний час?` → `schedule_history` lists pending/acked triggers.
- **AC#6 regressions phase 1-4** — ping skill, memory search flowgent (будет fail если seed drift не починен), installer marketplace_list.
- **AC#7 restart clean_slate** — owner перезапускает daemon через `systemctl --user restart` в момент активного fire → on reboot trigger revert to pending → повторная доставка (at-least-once).

---

## 5. Test landscape — 516 passing

Post-fix-pack:
- **Phase 1-4 regressions:** 387 → preserved (test fixes applied для schema 3 migration).
- **Phase 5b scheduler tests:** +129 new (tests/test_scheduler_*.py в 24 файлах).
- **1 pre-existing failure:** `test_memory_search_seed_flowgent` — Mac vault не содержит `flowgent.md` (seed drift, документирован). Не relate к phase 5b.
- **3 skipped:** integration tests behind `ENABLE_*_INTEGRATION=1` env gate.

Key test files:
- `test_scheduler_cron_parser.py` — 22 valid + 5 invalid fixtures.
- `test_scheduler_cron_semantics.py` — is_due, DST spring-skip, fall-fold, catchup edge.
- `test_scheduler_store.py` — CRUD, UNIQUE, CASCADE, idempotency.
- `test_scheduler_tool_*.py` — 6 files (happy + error codes).
- `test_scheduler_loop_fakeclock.py` — FakeClock-driven producer loop.
- `test_scheduler_dispatcher_lifecycle.py` — consumer, retries, mark_dead.
- `test_scheduler_dispatcher_reads_trigger_prompt.py` — CR2.2 invariant.
- `test_scheduler_sweep_expired_sent.py` — CR2.1 tick-time revert.
- `test_scheduler_recovery.py` — clean_slate_sent on boot.
- `test_scheduler_origin_branch.py` — IncomingMessage scheduler path.
- `test_handler_per_chat_lock_serialization.py` — CR-1 new feature.
- `test_daemon_clean_exit_marker.py` — write+classify+unlink cycle.
- `test_scheduler_prompt_rejects_system_note.py` — CR-3 layer 1 (anchored + embedded + sentinel-tag fragments).
- `test_scheduler_prompt_rejects_cyrillic_lookalike.py` — fix-pack H3 homoglyph fold.
- `test_scheduler_dispatch_marker.py` — CR-3 layer 2+3 (nonce wrap + ZWSP scrub).
- `test_scheduler_list_wraps_prompts.py` — fix-pack F6 spec §B.2 compliance.
- `test_scheduler_dispatcher_empty_output_reverts.py` — fix-pack F9.
- `test_scheduler_queue_full_put_nowait.py` + `test_scheduler_loop_reclaim_only_saturated.py` — queue saturation behavior + C-3 terminal-row preservation.
- `test_scheduler_handler_reraises_bridge_error.py` — fix-pack F3 dead-letter integrity.
- `test_scheduler_store_mark_sent_guard.py` — fix-pack C-1 race prevention.
- `test_supervisor_cancels_inner_on_shutdown.py` — fix-pack F4 shutdown race.
- `test_clean_exit_marker_permissions.py` — fix-pack F15 0o600 chmod.
- `test_memory_integration_ask.py` — NH-20 debt closer (gated, real OAuth).
- `test_scheduler_integration_real_oauth.py` — scheduler end-to-end gated.

---

## 6. Known debt / carry-forwards

### Phase 5c (prioritized backlog)

- **Dead config `missed_notify_cooldown_s`** — currently never read. Either wire (throttle crash-loop recap spam) or remove.
- **Sentinel regex scrub gap** — `wrap_scheduler_prompt` ZWSP-scrubs only `scheduler-prompt`, not `untrusted-*` fragments. Defense-in-depth gap.
- **ZoneInfo empty-string** raises ValueError unhandled → leaks to model.
- **Unicode digits** in cron accepted (e.g. `٠ ٩ * * *` — Arabic digits). Niche case.
- **Reclaim retry envelope narrowed** — post-fix-pack only queue-saturation rows eligible for reclaim. Bridge errors / empty output revert rely on next cron-minute materialisation. If observed in practice to lose triggers, phase 5c broadens.

### Phase 9 (accepted for now)

- **`tool_error` duplicated 3× now** (installer + memory + scheduler). Refactor after 4th MCP server (phase 8 gh).
- **`scheduler-audit.log` no rotation** (Q-R4 deferred per phase-4 pattern; single-user low disk-fill).
- **`assistant.db` no backup recipe beyond runbook** — schedule it via cron to remote host when phase 8 gh push lands.
- **27 `mcp__*` tools total** (7 installer + 6 memory + 6 scheduler + 8 built-in). NH-7 auto-invoke inflation worth measuring; phase 9 `disallowed_tools` budget.

### Phase 6+ prerequisites

- **Phase 6 (media)** — scheduler-fired turn may trigger media tools; ensure origin="scheduler" propagation works.
- **Phase 7 (daily vault git commit)** — first real seeded schedule (`schedule_add cron="0 3 * * *" prompt="commit vault"`). Phase 5b не seeds — ждёт когда phase 7 добавит gh CLI.
- **Phase 8 (out-of-process scheduler + gh push)** — migrate to `launchd`/`systemd` separate unit + UDS. `ScheduledTrigger` dataclass becomes wire format. Singleton discipline across hosts becomes crucial (OAuth session divergence).

---

## 7. Lessons learned (additional to phase 4)

- **Per-chat lock is a prerequisite that stayed invisible until phase 5.** Phase-2 description implied it existed, phase-3+4 never added it. Phase-5's scheduler + user-turn concurrency forced the feature. Moral: verify "X exists" claims via grep before planning.
- **Reviewers in parallel catch non-overlapping bugs.** code-review found state-machine race (C-1 no `status='pending'` guard), devil found exception-swallow on scheduler path (C1), QA found spec-violations (schedule_list unwrapped) + homoglyph bypass. Different mental models find different bugs.
- **Coder agents can hang without signalling.** First coder session declared scope-overwhelm, walked away with partial work. Second continuation picked up. Orchestrator patience (wait for auto-notification) vs poll-and-nudge balance — wait, but sanity-check every 20-30 min via `git status` to see progress indirectly.
- **Independent pytest verification beats coder self-report.** Reviewers independently confirmed 487 passed (pre-fix-pack). Monitor output eventually proved 516 after fix-pack. Trust but verify.
- **Pipeline discipline pays.** Skipping devil wave 2 or parallel reviewers would have shipped 6 CRITICAL and 10 HIGH directly to production. Each review wave caught bugs the prior waves missed, especially when the fix-pack itself introduced new edges.
- **OAuth session transfer trick (Mac Keychain → Linux file) is reusable** — documented in memory `reference/vps_deployment.md` for future host migrations.
- **Clean-exit marker has subtle races** — (a) SIGKILL before atomic rename → no marker, false suspend-detect; (b) Stale marker from 2 days ago confusing classify_boot → mitigated by mtime check. Phase 5b incorporated both mitigations via M2.6/M2.7.

---

## References

- `plan/phase5/description-v2.md` (patched, 520 lines).
- `plan/phase5/implementation-v2.md` (coder blueprint, 1447 lines).
- `plan/phase5/devil-wave-{1,2,3}.md`.
- `plan/phase5/spike-findings-v2.md` (RQ0-RQ6 live).
- `plan/phase5/review-{code,qa,devops}.md`.
- `plan/phase5/runbook.md` (operational diagnostics).
- `plan/phase5a/summary.md` (VPS migration).
- `deploy/systemd/0xone-assistant.service` (committed).
- Phase 4 summary (`plan/phase4/summary.md`) for pivot lineage.
- Memory: `project_phase4_shipped.md`, `reference_vps_deployment.md`,
  `reference_claude_agent_sdk_gotchas.md`.
