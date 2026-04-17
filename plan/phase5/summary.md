# Phase 5 — Summary

Документ подводит итоги завершённой фазы 5 проекта `0xone-assistant`
(Scheduler daemon + in-process dispatcher). Источники: `plan/phase5/{description,
detailed-plan,implementation,spike-findings}.md`, исходники `src/assistant/scheduler/` +
`tools/schedule/` + `skills/scheduler/`, **14 коммитов** `50af2d8 → c100dbf`
поверх phase-4 HEAD `459a09d`, **619 passed + 1 skipped** в 19 тестовых файлах
по scheduler'у (+200 vs phase 4), lint + mypy strict зелёные.

## 1. Что было сделано

- **Self-driven turns по cron.** Пользователь говорит "напомни каждый день в 09:00
  посмотреть inbox" → модель вызывает `python tools/schedule/main.py add --cron "0 9 * * *"`
  → в 09:00 локального времени `SchedulerLoop` материализует trigger → `SchedulerDispatcher`
  дёргает `ClaudeHandler.handle(IncomingMessage(origin="scheduler", ...))` → ответ
  летит владельцу через `TelegramAdapter.send_text(OWNER_CHAT_ID)`. Ни одной новой
  зависимости — всё stdlib + существующий bridge.
- **Cron-расписания, отдельная SQLite-миграция v3** (`src/assistant/state/migrations/0003_scheduler.sql`):
  две таблицы `schedules` + `triggers` в shared `data/assistant.db`, `UNIQUE(schedule_id,
  scheduled_for)` + `INDEX(status, scheduled_for)`, state-machine `pending → sent → acked`
  плюс терминальные `dead` / `dropped`, soft-delete через `enabled=0`.
- **`SchedulerLoop` (producer) + `SchedulerDispatcher` (consumer) через `asyncio.Queue`**
  в одном процессе, одном event-loop'е (`src/assistant/scheduler/{loop,dispatcher}.py`).
  Тик-интервал 15 s, runtime revert-sweep каждые 4 тика (60 s), heartbeat `_last_tick_at`
  для Daemon health-check'а. Shutdown через `stop_event + wait_for(get, timeout=0.5)` —
  без poison-pill'а (спайк S-5 показал, что `put_nowait(POISON)` на полной queue кидает
  `QueueFull`).
- **Delivery в OWNER_CHAT_ID с `origin="scheduler"` system-note'ом.** `IncomingMessage`
  получил 5-е поле `meta: dict[str, Any] | None`; `ClaudeHandler._run_turn` прокидывает
  scheduler-note первым в `system_notes`, URL-detector — вторым (порядок, подтверждённый
  спайком S-7). Handler никогда не звонит адаптер напрямую; dispatcher собирает chunks
  и одним `send_text` отправляет в Telegram.
- **At-least-once с clean-slate + LRU + runtime revert sweep + retry re-enqueue.**
  `_inflight` set у dispatcher'а исключается из sweep'а; startup clean-slate
  (`status='sent' → 'pending'` без проверки возраста, один раз до старта consumer'а);
  LRU dedup `deque(maxlen=256)` на consumer side от дубликатов после crash'а; **retry
  pipeline** — неуспешные trigger'ы пишутся в `pending_retry` и каждый 4-й тик пушатся
  обратно в queue через `list_pending_retries` + `_reenqueue_pending_retries`
  (`src/assistant/scheduler/loop.py:167`).
- **stdlib-only CLI `tools/schedule/main.py` + скилл `skills/scheduler/SKILL.md`.**
  Subcommands `add / list / rm / enable / disable / history / run-once`, 5-field POSIX cron,
  IANA tz через `ZoneInfo` (регекс не используется — S-10 показал, что `Etc/GMT+3`
  регекс бы выкинул). Bash allowlist в `bridge/hooks.py` валидирует argv (enum subcommands,
  cron regex, prompt size cap, deny дублирующих флагов, deny `$`-метасимволов).
- **Daemon-интеграция** (`src/assistant/main.py`): advisory `fcntl.flock` на
  `data/run/daemon.pid` на входе в `Daemon.start()` (вторая копия — `exit 0` с логом
  `daemon_already_running`), origin branch (`config.scheduler.enabled`), startup-health
  (v3 миграция + catchup-miss recap одним Telegram-сообщением), health-check bg-task'а
  на scheduler loop + dispatcher heartbeat'ы.

## 2. Ключевые архитектурные решения

1. **`asyncio.Queue` вместо UDS** (wave-1 B3). Producer и consumer живут в одном процессе,
   одном event-loop'е; UDS — false complexity (stale-socket cleanup, `chmod` race,
   `EADDRINUSE`, path length limit на macOS, 40 LOC boilerplate). Phase 8 заменит queue
   на UDS без правок контракта — граница expressed через dataclass `ScheduledTrigger`.
