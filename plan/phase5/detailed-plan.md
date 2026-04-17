# Phase 5 — детальный план (Scheduler daemon + in-process dispatcher)

Phase 5 замыкает контур "self-driven" бота: до сих пор turn'ы начинались только с Telegram-сообщения владельца. Теперь бот может сам инициировать turn по cron-расписанию (ежедневное саммари vault, weekly review). Ключевой архитектурный риск — надёжная доставка trigger'а без дубликатов между separate lifecycle-ветками на фоне того, что scheduler и bot делят один процесс.

## Changes from wave-0 (для orchestrator triage)

Пять блокеров, найденных wave-1 devil's-advocate, закрыты:

- **B1 startup race (`sweep_pending`):** добавлен advisory `flock` на `<data_dir>/run/daemon.pid` на самом входе в `Daemon.start()` до любых DB-операций. Вторая копия daemon'а выходит с `exit 0` и логом `daemon_already_running`. См. §1.13 и §8.1.
- **B2 revert-timeout:** `sent_revert_timeout_s` поднят до `claude.timeout + 60s` = **360s** по default (env `SCHEDULER_SENT_REVERT_TIMEOUT_S`); добавлен in-memory `_trigger_ids_in_flight: set[int]` у `SchedulerDispatcher`, который исключается из revert sweep. См. §1.4 и §8.2.
- **B3 IPC via `asyncio.Queue`:** UDS выброшен полностью, in-process IPC = bounded `asyncio.Queue[ScheduledTrigger]` между `SchedulerLoop` (producer) и `SchedulerDispatcher` (consumer). Нет сокет-файла, нет `chmod`, нет `EADDRINUSE`. Phase 8 сможет поменять queue на UDS в отдельном процессе — boundary выражен через dataclass `ScheduledTrigger`. См. §1.3, §5, §6.
- **B4 `origin="scheduler"` branch в `ClaudeHandler`:** ветка отсутствует в текущем `handlers/message.py` — специфицирована. Scheduler-note идёт первым в `system_notes`, URL-detector note — вторым. См. §1.6 и §11.4.
- **B5 `last_fire_at` / `is_due` семантика:** полное описание функции `is_due(expr, last_fire_at, now, tz)` с катч-ап, DST spring/fall и минутными границами. Test-plan пополнен `test_scheduler_cron_semantics.py` (30+ кейсов). См. §2.2, §9.1.

Шесть спец-level добавлений (из GAPS):

- **GAP #11:** `SCHEDULER_MAX_SCHEDULES=64`, CLI `add` проверяет cap (§3.2).
- **GAP #12:** startup revert policy для `status='sent'` = clean-slate (после crash in-flight set пустой), без проверки возраста (§8.2).
- **GAP #15:** outermost try/except в `SchedulerLoop.run()` шлёт one-shot Telegram-notify (cooldown 24h, marker-file pattern phase-3) (§8.4).
- **GAP #16:** startup missed-triggers recap — одно Telegram-сообщение "пропущено N напоминаний" если ≥ 1 catch-up miss (§8.3).
- **GAP #18:** один источник cron-парсера — `src/assistant/scheduler/cron.py`; CLI импортирует через `sys.path.insert` на `<project_root>/src` (как `_memlib` phase-4) (§6).
- **GAP #2:** dispatcher фильтрует `schedule.enabled=0` перед delivery; терминальный статус `dropped` (§8.2).

## 1. Архитектурные решения с tradeoff'ами

### 1.1. Scheduler как отдельный процесс vs `asyncio.create_task` внутри Daemon'а

**Опции:**
- (A) Отдельный процесс `daemon/main.py`. Плюсы: изоляция crash'ей, launchd/systemd готовность. Минусы: deployment complexity, второй venv, shared DB single-writer.
- (B) `_spawn_bg(...)` внутри существующего `Daemon`. Плюсы: один процесс, один connection, готовый `_bg_tasks` drain, SIGINT propagate автоматом. Минусы: crash scheduler'а = crash бота.

**Рекомендуется: (B)**. `Daemon` уже single-user, single-process; отсутствие супервизора делает (A) без outer restart'ера эквивалентным (B) по отказоустойчивости. Phase 8 вынесет в отдельный процесс — boundary между producer (`SchedulerLoop`) и consumer (`SchedulerDispatcher`) выражен через `asyncio.Queue[ScheduledTrigger]`, который phase-8 заменит на UDS.

### 1.2. Cron-парсер: APScheduler vs `croniter` vs stdlib

**Опции:**
- (A) APScheduler — overkill, полноценный scheduler с jobstore.
- (B) `croniter` ~800 LOC pure-python — тёплый API, доп зависимость.
- (C) stdlib 5-field POSIX parser, ~180 LOC.

**Рекомендуется: (C)** — продолжаем stdlib-only дисциплину phase-3/4. Никаких `@reboot`/`@yearly`; 5-field покрывает 95% use-case'ов. **Один источник:** `src/assistant/scheduler/cron.py`; CLI (`tools/schedule/main.py`) импортирует его через `sys.path.insert(<project_root>/src)` — ровно как phase-4 `_memlib` импортируется из `src/assistant/memory/*`. Ни одной дубликатной копии.

### 1.3. Транспорт trigger'а: UDS vs DB-poll vs `asyncio.Queue`

**Опции:**
- (A) UDS `data/run/bot.sock` + line-JSON. Плюсы: instant delivery, готовая абстракция для out-of-process. Минусы: 40 LOC boilerplate, stale-socket cleanup, `chmod` race, `EADDRINUSE` на второй start, path length limits на Mac.
- (B) DB polling — задержка = poll interval.
- (C) In-process `asyncio.Queue[ScheduledTrigger]` — producer put, consumer get в том же event-loop'е.

**Рекомендуется: (C)**. UDS в phase 5 — false complexity: producer и consumer живут в одном процессе, в одном event-loop'е, и никогда не будут запущены отдельно до phase 8. Все risk-row'ы про UDS (stale socket, chmod race, path length) уходят. Delivery state-machine сохраняется (pending → sent → acked) — authoritative status в DB. Queue — только сигнальный канал, идёт поверх DB-записи.