2. **Stdlib cron parser** (`src/assistant/scheduler/cron.py`, 280 LOC). Никаких APScheduler/
   croniter — продолжаем phase-3/4 дисциплину. 5-field POSIX (`* , - /`), Sunday=0, DST
   spring-skip/fall-ambiguous через `ZoneInfo.fold` round-trip (S-3 рецепт).
   Один источник — CLI импортирует через `sys.path.insert(<project_root>/src)`, как
   phase-4 `_memlib`.
3. **Shared `assistant.db` + `ConversationStore.lock`** (спайк S-1: p99 = 3.4 ms под
   compressed load'ом, 29× ниже 100 ms budget'а). Scheduler — никакого выделенного
   aiosqlite-connection'а; один lock для всего process'а. Status-precondition в SQL
   `WHERE` — все `mark_*` transitions no-op при race'е (rowcount=0 → log skew, не raise).
4. **`fcntl.flock` mutex на `daemon.pid`** (wave-1 B1, спайк S-4). Case 5 на macOS —
   same-process different-fd тоже block'ит, защищает от двойного start'а при
   test-reloader edge case'е. Advisory-only FS (NFS/SMB/iCloud) out-of-scope (унаследовано
   из phase 4).
5. **Retry pipeline через `pending_retry` + re-enqueue sweep** (post-ship CRITICAL #1).
   Dispatcher помечает failed trigger как `pending` с `attempts+=1`, но САМ ничего не
   делает. `SchedulerLoop._reenqueue_pending_retries` каждый 4-й тик подтягивает такие
   строки (`SELECT ... WHERE status='pending' AND attempts>0 AND id NOT IN inflight`) и
   пушит обратно в queue. Без этого retry никогда бы не выполнился — `attempts` инкрементился
   бы только через revert-sweep (получился "pending_retry orphan"; см. §3).
6. **`asyncio.shield` на `mark_pending_retry` + explicit task tracking** (wave-2 B-W2-3
   + post-ship HIGH #5). При SIGTERM mid-delivery dispatcher shield'ит DB UPDATE чтобы
   не race'нуть с shutdown-cancellation'ом; `_pending_updates` set трекает shielded tasks
   так, что `Daemon.stop()` дожидается drain'а до закрытия aiosqlite conn'а (иначе
   `ProgrammingError: Cannot operate on a closed database`).
7. **`TelegramRetryAfter` retry loop в adapter** (wave-2 G-W2-1 + post-ship HIGH #3).
   `adapter.send_text` ретраит с cap'ом `TELEGRAM_MAX_RETRY_AFTER_S=120` (без cap'а
   hostile Telegram `retry_after: 3600` подвесил бы dispatcher на час).
   `_on_text` handler'а routed через `send_text` тоже (post-ship CRITICAL #2) —
   user-replies иначе обходили бы retry-loop.

## 3. Что поймали пайплайн-wave'ы

Четыре review-волны, каждая поймала свой класс ошибок:

**Devil wave-1 на plan** — 5 blockers + 14 gaps, все plan-level YAGNI/vague:
- UDS → `asyncio.Queue` (B3).
- `sent_revert_timeout_s` 300 s → 360 s (B2, claude.timeout + 60 s запас).
- startup race — `sweep_pending` до flock'а → flock first (B1).
- `origin="scheduler"` branch в `ClaudeHandler` отсутствовал (B4) — потребовал спецификации.
- `is_due` семантика DST-ambiguous → полное определение с spring-skip/fall (B5).

**Researcher wave-1** — 10 спайков (S-1..S-10, восемь PASS, один PARTIAL, один
authority-probe), эмпирически снял 5 design-question'ов до написания implementation.md:
- S-5 шейпнул shutdown pattern (stop_event + wait_for, не poison-pill).
- S-10 убил `_TZ_RE` (регекс бы потерял `Etc/GMT+3`).
- S-3 дал конкретный `is_existing_local_minute` рецепт через `fold` round-trip.

**Devil wave-2 на implementation.md v1** — 5 blockers + 10 gaps, spec-level:
- `revert_stuck_sent` был в плане, но нигде не вызывался (B-W2-1) → wired в
  `SchedulerLoop._tick` каждый 4-й тик.
- `Daemon.stop()` ordering — adapter.stop() был до bg-drain'а, handler мог dropped
  in-flight message (B-W2-2).
- `CancelledError` leak — `mark_pending_retry` не shielded (B-W2-3).
- `_TZ_RE` mentioned но не used — dead code + bypass (B-W2-4).
- dup-флаги в allowlist'е не rejected — `--cron X --cron Y` bypass (B-W2-5).

**Code-reviewer + devils-advocate final на shipped code** — **1 CRITICAL, который
упустили все 3 предыдущие волны:**
- Dispatcher помечает retry в `mark_pending_retry`, но никто не пушит строку обратно
  в queue. Revert-sweep смотрит только на `status='sent'` (через timeout), а pending_retry
  row'ы так бы и висели. Нужен **retry re-enqueue pass** — отдельный sweep на `status='pending'
  AND attempts>0`. Все три планирующие волны пропустили: plan говорил про state-machine,
  researcher проверял атомарность, devil-wave-2 проверял shielding — но "что происходит
  с failed trigger'ом между `mark_pending_retry` и next delivery?" никто не спросил.
  Исправлено в `5cfad33` (fix-pack CRITICAL #1).

**Урок**. Multi-wave review catches different things by construction:
- **Devil wave-1** находит plan-level YAGNI и ambiguities (UDS, revert-timeout, race).
- **Researcher** подтверждает/опровергает эмпирически (S-5 изменил shutdown, S-10
  убил регекс) — spikes дёшевы и меняют design до того, как coder начал.
- **Devil wave-2** ловит spec-level gaps (компонент заявлен, но нигде не вызывается).
- **Code-reviewer + devil final на код** — coder blindspot'ы (orphan retry-state'ы,
  которые не видны на уровне диаграмм).

## 4. Что НЕ делали в phase 5 (scope discipline)

- **`_memlib` → relative imports refactor.** Phase-4 tech-debt #4. Wave-2 G-W2-9 решил
  отложить: scheduler CLI импортирует через тот же `sys.path.insert` pattern, что и
  `tools/memory/`; это работает, но масштаб при третьем tool package'е ухудшается.
  Отложено в phase 6.
- **`HISTORY_MAX_SNIPPET_TOTAL_BYTES` cap.** Phase-4 tech-debt #7. Wave-2 G-W2-8:
  scheduler heavier history (multi-turn с `memory search`) → сумма snippet'ов blow out
  context может. Добавление cap'а — phase 6.
- **Out-of-process scheduler.** Phase 8 полировка — в `docs/ops/{launchd.plist.example,
  scheduler.service.example}` лежат заготовки unit-файлов, но scheduler в phase 5
  живёт как `_spawn_bg(SchedulerLoop(...).run())` внутри `Daemon`.
- **Default seed schedules.** Phase 7 (`tools/gh` + ежедневный vault git-commit).
- **Admin panel / web UI для listing расписаний.** Phase 8+.
- **Observability / Prometheus метрики.** Phase 8+ ops.
- **Human-friendly cron** ("every day at 9am" → 5-field). Модель сама генерит 5-field
  строку по SKILL.md-примерам.
- **Per-schedule allowed-tools narrowing.** Scheduler-turn получает тот же allowed_tools
  union, что и user-turn (phase-4 Q8 static intersection применяется единообразно).

## 5. Технический долг (для phase 6+)

| # | Pri | Замечание | Файл | Фаза закрытия |
|---|-----|-----------|------|---------------|
| 1 | 🟡 | `_memlib` rename + relative imports (`from tools.memory._memlib import …` + `__init__.py` в `tools/`) — sys.path collision масштабируется | `tools/schedule/main.py:1-20`, `tools/memory/main.py:1-15` | Phase 6 |
| 2 | 🟡 | `HISTORY_MAX_SNIPPET_TOTAL_BYTES` cap отсутствует — каждый tool_result truncate'ится, но сумма может blow out context | `src/assistant/bridge/history.py` | Phase 6 |
| 3 | 🟡 | Dispatcher-heartbeat health-check делает только re-notify; нет auto-restart scheduler'а при dead-loop'е | `src/assistant/main.py` (`_scheduler_health_check_bg`) | Phase 6 / 8 |
| 4 | 🟡 | Runtime sweep cadence hardcoded `_SWEEP_EVERY_N_TICKS = 4` (60 s) — пороги не в `Settings.scheduler.*` | `src/assistant/scheduler/loop.py:60-66` | Phase 6+ если будет тюнить |
| 5 | 🟢 | 99 pre-existing test-only mypy errors (tool `_lib` shadowing) — shipped-код strict, тесты нет | `tests/` | Phase 6 вместе с #1 |
| 6 | 🟢 | `test_run_notifies_on_fatal_crash` использует global monkeypatch — cosmetic | `tests/test_scheduler_loop.py` | Phase 6 cosmetic |
| 7 | 🟢 | Mid-chunk send cancel = duplicate prefix в следующей попытке (`at-least-once` — это контракт) — документировано в SKILL.md, не исправлено | `src/assistant/adapters/telegram.py` + SKILL.md | Принято как known limitation |
| 8 | 🟢 | `count_catchup_misses` walk — O(window × schedules); не проблема на ≤64 расписаниях (cap GAP #11), но на 1000+ шумно | `src/assistant/scheduler/store.py` (`count_catchup_misses`) | Phase 6+ если noisy |

## 6. Метрики

**LOC исходников phase 5:**
- `src/assistant/scheduler/` — **NEW 1388 LOC** в 5 `.py`:
  - `store.py`: 462. `loop.py`: 313. `dispatcher.py`: 293. `cron.py`: 280. `__init__.py`: 40.
- `src/assistant/state/migrations/0003_scheduler.sql` — **45 LOC** (2 таблицы + 2 индекса + `user_version=3`).
- `tools/schedule/main.py` — **335 LOC** (stdlib-only CLI).
- `skills/scheduler/SKILL.md` — **130 LOC**.
- Edits в `src/assistant/`: `main.py`, `config.py`, `bridge/hooks.py`, `bridge/claude.py`,
  `bridge/system_prompt.md`, `adapters/base.py`, `adapters/telegram.py`, `handlers/message.py`,
  `state/db.py`.
- `docs/ops/{launchd.plist.example, scheduler.service.example}` — заготовки unit-файлов для phase 8.

**LOC тестов:** +19 новых тест-файлов (`test_scheduler_*` + `test_schedule_cli` +
`test_telegram_*`). **619 passed + 1 skipped** (+200 vs phase 4's 419).

**Коммиты phase 5:** 14 (`50af2d8`..`c100dbf`): 8 initial (main pass) + 6 fix-pack
(post-review). Диф по `src/` + `tools/` + `skills/`: **+2526 / −26**. Общий диф (всё
включая тесты + plan-артефакты + spikes): **+13541 / −54** в 67 файлов.

**Spike artifacts:** 10 файлов `spikes/phase5_*.py` + 10 JSON reports (~1700 LOC).

**CI-gates:** `uv sync` OK, `just lint` зелёный (ruff + ruff format + mypy src/ strict),
`just test` — 619 passed в ~14 s.

**Pipeline waves:** 10-stage sequence по шаблону memory (plan → Q&A → devil-1 → revision →
researcher-1 → devil-2 → researcher fix-pack → coder → code-review+devil-final →
coder fix-pack). 12 Q&A-вопросов, все Recommended. Календарное время: один день
(2026-04-17).

## 7. Уроки

1. **Research is gold — эмпирические spike'и меняют design empirically, не теоретически.**
   S-5 (`asyncio.Queue` shutdown semantics) изменил shutdown pattern с poison-pill'а на
   `stop_event + wait_for(get, timeout)`. S-10 (`ZoneInfo` authority) выбросил регекс
   `_TZ_RE` вообще — стандартная библиотека авторитетнее любого фильтра. Без спайков
   обе ошибки попали бы в shipped код.
2. **Multi-wave review catches different things by construction.** Devil wave-1 ловит
   plan-level YAGNI (UDS) и ambiguities. Researcher закрывает эмпирически. Devil wave-2
   ловит spec-level gaps (компонент заявлен, но нигде не вызывается — `revert_stuck_sent`
   был orphan в v1). Code-reviewer final на код ловит coder blindspot'ы, которых нет
   на уровне диаграмм (retry-pipeline orphan — ни одна planning-волна его не поймала,
   нашли только на коде).
3. **`asyncio.shield` + bare `raise` is subtle — требует explicit тест.** B-W2-3 wave-2
   спецификации написал shield, но coder первой версии мог легко забыть `await` на
   shielded task'е (orphan task → `ProgrammingError` на shutdown). Post-ship HIGH #5
   добавил explicit `_pending_updates` set + `Daemon.stop()` ждёт drain'а. Тест
   `test_scheduler_shield_drain` фиксирует контракт.
4. **Status-precondition SQL вместо in-memory state check.** Все `mark_*` transitions
   проверяют `WHERE status='<expected>'` — rowcount=0 означает skew (concurrent sweep,
   operator delete, race). Dispatcher логирует `state_skew` и продолжает, не raise'ит.
   Это превращает race conditions из crash-режимов в observable анти-инварианты.
5. **`asyncio.Queue` + heartbeat-before-put — неочевидный invariant.** Post-ship CRITICAL #4:
   `queue.put()` блокирует producer при полной queue; `_last_tick_at` должен
   обновляться ДО `put`, иначе health-check читает loop как мёртвый пока dispatcher
   медленно drain'ит. Backpressure ≠ liveness failure — heartbeat должен отражать
   "я итерирую", а не "я успешно put'ил".

---

Phase 5 закрыт. Self-driven turns по cron работают E2E: `add --cron "*/5 * * * *"
--prompt "ping"` → через ≤300 s Telegram receives ответ. Рестарт Daemon'а посреди
срабатывания: trigger'ы revert'ятся на startup clean-slate, LRU гасит дубликаты,
`UNIQUE(schedule_id, scheduled_for)` гарантирует at-least-once idempotence на
unique ключе. Retry pipeline (failed → `pending_retry` → re-enqueue каждый 4-й тик)
закрыт post-ship'ом. 14 коммитов, 619 тестов зелёные. Phase 6 (media / image flow)
разблокирован; унаследованный phase-4 техдолг (`_memlib` + HISTORY cap) адресуется
в phase 6.