Queue параметры:
- `maxsize=64` — при переполнении `await queue.put(...)` блокирует tick-loop (естественный backpressure). Scheduler ticks 15-секундные; 64 unprocessed trigger'ов = ≥16 минут дрейфа, повод для warn-лога.
- Producer: `SchedulerLoop.run()` на каждом tick материализует due-triggers, для каждого делает `_trigger_ids_in_flight.add(id)` потом `await queue.put(ScheduledTrigger(...))`.
- Consumer: `SchedulerDispatcher.run()` = `while not stop: t = await queue.get(); try: await self._deliver(t); finally: self._inflight.discard(t.trigger_id)`.

Phase 8 сможет без изменений интерфейса `ScheduledTrigger` вынести consumer в отдельный процесс (UDS = wire-format для того же dataclass'а).

### 1.4. Delivery semantics: at-least-once с in-flight-set + LRU dedup

**State machine:**

```
(schedule due) → INSERT OR IGNORE triggers(..., status='pending')
              → _inflight.add(id)
              → await queue.put(trigger)          [producer side]
              → UPDATE status='sent', sent_at=now [producer, after put]
              → (consumer picks up)
              → await _deliver(trigger)
              → UPDATE status='acked', acked_at=now
              → _inflight.discard(id)
                 [on error: UPDATE status='pending', attempts+=1, last_error=...]
```

**Revert sweep:**

```sql
UPDATE triggers
SET status='pending', attempts=attempts+1
WHERE status='sent'
  AND (julianday('now') - julianday(sent_at))*86400 > :timeout_s
  AND id NOT IN (:inflight_ids);
```

**Ключевые параметры:**
- `sent_revert_timeout_s` = `claude.timeout + 60s` = **360s** по default. Env `SCHEDULER_SENT_REVERT_TIMEOUT_S`. Обоснование: scheduler-turn с memory-операциями идёт 60-180s; нужен запас выше timeout'а claude-turn'а, иначе revert сработает до того как consumer дойдёт до mark_acked.
- `_inflight` set живёт в `SchedulerDispatcher` и передаётся в `SchedulerLoop.run()` как referent; sweep читает `list(self._dispatcher.inflight())` на момент вызова.
- LRU dedup (`_recent_acked_trigger_ids`, 256 slots) на consumer side — страхует от того, что после crash и revert тот же id может придти вторым trigger'ом: check `if id in LRU: skip`.
- **Invariant:** `_inflight ⊆ {trigger.id | status='sent'}` в любой момент (см. §16).

**Crash между put и mark_sent:** producer put'нул в queue, не успел `UPDATE status='sent'`, consumer ещё не начал. Crash. При restart: `_inflight` пустой, trigger в DB `status='pending'` (не перешёл в 'sent'), next tick перевыдаст. Unique `(schedule_id, scheduled_for)` защитит от двойного INSERT.

**Crash между mark_sent и deliver:** trigger в `status='sent'`, `_inflight` пустой. Startup revert sweep (GAP #12) делает clean-slate: UPDATE всех `status='sent'` в `status='pending'` **без проверки возраста** (один-раз, на boot). Документируем: "post-crash clean-slate policy, runs ONLY at boot, before dispatcher starts accepting".

**Crash после deliver, до ack:** аналогично sent→pending на startup; LRU пуст после restart, но Claude SDK ничего не запомнит (conversation запись была сделана), чистый duplicate turn. Мы принимаем эту уязвимость (at-least-once), размер LRU 256 держим как runtime-safeguard.

### 1.5. Scheduler-injected IncomingMessage — какой `chat_id` и как?

**Рекомендуется:** `chat_id = OWNER_CHAT_ID` + `origin="scheduler"`. Scheduler-turn идёт в ту же conversation, что и обычные диалоги; единая история критична для job'ов "сравни с вчерашним саммари". Изоляция через `origin` hint в system-note (см. §1.6).

### 1.6. System-notes merge order (phase-2 + phase-5)

Сейчас `ClaudeHandler._run_turn` строит `system_notes: list[str] | None`:
- если есть URL-detector matches → `system_notes = [note]`.

Phase-5 добавляет второе условие. **Порядок детерминирован:**

1. `scheduler_note` (если `msg.origin == "scheduler"`) — идёт ПЕРВЫМ.
2. `url_note` (если detected) — идёт ВТОРЫМ.

Текст scheduler_note:
```
"autonomous turn from scheduler id=%d; owner is not active; "
"do not ask clarifying questions, answer proactively and finish"
```

(где `%d` — `trigger_id`, хранится в `IncomingMessage.meta` как `{"trigger_id": N}` — новое поле в dataclass, опциональное).

`ClaudeBridge.ask(... system_notes=system_notes)` без изменений семантики merge'а — просто итерирует и добавляет каждый note как `{type: text, text: "[system-note: <note>]"}`. Добавляем docstring-параграф в `bridge/claude.py::ask`:

> "If `system_notes` has multiple entries, they are appended in the order supplied. Callers MUST sort by priority before passing: phase-5 convention is `[scheduler_note, url_note]` — scheduler context first, URL hint second, so model reads origin before acting on URL."

LOC estimate: `handlers/message.py` +20 LOC (origin branch + trigger_id extraction); `bridge/claude.py` +5 LOC (docstring only, no code change).

### 1.7. Долгий scheduler-turn vs одновременный user-turn

Phase-2 `ClaudeHandler._chat_lock` уже сериализует turn'ы на одном `chat_id`. Scheduler-trigger берёт тот же lock, ждёт user-turn'а. Alt (parallel) потребовал бы conversation-history fork'а — overkill. Monitoring: warn если `triggers WHERE status='pending' AND (now-created_at) > 5min` > 0.

### 1.8. Зависимость на memory CLI из scheduler-job'а

Scheduler-turn — это обычный `ClaudeHandler`-turn. Видит manifest всех скилов. `_effective_allowed_tools` делает union всех allowed-tools ∩ baseline — Bash доступен, memory CLI работает. Per-turn scheduler-only narrowing не делаем (phase-4 S-A.3 показал SDK не партиционирует hooks per-skill).

### 1.9. Clock drift / DST

Per-schedule `tz` (IANA), default `SCHEDULER_TZ` env (default "UTC"). `zoneinfo` stdlib Python 3.9+. DST обработка — см. §2.2 (B5 блокер).

### 1.10. Cron-синтаксис: 5-field only

5-field POSIX. Никаких `@daily`/`@reboot`. Модель знает синтаксис из training data + ~10 примеров в SKILL.md.

### 1.11. Storage: shared `assistant.db` vs dedicated

Рекомендуется shared `assistant.db` (phase-1 migration pattern готов; новая v3 migration). Логически: `schedules` authoritative, как `conversations`.

### 1.12. Durability: WAL + нет `fsync`

`PRAGMA journal_mode=WAL, busy_timeout=5000` уже есть (`state/db.py`). Scheduler-tick = идемпотентный `INSERT OR IGNORE`; crash до commit'а → next tick materialize'нёт снова. `fsync` не нужен.

### 1.13. Мьютекс между двумя daemon-процессами (B1 блокер)

**Проблема:** launchd/systemd рестарт + ручной запуск владельца → две копии daemon'а. Текущий `TurnStore.sweep_pending()` (`state/turns.py:61-75`) **безусловно** UPDATE'ит все `status='pending'` turn'ы в `interrupted`. Если daemon A только что inject'нул scheduler-turn (записал pending), daemon B на startup sweep'нет его.

**Выбран fix (a) — advisory flock на pid-file:**

```python
import fcntl
pid_path = settings.data_dir / "run" / "daemon.pid"
pid_fd = os.open(str(pid_path), os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.flock(pid_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    log.warning("daemon_already_running", pid_path=str(pid_path))
    sys.exit(0)
os.ftruncate(pid_fd, 0)
os.write(pid_fd, f"{os.getpid()}\n".encode())
# fd hold'ится до конца процесса (flock auto-released on close/exit)
self._pid_fd = pid_fd  # сохраняем чтобы не закрылся
```

**Почему (a), не (b):**
- (a) закрывает race для **любого** startup-действия, не только `sweep_pending`. Будущие phase-8 housekeeping job'ы получат мьютекс бесплатно.
- (a) не требует epoch-column в `turns` и миграции v4.
- Код — 10 LOC.

**Документированные ограничения (acceptance):**
- `flock` — advisory на POSIX; процесс, не использующий flock, может всё равно стартовать. Мы явно принимаем: **наши** daemon-процессы все проходят через `Daemon.start()`.
- Windows out of scope (pre-condition всего проекта: macOS + Linux).
- NFS-моунты: advisory flock может не работать корректно. Out of scope — `data_dir` по default `~/.local/share/0xone-assistant` (local FS).
- stale pid-file после kill -9: `flock` сам освобождается на close fd при exit процесса; файл не удаляется, но это ок (next daemon перезапишет содержимое).

**Interaction с sweep_pending:** оставляем `TurnStore.sweep_pending()` как есть — после flock'а мы точно единственный процесс, sweep семантически корректен (pending turn'ы это orphan'ы предыдущего crashed daemon'а). Гарантия: flock ⇒ единственность ⇒ sweep не гасит чужие pending.

## 2. DB schema details

Migration: `src/assistant/state/migrations/0003_scheduler.sql`. Применяется через `_apply_v3` в `state/db.py`. Bump `SCHEMA_VERSION=3`.

### 2.1. DDL

```sql
-- 0003_scheduler.sql — phase 5: scheduler daemon + triggers ledger

CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cron          TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    tz            TEXT NOT NULL DEFAULT 'UTC',
    enabled       INTEGER NOT NULL DEFAULT 1,   -- 0 = soft-delete
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_fire_at  TEXT                          -- scheduled_for последнего успешно вставленного trigger'а
);
CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON schedules(enabled);

CREATE TABLE IF NOT EXISTS triggers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id   INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    prompt        TEXT NOT NULL,                -- snapshot на момент материализации
    scheduled_for TEXT NOT NULL,                -- ISO-8601 UTC минутная граница
    status        TEXT NOT NULL DEFAULT 'pending',
                                                -- pending | sent | acked | dead | dropped
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

`status` codes:
- `pending` — материализован, ждёт delivery.
- `sent` — producer put'нул в queue + UPDATE sent_at.
- `acked` — consumer успешно доставил, turn завершён (неважно success/error).
- `dead` — `attempts > 5`, manual-inspect required, one-shot Telegram notice.
- `dropped` — (новый терминальный) schedule был disabled между insert и delivery.

### 2.2. `is_due()` — семантика (B5 блокер)

Функция:

```python
def is_due(
    expr: CronExpr,
    last_fire_at: datetime | None,   # stored UTC ISO, None если никогда не срабатывало
    now: datetime,                    # UTC current
    tz: ZoneInfo,                     # schedule timezone
    catchup_window_s: int = 3600,     # SCHEDULER_CATCHUP_WINDOW_S
) -> datetime | None:
    """
    Return the minute-boundary UTC datetime `t` such that:
      - last_fire_at < t <= now
      - t converted to `tz` matches `expr` in all five fields
      - now - t <= catchup_window_s
    If multiple `t` match (missed ticks during suspend): return the LATEST.
    If none match: return None.
    If catchup exceeds window: return None + log `scheduler_catchup_miss`.
    """
```

**Cadence 15s vs cron 1-min:**
- На tick в 09:00:00, `last_fire_at = None` → `is_due` вернёт `t = 09:00:00Z` (minute boundary). `INSERT OR IGNORE` с `scheduled_for='09:00:00Z'` → успех. `last_fire_at` UPDATE'нется **только** если `INSERT` создал row (rowcount>0) → `last_fire_at=09:00:00Z`.
- На tick в 09:00:15, `last_fire_at=09:00:00Z` → `is_due` проверяет: есть ли match между `09:00:00 (эксклюзив) и 09:00:15 (инклюзив)`? Только minute-boundaries рассматриваются; `09:00:15` не minute boundary; следующая — `09:01:00Z`. `09:01 > 09:00:15` → нет match → `None`. Возврат `None` → skip.
- На tick в 09:00:30: аналогично, `None`.
- На tick в 09:01:00: `last_fire_at=09:00:00Z`, next candidate `09:01:00`, `09:00 < 09:01 <= 09:01:00` → match если `expr` подходит для 09:01.

**Идемпотентность INSERT:** `is_due` может вернуть тот же `t` дважды (если между call'ами `last_fire_at` не успел обновиться — например, INSERT успел, UPDATE нет, и процесс крашнулся). Unique `(schedule_id, scheduled_for)` гарантирует второй `INSERT OR IGNORE` = no-op, `rowcount=0`; соответственно `last_fire_at` во второй раз не обновится — но он и так уже правильный (`09:00`).

**DST spring-forward (non-existent minute):**

Пример: `Europe/Moscow` 2026-03-29, cron `30 2 * * *`. Местное время "02:30" не существует (переход 02:00→03:00). `is_due` итерируется по минутам между `last_fire_at` и `now`, конвертирует каждую в `tz`. Non-existent local time — `zoneinfo` по default даст `fold=0` и валидный UTC, но local representation будет "неправильный" (02:30 интерпретируется как 03:30 local). Мы явно проверяем: `if local_dt.replace(fold=1) != local_dt.replace(fold=0): skip non-existent`. Warn-лог one-time per schedule per year: `scheduler_dst_spring_skip schedule_id=X expected_local=2026-03-29T02:30:00`.

**DST fall-back (ambiguous minute):**

`Europe/Moscow` 2026-10-25, cron `30 2 * * *`. Local "02:30" встречается дважды (02:30 BEFORE переход и 02:30 AFTER). **Match на первом occurrence (`fold=0`), skip второго.** Документируем в SKILL.md: "в день перехода на зимнее время cron-трigger срабатывает один раз — в первом проявлении указанного часа".

**catchup_window_s:**

После compute `t`: если `(now - t).total_seconds() > catchup_window_s` (default 3600), возврат `None`, лог `scheduler_catchup_miss schedule_id=X missed_t=<iso>`. MacBook suspend 8 часов → просыпается, sees `last_fire_at=yesterday-09:00`, many potential `t` in between; latest из них более часа назад → drop. Ежедневные 09:00 миссы НЕ бэкфилятся.

**`last_fire_at` invariant:**

`last_fire_at == max(scheduled_for)` по `triggers WHERE schedule_id=X AND status != 'pending-duplicate'`. Точнее: UPDATE только когда `INSERT OR IGNORE` родил новую row (`cursor.rowcount == 1`). См. `SchedulerStore.try_materialize_trigger()` в §5.2.

## 3. CLI contract — `tools/schedule/main.py`

### 3.1. Structure

Stdlib-only. Импорт cron-парсера через `sys.path.insert` (GAP #18):

```python
# tools/schedule/main.py — top of file
import sys
from pathlib import Path
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent   # <project_root>
sys.path.insert(0, str(_ROOT / "src"))
from assistant.scheduler import cron as _cron  # единственный источник истины
```

CLI использует `sqlite3` stdlib (не aiosqlite), `PRAGMA busy_timeout=5000`. Concurrent write с daemon — WAL сериализует.

### 3.2. Subcommands

| Команда | Аргументы | Вывод | Exit codes |
|---|---|---|---|
| `add` | `--cron EXPR --prompt TEXT [--tz IANA]` | `{"ok": true, "data": {"id", "cron", "prompt", "tz"}}` | 0 / 2 / 3 / **3 при cap** |
| `list` | `[--enabled-only]` | `{"ok": true, "data": [{id, cron, prompt, tz, enabled, created_at, last_fire_at}, …]}` | 0 / 2 |
| `rm` | `ID` | `{"ok": true, "data": {"id", "deleted": true}}` — soft-delete (enabled=0) | 0 / 2 / 7 |
| `enable` | `ID` | `{"ok": true, "data": {"id", "enabled": true}}` | 0 / 7 |
| `disable` | `ID` | `{"ok": true}` | 0 / 7 |
| `history` | `[--schedule-id ID] [--limit 20]` | JSON list of trigger rows | 0 |

**Cap check (GAP #11)** в `add`: перед INSERT запрашиваем `SELECT COUNT(*) FROM schedules WHERE enabled=1`; если ≥ `SCHEDULER_MAX_SCHEDULES` (env, default 64) — exit 3 с `{"ok": false, "error": "scheduler_schedule_cap_reached", "cap": 64}`. +10 LOC.

### 3.3. Валидация

- **cron:** `_cron.parse_cron(expr)` → `CronExpr | raises CronParseError`.
- **prompt:** ≤ 2048 UTF-8 bytes; tabs/newlines ok; control chars (< 0x20 кроме \t\n) rejected.
- **tz:** `zoneinfo.ZoneInfo(name)` — catch `ZoneInfoNotFoundError`.
- **ID:** argparse `type=int`.

### 3.4. Exit codes

```
0 ok
2 usage (argparse)
3 validation / cap-reached
4 IO (DB locked after 3 retries, etc.)
7 not-found (ID)
```

## 4. Skill content — `skills/scheduler/SKILL.md`

```yaml
---
name: scheduler
description: "Расписание cron-задач. Используй когда владелец просит 'напомни каждый день', 'раз в неделю'. CLI `python tools/schedule/main.py`, 5-field POSIX cron."
allowed-tools: [Bash]
---
```

Body (детали в §4-полной версии phase-5):
- Команды с примерами bash-вызовов.
- Cron primer: `m h dom mon dow`, dow=0=воскресенье.
- Примеры (`0 9 * * *`, `0 9 * * 1`, `*/15 * * * *`, `0 9 1 * *`).
- Timezone: `SCHEDULER_TZ` default, `--tz "Europe/Moscow"` для override.
- Границы: prompt — snapshot, не шаблон; DST fall-back documented; one-shot напоминания хранить в memory, не в cron.

## 5. Dispatcher protocol (replaces UDS section)

### 5.1. `ScheduledTrigger` dataclass

```python
# src/assistant/scheduler/dispatcher.py
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class ScheduledTrigger:
    trigger_id: int
    schedule_id: int
    prompt: str
    scheduled_for: datetime   # UTC minute boundary
    attempt: int              # 1-based; attempts+1 на каждый resend
```

### 5.2. Queue semantics

- `asyncio.Queue[ScheduledTrigger]` с `maxsize=64`.
- Producer `SchedulerLoop` использует `await queue.put(t)` (blocking при переполнении → tick стопорится, warn-лог `scheduler_queue_full`).
- Consumer `SchedulerDispatcher` вечный `while not stop_event.is_set(): t = await queue.get()`.
- Shutdown: `Daemon.stop()` → `stop_event.set()` → `queue.put_nowait(_POISON)` → consumer видит poison → exits; producer ловит `stop_event` между ticks.

### 5.3. `SchedulerStore.try_materialize_trigger()` (producer side)

```python
async def try_materialize_trigger(
    self, schedule_id: int, prompt: str, scheduled_for: datetime
) -> int | None:
    """INSERT OR IGNORE и UPDATE last_fire_at атомарно.

    Returns: trigger_id если INSERT состоялся, None если unique violation
    (уже был материализован). last_fire_at UPDATE'ится ТОЛЬКО когда INSERT
    родил новую row.
    """
    async with self._lock:
        cursor = await self._conn.execute(
            "INSERT OR IGNORE INTO triggers(schedule_id, prompt, scheduled_for) "
            "VALUES (?, ?, ?)",
            (schedule_id, prompt, scheduled_for.isoformat() + "Z"),
        )
        if cursor.rowcount == 0:
            await self._conn.commit()
            return None
        trigger_id = cursor.lastrowid
        await self._conn.execute(
            "UPDATE schedules SET last_fire_at=? WHERE id=?",
            (scheduled_for.isoformat() + "Z", schedule_id),
        )
        await self._conn.commit()
        return trigger_id
```

### 5.4. Delivery state machine (consumer)

```
queue.get() → ScheduledTrigger t
  _inflight.add(t.trigger_id)            [set() membership, phase-5 in-memory]
  if t.trigger_id in _recent_acked_lru: skip (dup after restart)
    and still discard from _inflight in finally
  check SELECT enabled FROM schedules WHERE id=t.schedule_id
    if enabled=0: UPDATE status='dropped', return     [GAP #2]
  UPDATE status='sent', sent_at=now WHERE id=t.trigger_id
  msg = IncomingMessage(
    chat_id=OWNER_CHAT_ID,
    text=t.prompt,
    origin="scheduler",
    meta={"trigger_id": t.trigger_id, "schedule_id": t.schedule_id},
  )
  accumulator = []
  async def emit(text): accumulator.append(text)
  try:
    await handler.handle(msg, emit)       # respects per-chat lock
    joined = "".join(accumulator).strip()
    if joined:
      await adapter.send_text(OWNER_CHAT_ID, joined)
    UPDATE status='acked', acked_at=now WHERE id=t.trigger_id
    _recent_acked_lru.add(t.trigger_id)
  except Exception as exc:
    UPDATE status='pending', attempts=attempts+1,
      last_error=?                         [revert for retry]
    if attempts >= dead_attempts_threshold (5):
      UPDATE status='dead'
      _bootstrap_notify_failure(rc=-3, reason=f'trigger_{t.trigger_id}_dead')
  finally:
    _inflight.discard(t.trigger_id)
```

### 5.5. Backpressure & shutdown

- Queue full → producer blocks → next tick skips until drain. Не теряем trigger'ов (они в DB уже `pending`).
- Ctrl+C → `stop_event.set()` → dispatcher дожимает текущий `_deliver` (shielded?) → poison → exit. В `_deliver` мы НЕ shield'им handler.handle — user может отменить долгий turn.

## 6. File tree additions / changes

**Новые файлы:**

| Путь | LOC est | Роль |
|---|---|---|
| `src/assistant/state/migrations/0003_scheduler.sql` | 30 | DDL v3 |
| `src/assistant/scheduler/__init__.py` | 5 | |
| `src/assistant/scheduler/store.py` | 170 | aiosqlite: insert_schedule, list_schedules, list_due_schedules, try_materialize_trigger, mark_sent, mark_acked, mark_dead, mark_dropped, revert_stuck_sent, clean_slate_sent |
| `src/assistant/scheduler/cron.py` | 220 | 5-field парсер + `CronExpr` dataclass + `is_due(expr, last_fire_at, now, tz, catchup)` + DST handling |
| `src/assistant/scheduler/loop.py` | 180 | `SchedulerLoop.run()` — tick 15s, iterate enabled schedules, materialize, put to queue. Outermost try/except → telegram notify (GAP #15) |
| `src/assistant/scheduler/dispatcher.py` | 130 | `SchedulerDispatcher.run()` — consumer; `ScheduledTrigger` dataclass; `_inflight` set; LRU dedup |
| `tools/schedule/main.py` | 380 | argparse router + sys.path shim + cap check + CRUD |
| `skills/scheduler/SKILL.md` | 90 | skill manifest |
| `docs/ops/launchd.plist.example` | 40 | future out-of-process unit |
| `docs/ops/scheduler.service.example` | 30 | future systemd |
| `tests/test_scheduler_cron_parser.py` | 200 | parse kudos, out-of-range, field combos |
| `tests/test_scheduler_cron_semantics.py` | 200 | `is_due` — 30+ cases: minute boundary, multi-miss, DST spring, DST fall, catchup edge |
| `tests/test_scheduler_store.py` | 160 | CRUD + unique + cascade + last_fire_at invariant |
| `tests/test_scheduler_loop.py` | 220 | stub clock, advance, assert triggers materialized |
| `tests/test_scheduler_dispatcher.py` | 180 | queue put/get, inflight-set, revert exclusion, dropped (enabled=0) branch |
| `tests/test_scheduler_recovery.py` | 140 | clean-slate sweep + LRU dedup + flock exclusion |
| `tests/test_schedule_cli.py` | 200 | all subcommands + cap check + JSON shape |
| `tests/test_scheduler_bash_hook_allowlist.py` | 100 | allow/deny |

**Удалено vs wave-0:**
- `src/assistant/scheduler/ipc_client.py` — ВЫРЕЗАНО (B3).
- `src/assistant/scheduler/ipc_server.py` — ВЫРЕЗАНО.
- `tests/test_scheduler_ipc_roundtrip.py` — ВЫРЕЗАНО.
- `tools/schedule/_schedlib/cron.py` — ВЫРЕЗАНО (GAP #18: импортим из `src/assistant/scheduler/cron.py` через `sys.path`).
- `tools/schedule/_schedlib/store_sync.py` — ВЫРЕЗАНО, CRUD inline в `main.py` (~80 LOC, приемлемо).
- `tools/schedule/_schedlib/__init__.py` — ВЫРЕЗАНО.

**Изменения в существующих:**

| Файл | Дельта | Смысл |
|---|---|---|
| `src/assistant/state/db.py` | +20 | `_apply_v3` + `SCHEMA_VERSION=3` |
| `src/assistant/config.py` | +40 | `SchedulerSettings` (см. §7) |
| `src/assistant/main.py` | +100 | pid-file flock (§1.13), clean-slate revert (§8.2), scheduler+dispatcher spawn, catchup-miss recap (§8.3), outermost-exc notify (GAP #15) |
| `src/assistant/bridge/hooks.py` | +50 | scheduler CLI subcmd allowlist |
| `src/assistant/adapters/base.py` | +5 | `IncomingMessage.meta: dict[str, Any] | None = None` (для trigger_id прокидки) |
| `src/assistant/handlers/message.py` | +20 | origin=="scheduler" branch → scheduler_note → merge с url_note |
| `src/assistant/bridge/claude.py` | +5 docstring | описание merge order |
| `src/assistant/bridge/system_prompt.md` | +5 | "Current turn may be scheduler-initiated; check origin hint" |

**LOC total новых:** ~2670; изменения ~245.

## 7. Config — `SchedulerSettings`

```python
class SchedulerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCHEDULER_", ...)

    enabled: bool = True
    tick_interval_s: int = 15
    tz_default: str = "UTC"                  # ZoneInfo name
    catchup_window_s: int = 3600
    dead_attempts_threshold: int = 5
    sent_revert_timeout_s: int = 360          # B2: claude.timeout(300) + 60
    dispatcher_queue_size: int = 64
    max_schedules: int = 64                   # GAP #11
    missed_notify_cooldown_s: int = 86400    # GAP #15/#16 — 24h
```

## 8. Lifecycle management

### 8.1. Startup order (inside `Daemon.start()`)

```python
# 0. PRE-DB: pid-file flock (B1 blocker). ЕСЛИ не взяли — exit 0 сразу.
self._acquire_pid_lock_or_exit()

# 1. Settings → ensure dirs → preflight claude CLI → ensure_skills_symlink.
await _preflight_claude_cli(self._log)
ensure_skills_symlink(self._settings.project_root)
self._settings.db_path.parent.mkdir(parents=True, exist_ok=True)
(self._settings.data_dir / "run").mkdir(parents=True, exist_ok=True)
self._ensure_vault()

# 2. DB connect + migrations.
self._conn = await connect(self._settings.db_path)
await apply_schema(self._conn)

conv = ConversationStore(self._conn)
turns = TurnStore(self._conn, lock=conv.lock)
sched_store = SchedulerStore(self._conn, lock=conv.lock)

# 3. Turn sweep (safe — flock гарантирует single daemon).
swept = await turns.sweep_pending()

# 4. Scheduler clean-slate (GAP #12): ВСЕ status='sent' → pending, без filters.
#    Работает до того как dispatcher стартует — _inflight ещё пустой.
reverted = await sched_store.clean_slate_sent()

# 5. Catchup recap (GAP #16): до запуска loop'а проверить сколько missed.
missed_counts = await sched_store.count_catchup_misses_since_last_boot()
# если suspended long time: посчитать скольки triggerов last_fire_at
# был > catchup_window_s назад и с тех пор были бы ticks. (эвристика)

# 6. Bridge + adapter + handler.
bridge = ClaudeBridge(self._settings)
self._adapter = TelegramAdapter(self._settings)
handler = ClaudeHandler(self._settings, conv, turns, bridge)
self._adapter.set_handler(handler)
await self._adapter.start()

# 7. Dispatcher and loop (paired):
dispatch_queue = asyncio.Queue(maxsize=self._settings.scheduler.dispatcher_queue_size)
dispatcher = SchedulerDispatcher(
    queue=dispatch_queue, store=sched_store, handler=handler,
    adapter=self._adapter, owner_chat_id=self._settings.owner_chat_id,
    settings=self._settings,
)
loop_ = SchedulerLoop(
    queue=dispatch_queue, store=sched_store,
    dispatcher=dispatcher,   # for _inflight read-only view
    settings=self._settings,
)
self._scheduler_dispatcher = dispatcher
self._spawn_bg(dispatcher.run(), name="scheduler_dispatcher")
self._spawn_bg(loop_.run(), name="scheduler_loop")

# 8. Existing fire-and-forget.
self._spawn_bg(self._sweep_run_dirs(), name="sweep_run_dirs")
self._spawn_bg(self._bootstrap_skill_creator_bg(), name="skill_creator_bootstrap")

# 9. Missed recap notify (GAP #16), fire-and-forget.
if missed_counts > 0:
    self._spawn_bg(
        self._adapter.send_text(
            self._settings.owner_chat_id,
            f"пока система спала, пропущено {missed_counts} напоминаний.",
        ),
        name="catchup_recap",
    )
```

### 8.2. Clean-slate sweep (GAP #12)

```python
async def clean_slate_sent(self) -> int:
    """Revert ALL triggers with status='sent' to 'pending' at boot.

    Rationale: flock (§1.13) гарантирует мы единственный процесс. Любая
    'sent' row = orphan от предыдущего crashed daemon'а. Clean-slate policy:
    вернуть в pending без проверки возраста, attempts+=1. Runs ONCE at boot,
    before dispatcher accepts.
    """
    async with self._lock:
        cursor = await self._conn.execute(
            "UPDATE triggers SET status='pending', attempts=attempts+1 "
            "WHERE status='sent'"
        )
        await self._conn.commit()
        return cursor.rowcount or 0
```

Runtime revert (after boot) использует `revert_stuck_sent(timeout_s=sent_revert_timeout_s, exclude_ids=inflight)` — это уже учитывает `_inflight` set и timeout.

### 8.3. Catchup-miss recap (GAP #16)

При boot, после `clean_slate_sent`, проходимся по `schedules WHERE enabled=1`:
для каждого considering last `last_fire_at` + его cron + `catchup_window_s`, если хотя бы один minute-boundary `t` существует такой что `last_fire_at < t` и `now - t > catchup_window_s` — засчитать как miss. Суммируем miss'ы. Если сумма > 0 → отправить владельцу одно сообщение на русском: `"пока система спала, пропущено N напоминаний из [schedule_ids...]"`.

Используем marker-file pattern (`~/.local/share/0xone-assistant/run/.scheduler_recap_marker`) с `ts_epoch` для 24-часового cooldown (не спамим при каждом quick restart).

### 8.4. Outermost exception handler (GAP #15)

```python
# src/assistant/scheduler/loop.py::SchedulerLoop.run
async def run(self) -> None:
    try:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                self._log.warning("scheduler_tick_failed", exc_info=True)
            await asyncio.sleep(self._settings.scheduler.tick_interval_s)
    except Exception as exc:
        self._log.error("scheduler_loop_fatal", error=repr(exc), exc_info=True)
        await self._notify_loop_crash(exc)
        raise
```

`_notify_loop_crash` — reuse `Daemon._bootstrap_notify_failure` pattern с новым marker `.scheduler_loop_notified`, cooldown 24h. Сообщение: `"scheduler loop crashed: <reason>. рестарт требует `ops restart` (или launchd авто-restart в phase 8)."`.

### 8.5. Shutdown

`Daemon.stop()` уже drain'ит `_bg_tasks` с 5s timeout. Новые задачи (`scheduler_loop`, `scheduler_dispatcher`) попадают в ту же drain-очередь. `stop_event.set()` распространится через `SchedulerLoop._stop` / `SchedulerDispatcher._stop` (подписаны через вспомогательный канал). Pid-fd закрывается автоматически в `finally` / на process exit.

## 9. Testing plan

### 9.1. Unit

- `test_scheduler_cron_parser.py`: 30+ кейсов. `*`, lists `1,2,3`, ranges `1-5`, steps `*/15`, combos, field-count errors, out-of-range.
- `test_scheduler_cron_semantics.py` (**new**): 30+ кейсов для `is_due`:
  - first-ever fire (`last_fire_at=None`).
  - exact minute boundary.
  - within same minute (15s tick inside minute) — no double-fire.
  - next-minute tick — fires.
  - gap 1 minute / 1 hour / 1 day.
  - DST spring: `30 2 * * *` on Europe/Moscow 2026-03-29 → skip.
  - DST fall: `30 2 * * *` on Europe/Moscow 2026-10-25 → fire once (fold=0).
  - catchup_window edge: `t = now - 3599` → fire; `t = now - 3601` → drop.
  - last_fire_at update only on INSERT-success.
- `test_scheduler_store.py`: CRUD + unique + cascade + `try_materialize_trigger` idempotency.
- `test_schedule_cli.py`: each subcommand + cap check (add 65-th schedule → exit 3) + JSON shape.

### 9.2. Integration

- `test_scheduler_loop.py`: fake clock, advance 1h → N triggers materialized. Schedule `*/5 * * * *` across 15min → 3 triggers, not 4.
- `test_scheduler_dispatcher.py`:
  - put ScheduledTrigger → consumer delivers → status='acked'.
  - handler raises → status='pending', attempts+=1.
  - attempts=5 → status='dead' + notify called.
  - disabled schedule → status='dropped' before delivery (GAP #2).
  - revert_stuck_sent excludes IDs in `_inflight` set (B2).
  - LRU dedup skips duplicate trigger_id after simulated restart.
- `test_scheduler_recovery.py`:
  - seed trigger status='sent' sent_at=2h ago → boot → clean_slate_sent → status='pending'.
  - concurrent launch: two daemons; first holds flock; second exits 0 with warning. Confirm first daemon's scheduler-turn not swept by second's sweep_pending (since second never entered `_sweep`). (B1 regression guard.)
- `test_scheduler_bash_hook_allowlist.py`: 8 allow/deny кейсов (add ok, add `;rm` deny, prompt > 2048 deny, --tz valid/invalid, rm non-int, unknown subcmd).

### 9.3. E2E

- `test_scheduler_e2e.py`: monkey-patch clock in ClaudeHandler stub; `add --cron "* * * * *" --prompt "ping"` → wait 90s → observed `TelegramAdapter.send_text(owner_chat_id, ...)` call.
- `test_scheduler_origin_branch_e2e.py`: synthesize `IncomingMessage(origin="scheduler", meta={"trigger_id": 42})` → assert `bridge.ask` was called with `system_notes` first element containing `"id=42"`, second element = url_note if URL present (B4).

### 9.4. Failure modes

- Flock held → second daemon exits 0.
- Daemon crash mid-delivery → clean-slate on restart → re-delivered → single ack wins.
- Duplicate INSERT → ignored via unique.
- DST spring `30 2 * * *` → trigger skipped, warn log.
- Queue full → next put blocks → tick-loop pauses → warn log.
- Catchup miss → dropped + structured log.

## 10. Security concerns

1. **Prompt injection.** Prompt — snapshot at add time, не шаблон. Vault edits не влияют на запланированный prompt.
2. **Scheduler CLI available from shell** — feature, single-user бот.
3. **`schedules.tz` IANA.** `zoneinfo` принимает `../etc/passwd` — CLI guard через regex + try/except.
4. **Dead-letter notify.** `_bootstrap_notify_failure` pattern, cooldown 7d.
5. **Per-schedule `allowed-tools`:** не делаем в phase 5.
6. **pid-file flock:** fd остаётся открытым до process exit; не утечка, не secret.

## 11. Open questions for orchestrator Q&A (обновлено под wave-1)

1. **In-process vs отдельный процесс.** Recommended: in-process, queue-based. Phase 8 выносит через замену queue на UDS.
2. **OWNER_CHAT_ID vs dedicated chat.** Recommended: OWNER_CHAT_ID + origin hint.
3. **Shared `assistant.db` vs separate.** Recommended: shared.
4. **Delivery semantics.** Recommended: at-least-once с LRU + clean-slate + in-flight set.
5. **Cron dialect.** Recommended: 5-field only.
6. **Timezone.** Recommended: per-schedule tz с default из env.
7. **Proactively в Telegram.** Recommended: да, `adapter.send_text`.
8. **User active — preempt/queue/parallel.** Recommended: queue через per-chat lock.
9. **rm soft vs hard.** Recommended: soft (enabled=0).
10. **Seed schedules в phase 5 или phase 7.** Recommended: phase 7.
11. **CLI доступна из shell и модели.** Recommended: оба.
12. **`dead_attempts_threshold`.** Recommended: 5 + cooldown 24h telegram notify.
13. **(NEW) Advisory flock vs hard mutex.** Recommended: advisory flock — phase 5 single-user; phase 8 может добавить systemd `Requires=` vs launchd `KeepAlive.LaunchdPlist` семантику.

## 12. Dependencies on other phases

- Phase 2: `IncomingMessage.origin`, per-chat lock, `TurnStore.sweep_pending`.
- Phase 3: `_BASH_PROGRAMS` allowlist, `_spawn_bg` pattern, `_bootstrap_notify_failure` marker-file pattern.
- Phase 4: memory CLI (scheduler-jobs используют optionally); `_memlib` sys.path pattern для CLI imports.
- Phase 6/7: не зависят.
- Phase 8 ops polish: разрезает dispatcher/loop на отдельный процесс через замену queue на UDS.

## 13. Tech debt явно отложенный

1. Out-of-process daemon — phase 8.
2. Retry failed Claude-turn — manual; ack гасит.
3. Dead-letter admin panel — phase 8.
4. Web UI для списка schedules — phase 8.
5. Pause/resume через ENV `SCHEDULER_ENABLED=0` — без API.
6. Backfill после long suspend — дропаем missed, есть catchup recap message.
7. Prometheus metrics — phase 8.
8. History replay UX с total snippet cap (phase-4 debt #7) — делаем в phase 5: `HISTORY_MAX_SNIPPET_TOTAL_BYTES=16384` в `bridge/history.py` (+20 LOC). Прямой follow-up.
9. Obsidian FS watcher (phase-4 debt #3) — phase 8.
10. Out-of-process разрез: phase 5 платит `ScheduledTrigger` dataclass как boundary; phase 8 заменит in-process queue на UDS server с JSON-encoding этого же dataclass'а.

## 14. Risk register

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | Duplicate delivery (crash between sent and acked) | 🔴 | Unique (schedule_id, scheduled_for) + LRU dedup (256) + clean-slate on boot + in-flight set exclusion. |
| 2 | Long user-turn blocks scheduler через per-chat lock | 🟡 | Monitor: warn `pending>5min`. |
| 3 | DST spring skip / fall ambiguity | 🟡 | Spec §2.2; skip non-existent, fire once on fold=0; warn log; documented in SKILL.md. |
| 4 | Suspend/resume → missed triggers | 🟡 | `SCHEDULER_CATCHUP_WINDOW_S=3600`, одиночный recap message на startup. |
| 5 | Prompt injection через memory при add | 🟡 | Snapshot, size cap 2048. |
| 6 | Scheduler-turn зацикливается (модель пишет новые schedules) | 🟡 | `max_turns`=20 cap + recursion warning если >3 новых schedules за час. |
| 7 | Dispatcher crash leaves `_inflight` inconsistent | 🟡 | Startup clean-slate (§8.2); in-flight set = empty on boot; consumer restart clean. |
| 8 | Queue full backpressure stalls tick | 🟢 | 64 slots; warn log; pending triggers не теряются (в DB). |
| 9 | Clock drift daemon vs user date | 🟢 | Single-host, monotonic time. |
| 10 | DB busy_timeout exhausted | 🟢 | WAL + retry 3 раза. |
| 11 | Scheduler-loop crash валит Daemon | 🟡 | Outermost try/except in `run()` + notify (GAP #15); bg-task done_callback убирает ref. Full exit → future launchd restart (phase 8). |
| 12 | Cron parser bug (false due) | 🔴 | 30+ cases parser + 30+ semantics. |
| 13 | Two daemons race на sweep_pending / scheduler rows | 🔴 | **B1:** advisory flock; second daemon exits 0. |
| 14 | Revert-timeout кратче чем claude.timeout → premature double-fire | 🔴 | **B2:** 360s = claude.timeout+60, + in-flight set exclusion. |
| 15 | `origin="scheduler"` code path missing | 🔴 | **B4:** explicit branch in `_run_turn` + merge order spec. |
| 16 | `last_fire_at` semantics ambiguous → missed or double | 🔴 | **B5:** `is_due` contract + UPDATE on insert-success only. |

Удалено из wave-0: #7 UDS chmod race, #12 UDS EADDRINUSE — транспорт заменён на queue.

## 15. Скептические заметки к собственному дизайну

- **In-process scheduler** — sunk cost single-user scope.
- **Stdlib cron parser** — дороже, если мы ошибёмся, но 30+ test-кейсов против crontab.guru fixtures = приемлемо.
- **`assistant.db` shared** — WAL auto-checkpoint default 1000 pages достаточен для 15s tick; под 30-day soak проверить.
- **Clean-slate on boot** — аргумент "may re-fire triggers acked по сети, но DB update не добежал" — согласен, но это out-of-scope: ack = DB commit, нет внешних side-effect'ов до DB-ack (Telegram send идёт ДО ack, значит double-fire = double-Telegram; LRU тут не помогает после restart). Принимаем как at-least-once fundamental limitation.
- **Queue maxsize=64** — если scheduler deliver-path deadlocks, 64 trigger'ов накопятся за 16 минут; warn log должен триггернуть human attention. Не self-healing.
- **Pid-file flock** — на stale NFS может повиснуть на `flock()` call (if filesystem not POSIX-compliant). Mitigation: pre-check что `data_dir` — local FS; error log если `st_dev` отличается от HOME's st_dev. Cheap guard.

## 16. Invariants (новый раздел)

Формальные инварианты, которые должны держаться на протяжении всего runtime'а phase-5:

1. **At-most-one Daemon per `data_dir`.** Удерживается advisory `flock(LOCK_EX|LOCK_NB)` на `<data_dir>/run/daemon.pid`. Нарушение невозможно в пределах POSIX-compliant local FS + наших daemon-процессов (out-of-scope: Windows, NFS без lockd, ручной kill -9 без cleanup — последнее ок: flock освобождается на close fd).
2. **`triggers.(schedule_id, scheduled_for)` UNIQUE.** SQL enforced. Гарантия: `INSERT OR IGNORE` на tick идемпотентен.
3. **`_trigger_ids_in_flight ⊆ {triggers.id | status='sent'}`.** Добавление в set ПЕРЕД `UPDATE status='sent'`; удаление ПОСЛЕ `UPDATE status='acked'/'pending'/'dead'`. Не нарушается в happy path. Crash нарушает (set пропадает, row остаётся 'sent') — восстанавливается clean-slate на boot.
4. **`sent_revert_timeout_s ≥ claude.timeout + 60`.** Config default (360 ≥ 300+60). Env override разрешён, но предупреждение в `config.py` если < claude.timeout.
5. **`last_fire_at == max(triggers.scheduled_for WHERE schedule_id=X AND ever-inserted)`.** Enforced в `try_materialize_trigger`: UPDATE `last_fire_at` происходит атомарно в той же транзакции что и `INSERT` только когда rowcount=1.
6. **Scheduler-loop и dispatcher ВСЕГДА запущены парой.** `Daemon.start()` spawn'ит обе перед `_log.info("daemon_started")`. Если одна крашится → `_notify_loop_crash` + процесс продолжает (вторая bg-task висит одна) → warning caveat: scheduler частично сломан. Phase 8 launchd restart'нёт весь процесс.
7. **`OWNER_CHAT_ID` — единственный chat для scheduler-delivery.** Dispatcher hard-codes `owner_chat_id` из `Settings`; schedule не имеет `chat_id` колонки.
8. **`origin="scheduler"` ⇒ первый элемент `system_notes` — scheduler-note.** Enforced в `ClaudeHandler._run_turn`; URL-note идёт после.

---

### Critical Files for Implementation

- /Users/agent2/Documents/0xone-assistant/src/assistant/main.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/state/db.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/handlers/message.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/config.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/hooks.py
