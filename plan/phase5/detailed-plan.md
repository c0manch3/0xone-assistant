# Phase 5 — детальный план (Scheduler daemon + UDS IPC)

Phase 5 замыкает контур "self-driven" бота: до сих пор turn'ы начинались только с Telegram-сообщения владельца. Теперь бот может сам инициировать turn по cron-расписанию (ежедневное саммари vault, weekly review). Ключевой архитектурный риск — надёжная доставка trigger'а без дубликатов между separate lifecycle-ветками.

## 1. Архитектурные решения с tradeoff'ами

### 1.1. Scheduler как отдельный процесс vs `asyncio.create_task` внутри Daemon'а

**Опции:**
- (A) Отдельный процесс `daemon/main.py`, общается через UDS/DB. Плюсы: изоляция crash'ей, независимый lifecycle, готово к launchd/systemd. Минусы: второй venv, второй лог-output, deployment complexity, shared-state через DB single-writer с `PRAGMA busy_timeout`.
- (B) `asyncio.create_task` через `Daemon._spawn_bg(scheduler_loop(...), name="scheduler_tick")`. Плюсы: один процесс, готовый `_bg_tasks` pattern phase-3, один connection pool, Ctrl+C propagate без отдельных хендлеров. Минусы: crash планировщика = crash бота; asyncio loop overhead; нельзя отдельно apt-update один без другого.

**Рекомендуется: (B)** для phase 5. `Daemon` уже single-user, single-process, crash любой bg-задачи всё равно валит бота (нет supervisor'а). In-process scheduler экономит контур IPC (но UDS остаётся, потому что scheduler-часть = producer, bot-часть = consumer — две coroutine в одном процессе общаются через UDS для **будущей** расщепляемости; сегодня — через `asyncio.Queue`, ниже §1.3). Phase 8 ops-polish сможет вынести в отдельный процесс — UDS boundary уже будет.

### 1.2. Cron-парсер: APScheduler vs `croniter` vs stdlib

**Опции:**
- (A) APScheduler — полноценный scheduler с jobstore, trigger types, executors. Overkill: мы уже решили хранить в своей DB, нам нужен только "is this cron expr due NOW?".
- (B) `croniter` — pure python, ~800 LOC, тёплый API `croniter(expr, base).get_next()`. Доп зависимость.
- (C) stdlib cron parser (наш собственный). Поддержка 5-field POSIX: `m h dom mon dow`, операторы `* , - /`, диапазоны. ~150 LOC. Никакой зависимости.

**Рекомендуется: (C)** в духе stdlib-only phase-3/4. Ограничиваемся 5-field классическим cron (без `@reboot`, `@yearly` синтаксиса). Это покрывает ~95% владельческих use-case'ов ("каждый день в 9", "каждую среду", "первое число месяца"). Human-friendly парсинг ("every monday at 9am") — задача модели, не CLI.

### 1.3. Транспорт trigger'а: UDS vs DB poll vs `asyncio.Queue`

**Опции:**
- (A) UDS `data/run/bot.sock` + line-delimited JSON. Instant delivery, чистый backpressure, готовая абстракция для out-of-process расщепления.
- (B) DB polling: bot каждые N сек читает `triggers WHERE status='pending'`. Простота, но задержка = poll interval, батчинг тривиальный.
- (C) In-process `asyncio.Queue` — прямой вызов `handler.handle(msg, emit)` из scheduler loop'а. Мгновенно, но coupling; невозможно расщепить.

**Рекомендуется: (A) UDS**, даже в in-process режиме phase 5. Мы платим ~40 LOC за `asyncio.start_unix_server` + writer, но получаем готовый boundary для phase 8. DB остаётся authority о статусе (not transport); UDS = signal "иди посмотри DB" + payload для instant delivery. В deployment-sheet документируем: socket path `<data_dir>/run/bot.sock`, permissions `0o600`, owner = daemon user.

### 1.4. Delivery semantics: at-least-once vs at-most-once vs exactly-once

**Опции:**
- (A) **At-least-once** с идемпотентностью по `trigger.id` + unique `(schedule_id, scheduled_for)`. Возможны дубликаты при crash между `sent` и `acked`; defeat через unique index на (schedule_id, scheduled_for).
- (B) At-most-once: fire-and-forget, при crash trigger теряется. Плохо для "ежедневный git commit".
- (C) Exactly-once: distributed transactions. Overkill для single-process.

**Рекомендуется: (A).** Stat-машина: `pending → sent → acked`. Recovery sweep при старте Daemon'а: `UPDATE triggers SET status='pending', attempts=attempts+1 WHERE status='sent' AND (julianday('now') - julianday(sent_at))*86400 > 60`. Таким образом trigger, ушедший в UDS, но не успевший получить ack, перевышлется. Bot deduplicates по `trigger_id` (keeps a small LRU of last 100 acked IDs in memory — не даёт дубликату стать вторым turn'ом).

### 1.5. Crash between "write trigger" and "send via UDS"

Fence-пункты:
1. Scheduler tick → `INSERT OR IGNORE INTO triggers` с `status='pending'` — уникальность защищает от двойной материализации на одном tick'е.
2. UDS write → `UPDATE status='sent', sent_at=now`.
3. Bot reads UDS → synthesizes IncomingMessage → acks → `UPDATE status='acked', acked_at=now`.

Crash после 1, до 2: status='pending', next tick видит row'у, шлёт снова (idempotent благодаря unique). Crash после 2, до 3: status='sent'; recovery sweep >60s = ревёрт в 'pending'. Crash после 3: idempotent ack (WHERE status='sent' OR status='pending'). `attempts` counter даёт dead-letter cut-off (>5 → `status='dead'`, warn в лог).

### 1.6. Scheduler-injected IncomingMessage — какой `chat_id`?

**Опции:**
- (A) `chat_id = OWNER_CHAT_ID` — scheduler-turn живёт в той же conversation, что и обычные диалоги. Плюс: вся история в одном месте, модель видит предыдущий контекст. Минус: trigger inject'ится в середину живого диалога, meaning history-load покажет "сработал cron" как будто пользователь сам так сказал.
- (B) Dedicated `chat_id = -1` (виртуальный scheduler-chat). Plus: чистая изоляция, свой conversation. Minus: отдельная история; модель не помнит "кто владелец", нужен стартовый system-note.
- (C) `chat_id = OWNER_CHAT_ID` + отдельный `turn_id` + `origin="scheduler"` + system-note "предыдущее сообщение — не от пользователя, а от cron".

**Рекомендуется: (C).** Используем OWNER_CHAT_ID (единая история — scheduler-job "посмотри что написал вчера" работает), но в `ClaudeHandler._run_turn` при `msg.origin == "scheduler"` добавляем в `system_notes` одноразовую пометку: "текущий turn инициирован cron-расписанием ID=X, а не пользователем. Отвечай проактивно, не задавай уточняющих вопросов (владелец не активен)." Phase-2 `origin` enum уже есть; phase-5 просто использует ветку.

### 1.7. Долгий scheduler-turn vs одновременный user-turn

Phase-2 `ClaudeHandler._chat_lock` уже сериализует turn'ы на одном `chat_id`. Scheduler-trigger ждёт, пока user-turn закончится. Альтернатива (parallel) потребовала бы разделения ConversationStore — overkill. Недостаток: долгий user-chat "крадёт" slot, scheduler-trigger копится. Мониторинг: если `triggers WHERE status='pending' AND age > 5min` → warn в лог.

### 1.8. Зависимость на memory CLI из scheduler-job'а

Scheduler-turn — это обычный ClaudeHandler-turn. Он видит манифест скилов (включая `memory`). `_effective_allowed_tools` вычисляет union всех `allowed-tools` ∩ baseline. Memory CLI доступен под Bash. Per-turn scheduler-only narrowing — **не делаем** (детальный plan Q8 phase-4 показал SDK не партиционирует hooks per-skill). Риск: scheduler-job может случайно зациклиться в memory write'ах — mitigated MEMORY_MAX_BODY_BYTES cap + max_turns cap на turn.

### 1.9. Clock drift / DST

**Опции:**
- (A) UTC-only в `schedules.tz='UTC'` + cron expr в UTC. Плюс: нет DST, простой код. Минус: "9 утра по Москве" = 6/7 UTC в зависимости от времени года — пользователь должен помнить.
- (B) Per-schedule `tz` с IANA name ("Europe/Moscow"). Стандартный `zoneinfo` stdlib. Плюс: user-friendly. Минус: DST edge-cases (2:30 AM skipped в переходе на летнее время).
- (C) Global `TZ` env + без per-schedule override.

**Рекомендуется: (B).** `zoneinfo` в stdlib с Python 3.9. `schedules.tz` default = `SCHEDULER_TZ` env var (default "UTC"), но можно override через `add --tz "Europe/Moscow"`. DST skip — документируем: если cron matches 2:30 AM и этот час не существует, trigger пропускается (логируется warn). Catch-up policy ("пропустил 30 минут из-за `suspend`") — опция `SCHEDULER_CATCHUP_WINDOW_S=3600`; миссы старше пропускаем.

### 1.10. Cron-синтаксис: 5-field vs extended vs human-readable

**Рекомендуется:** строго 5-field POSIX. Никаких `@daily`, `@reboot`, нет секундной колонки (6-field APScheduler). Модель знает 5-field из своих training data; SKILL.md даёт ~10 примеров.

### 1.11. Storage: shared `assistant.db` vs dedicated `scheduler.db`

**Опции:**
- (A) Shared `<data_dir>/assistant.db` с `conversations/turns/schedules/triggers`. Одна connection, одна транзакция возможна, один `apply_schema` entry point.
- (B) Separate `<data_dir>/scheduler.db`. Плюс: изоляция, удобно backup'ить. Минус: second aiosqlite conn, second migration code path.

**Рекомендуется: (A).** Мы уже имеем `apply_schema` с migration pattern (phase 1 → v1, phase 2 → v2). Новая v3 migration добавляет две таблицы. `memory-index.db` остаётся отдельной, т.к. это derived-data (регенерится из vault). `schedules` — authoritative, logical fit с `conversations`.

### 1.12. Durability: WAL + `fsync`

`assistant.db` уже в `journal_mode=WAL` + `busy_timeout=5000` (см. `src/assistant/state/db.py:30-34`). Достаточно для single-writer pattern. `fsync` не нужен на INSERT triggers — scheduler tick = idempotent (`INSERT OR IGNORE`). Если после `INSERT` процесс умирает до commit'а — на следующем tick'е row вновь материализуется (cron expr же детерминистский).

## 2. DB schema details

Migration file: `src/assistant/state/migrations/0003_scheduler.sql`. Применяется через добавленную `_apply_v3` в `state/db.py`.

```sql
-- 0003_scheduler.sql
-- Phase 5: scheduler daemon + triggers ledger.

CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cron          TEXT NOT NULL,
    prompt        TEXT NOT NULL,
    tz            TEXT NOT NULL DEFAULT 'UTC',
    enabled       INTEGER NOT NULL DEFAULT 1,  -- 0 = soft-delete
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_fire_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON schedules(enabled);

CREATE TABLE IF NOT EXISTS triggers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id   INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    prompt        TEXT NOT NULL,              -- snapshot at materialization
    scheduled_for TEXT NOT NULL,              -- ISO-8601 UTC
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|sent|acked|dead
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

Scheduler-init в `Daemon.start()` — после `apply_schema` уже даёт v3; `SchedulerStore` получает `aiosqlite.Connection` shared с `ConversationStore`.

## 3. CLI contract — `tools/schedule/main.py`

Stdlib-only, `python tools/schedule/main.py <sub> …`. `_schedlib/` subpackage (избегаем collision по phase-4 tech debt #4 — используем relative-import pattern `from _schedlib import …` после `sys.path.insert` на `_HERE`).

### 3.1. Subcommands

| Команда | Аргументы | Вывод | Exit codes |
|---|---|---|---|
| `add` | `--cron EXPR --prompt TEXT [--tz IANA]` | `{"ok": true, "data": {"id": 42, "cron": "…", "prompt": "…", "tz": "…"}}` | 0 ok / 2 usage / 3 validation (malformed cron, prompt > 2 KB) |
| `list` | `[--enabled-only]` | `{"ok": true, "data": [{"id", "cron", "prompt", "tz", "enabled", "created_at", "last_fire_at"}, …]}` | 0 / 2 |
| `rm` | `ID` | `{"ok": true, "data": {"id": 42, "deleted": true}}` — soft-delete (enabled=0) | 0 / 2 / 7 (not found) |
| `enable` | `ID` | `{"ok": true, "data": {"id": 42, "enabled": true}}` | 0 / 7 |
| `disable` | `ID` | `{"ok": true}` | 0 / 7 |
| `history` | `[--schedule-id ID] [--limit 20]` | JSON list of triggers (id, scheduled_for, status, attempts, last_error, sent_at, acked_at) | 0 |

### 3.2. Валидация

- **cron:** regex на 5 whitespace-separated fields, каждый — один из `{*, N, N-M, N,N,N, */N, N-M/K}`. `_schedlib/cron.py::parse_cron(expr) -> CronExpr | raise`.
- **prompt:** ≤ 2048 bytes UTF-8; tабs/newlines разрешены; control chars rejected.
- **tz:** `zoneinfo.ZoneInfo(name)` — catch `ZoneInfoNotFoundError`.
- **ID:** argparse `type=int`.

### 3.3. Exit codes

```
0 ok
2 usage (argparse)
3 validation
4 IO (DB locked, etc.)
7 not-found
```

### 3.4. Connection pattern

CLI использует `sqlite3` stdlib (а не aiosqlite — он для main process'а). `PRAGMA busy_timeout=5000`. Параллельная запись scheduler-tick из daemon'а vs CLI из shell'а — SQLite serialized по WAL.

## 4. Skill content — `skills/scheduler/SKILL.md`

```yaml
---
name: scheduler
description: "Расписание cron-задач для 0xone-assistant. Используй когда владелец просит 'напомни каждый день', 'каждую среду', 'раз в месяц'. CLI `python tools/schedule/main.py`, принимает стандартный 5-field cron."
allowed-tools: [Bash]
---
```

Body:
- **Команды** (с конкретными примерами bash-вызовов).
- **Cron primer:** 5 полей `m h dom mon dow`, где `dow=0` — воскресенье. Примеры:
  - `0 9 * * *` — каждый день в 9:00.
  - `0 9 * * 1` — каждый понедельник в 9:00.
  - `*/15 * * * *` — каждые 15 минут.
  - `0 9 1 * *` — первое число каждого месяца в 9:00.
- **Timezone:** по-умолчанию `SCHEDULER_TZ` env (обычно UTC), явно — `--tz "Europe/Moscow"`.
- **Границы:**
  - Прежде чем `add`, подтверди prompt у владельца (scheduler-turn=autonomous, не сможет спросить позже).
  - Не планируй "проверь почту" — нет media-скилов в phase 5.
  - Не используй cron для одноразовых напоминаний — `schedules` = только repeat. One-shot → храни в memory `inbox/reminder-<date>.md`.

Примеры диалогов:
- User: "напоминай утром делать отчёт по inbox" →
  Model: `python tools/schedule/main.py add --cron "0 9 * * *" --prompt "сделай саммари ~/.local/share/0xone-assistant/vault/inbox за последние 24 часа и пришли список"`.

## 5. IPC протокол

### 5.1. Socket

`<data_dir>/run/bot.sock`, SOCK_STREAM, permissions 0o600. Owner — daemon user. `asyncio.start_unix_server(callback, path=sock_path)` — один accept'ит, обрабатывает line-by-line.

### 5.2. Message schema

```json
{
  "trigger_id": 127,
  "schedule_id": 42,
  "prompt": "сделай саммари inbox",
  "scheduled_for": "2026-04-17T09:00:00Z",
  "attempt": 1
}
```

### 5.3. Ack schema

```json
{"ok": true, "trigger_id": 127}
```

или

```json
{"ok": false, "trigger_id": 127, "error": "handler raised ClaudeBridgeError"}
```

### 5.4. Sequence

1. Scheduler client: `INSERT OR IGNORE triggers(…, status='pending')` → если `changes()=0`, skip (идемпотентность на reuse tick'а).
2. Client: `UPDATE status='sent', sent_at=now WHERE id=?` → connect UDS → write line.
3. Server (в Daemon'е): read line → parse JSON → check dedup cache `_recent_acked_trigger_ids` (LRU 256) → synthesize `IncomingMessage(chat_id=owner_chat_id, text=prompt, origin="scheduler")` → `handler.handle(msg, emit)` где `emit` аккумулирует текст → `adapter.send_text(owner_chat_id, joined_text)` → write ack JSON → close.
4. Client: on ack.ok → `UPDATE status='acked', acked_at=now`. On ack.fail → `UPDATE status='pending', last_error=?, attempts=attempts+1`. On socket error → leave `sent`, recovery revert'нёт.

### 5.5. Backpressure

Client: connect timeout 5s, write timeout 10s. Если UDS недоступен (файла нет, ECONNREFUSED) → leave `sent`, next tick попробует заново. `attempts > 5` → `status='dead'`, одноразовый owner-notify.

## 6. File tree additions / changes

**Новые файлы:**

| Путь | LOC estimate | Роль |
|---|---|---|
| `src/assistant/state/migrations/0003_scheduler.sql` | 30 | DDL v3 |
| `src/assistant/scheduler/__init__.py` | 5 | |
| `src/assistant/scheduler/store.py` | 150 | aiosqlite обёртка над schedules/triggers (insert_trigger, mark_sent, mark_acked, revert_stuck_sent, list_due) |
| `src/assistant/scheduler/cron.py` | 180 | 5-field парсер + `next_fire_after(dt)` + matcher `is_due(expr, dt)` |
| `src/assistant/scheduler/loop.py` | 200 | `SchedulerLoop.run()` — tick каждые 15s, материализация due-triggers, UDS send |
| `src/assistant/scheduler/ipc_client.py` | 90 | UDS writer + ack reader |
| `src/assistant/scheduler/ipc_server.py` | 120 | `asyncio.start_unix_server` + handler bridge + dedup LRU |
| `tools/schedule/main.py` | 350 | argparse router |
| `tools/schedule/_schedlib/__init__.py` | 0 | |
| `tools/schedule/_schedlib/cron.py` | 150 | (duplicate-or-import of scheduler/cron; CLI is stdlib so duplicate) |
| `tools/schedule/_schedlib/store_sync.py` | 120 | sync sqlite3 CRUD for CLI |
| `skills/scheduler/SKILL.md` | 90 | skill manifest |
| `docs/ops/launchd.plist.example` | 40 | future out-of-process unit |
| `docs/ops/scheduler.service.example` | 30 | systemd unit example |
| `tests/test_scheduler_cron_parser.py` | 200 | cron-expr parsing/matching |
| `tests/test_scheduler_store.py` | 150 | sqlite CRUD + unique constraint |
| `tests/test_scheduler_loop.py` | 220 | tick + due materialization |
| `tests/test_scheduler_ipc_roundtrip.py` | 180 | UDS client/server e2e |
| `tests/test_scheduler_recovery.py` | 120 | crash-between-sent-acked revert |
| `tests/test_schedule_cli.py` | 180 | argparse + JSON output |
| `tests/test_scheduler_bash_hook_allowlist.py` | 100 | Bash hook accepts/rejects |

**Изменения в существующих:**

| Файл | Дельта | Смысл |
|---|---|---|
| `src/assistant/state/db.py` | +20 LOC | `_apply_v3` + bump `SCHEMA_VERSION = 3` |
| `src/assistant/config.py` | +30 LOC | `SchedulerSettings` (tick_interval_s=15, socket_path, tz default, catchup_window_s, dead_attempts_threshold=5, sent_revert_timeout_s=60, ack_timeout_s=10) |
| `src/assistant/main.py` | +70 LOC | `_start_scheduler()`, `_start_ipc_server()` spawn; recovery sweep at startup (revert `status='sent' WHERE stale`); ensure `<data_dir>/run/` dir present (уже есть) |
| `src/assistant/bridge/hooks.py` | +45 LOC | `_SCHEDULE_ALLOWED_SUBCMDS`, `_validate_schedule_invocation` в match-branch в `_validate_python_invocation` |
| `src/assistant/handlers/message.py` | +15 LOC | origin=="scheduler" → inject system-note "autonomous turn" |
| `src/assistant/bridge/system_prompt.md` | +5 LOC | "Current turn may be scheduler-initiated; check origin hint" |
| `src/assistant/adapters/telegram.py` | 0 LOC | Ничего — TelegramAdapter.send_text уже public (используется в bootstrap notify) |

**LOC total новых:** ~2100; изменения ~185. 21 новый файл.

## 7. Bash allowlist entries

В `bridge/hooks.py` добавляем в `_validate_python_invocation` после phase-4 логики: если script path = `tools/schedule/main.py`, валидируем subcommand + args.

```python
_SCHEDULE_ALLOWED_SUBCMDS = frozenset({"add", "list", "rm", "enable", "disable", "history"})
_CRON_EXPR_RE = re.compile(r"^[\d\*\-,/\s]{1,100}$")  # rough shape gate, full parse in CLI
_SCHEDULE_PROMPT_MAX = 2048
```

Правила:
- `argv[1] == "tools/schedule/main.py"` → проверяем `argv[2]` in `_SCHEDULE_ALLOWED_SUBCMDS`.
- Для `add` требуем `--cron EXPR --prompt TEXT`; EXPR matches `_CRON_EXPR_RE` (shell-метачары уже блокнуты на уровне `_SHELL_METACHARS`, но prompt может содержать кавычки `"`); prompt size ≤ 2048.
- `--tz IANA_NAME` — regex `^[A-Za-z_/+\-0-9]{2,64}$`.
- Для `rm|enable|disable|history` — `ID` — argparse catches non-int.

**Почему это важно:** модель генерирует prompt string, которая потом раскрутится в scheduler-turn'е как "user message". Злонамеренная memory-заметка может содержать wikilink, который модель случайно подставит в prompt — промпт-injection, пусть и self-sourced. Size cap 2048 bytes ограничивает объём поражения. Семантика "prompt — это user-text для будущего turn'а" — SKILL.md явно говорит модели не передавать туда credentials.

## 8. Lifecycle management

### 8.1. Startup (in `Daemon.start()`)

После `apply_schema` и перед `_spawn_bg(sweep)`:

```python
# 1. Recovery sweep: revert stale 'sent' triggers.
sched_store = SchedulerStore(self._conn)
reverted = await sched_store.revert_stuck_sent(timeout_s=60)
if reverted:
    self._log.warning("scheduler_reverted_stuck_triggers", count=reverted)

# 2. UDS server (bot side of IPC).
self._ipc_server = await start_ipc_server(
    socket_path=settings.scheduler.socket_path,
    handler=handler,
    adapter=self._adapter,
    log=self._log,
)
self._spawn_bg(self._ipc_server.serve_forever(), name="scheduler_ipc")

# 3. Scheduler loop (producer).
loop_ = SchedulerLoop(store=sched_store, ipc_client_factory=..., settings=settings)
self._spawn_bg(loop_.run(), name="scheduler_tick")
```

### 8.2. Shutdown

`Daemon.stop()` уже drain'ит `_bg_tasks` с 5s timeout. Новые задачи (scheduler_tick, scheduler_ipc) попадают в ту же drain-очередь. UDS-server слушается на path — закрываем сокет (`server.close(); await server.wait_closed()`), файл на диске удаляем в finally. SIGINT propagate через `stop_event.set()` → `stop()` → cancel.

### 8.3. Out-of-process mode (future phase 8)

Заготовки unit'ов:
- `docs/ops/launchd.plist.example` — `<key>RunAtLoad</key><true/>`, WorkingDirectory=project_root, ProgramArguments=`["python", "-m", "assistant.scheduler.daemon_main"]`.
- `docs/ops/scheduler.service.example` — systemd с `Type=simple`, `Restart=on-failure`, `User=assistant`.

В phase 5 unit'ы — только примеры, не устанавливаются.

## 9. Testing plan

### 9.1. Unit

- `test_scheduler_cron_parser.py`: 30+ кейсов. `*`, lists (`1,2,3`), ranges (`1-5`), steps (`*/15`, `1-10/2`), wildcards + specifics, DST skip (2:30 AM), month/dow interaction (POSIX OR semantics), invalid (too many fields, non-numeric, out-of-range).
- `test_scheduler_store.py`: insert, unique constraint violation, list_due filter, revert_stuck_sent, cascade delete.
- `test_schedule_cli.py`: every subcommand happy-path + error, JSON shape stable, exit codes correct.

### 9.2. Integration

- `test_scheduler_loop.py`: stub clock, advance 1 hour, assert N triggers materialized. Schedule `*/5 * * * *` + 15m → 3 triggers.
- `test_scheduler_ipc_roundtrip.py`: spawn UDS server with stub handler, write message, assert ack written and trigger marked acked in DB.
- `test_scheduler_recovery.py`: seed trigger с status='sent' sent_at=2h ago → daemon start → revert sweep → trigger вернулся в pending.
- `test_scheduler_bash_hook_allowlist.py`: 8 кейсов allow/deny (add с нормальным cron, add с `;rm`, `add --prompt` 5000 bytes reject, rm 42, rm non-int, unknown subcommand, --tz IANA, --tz `../etc`).

### 9.3. E2E

- `test_scheduler_e2e.py`: monkey-patch `datetime.now` в ClaudeHandler stub; `add --cron "* * * * *" --prompt "ping"` → wait 90s → observed `TelegramAdapter.send_text` call с ownerchatid.

### 9.4. Failure modes

- Daemon crash mid-delivery: test reverts sent → re-delivers → single ack wins.
- Bot crash before ack: similar.
- Duplicate trigger: `INSERT OR IGNORE` тестируется на том же `(schedule_id, scheduled_for)`.
- DST transition: cron `30 2 * * *` в день перехода (`Europe/Moscow` 2026-03-29) — 2:30 AM не существует → trigger skipped, warn logged.
- Malformed cron via CLI → exit 3; via DB (manual INSERT обходом CLI) → loop catches, logs `cron_parse_failed`, marks schedule `enabled=0`.
- UDS unreachable: socket file removed mid-run → IPC client EAGAIN/ECONNREFUSED → backoff, leave status='sent'.

## 10. Security concerns

1. **Prompt injection через scheduler.** Модель создаёт schedule → prompt уходит в DB → через N дней scheduler-loop читает и синтезирует user-message. За эти N дней memory vault мог быть поправлен (юзер вручную в Obsidian вписал `[[rm -rf ~]]`) — но prompt это отдельная колонка в DB, читается as-is, не merged с vault. Mitigation: scheduler-prompt = snapshot at `add` time, не шаблон. **Если пользователь позволит модели "use memory to craft prompt"** — это уязвимость, но это внутри turn'а, не через scheduler. Не чиним в phase 5.
2. **DB таблица schedules доступна не только модели.** Владелец может редактировать `assistant.db` напрямую из shell. Принимается как legitimate — single-user бот.
3. **CLI доступен из shell.** `python tools/schedule/main.py add --cron "…" --prompt "…"` — пользователь может добавить расписание напрямую. Это feature (pairing с ручным vault editing).
4. **`schedules.tz` IANA strings.** `zoneinfo` принимает `../etc/passwd` как имя — `ZoneInfoNotFoundError`. CLI sanitize через regex + try/except.
5. **UDS permissions.** `os.chmod(sock_path, 0o600)` сразу после `bind` (atomic enough for single-user). Owner = bot user.
6. **Dead-letter queue.** После 5 неудачных attempts → `status='dead'`, одноразовое Telegram уведомление владельцу: "scheduler trigger N не доставлен, см. `history --schedule-id X`". Re-use `_bootstrap_notify_failure` pattern с marker-file cooldown.
7. **Per-schedule `allowed-tools`.** НЕ делаем — schedule — это просто prompt-text; в turn'е работает общий union ∩ baseline. Если владелец хочет, чтобы scheduler-job мог только memory, а не Bash — deferred to phase 8.

## 11. Open questions for orchestrator Q&A

1. **Q: In-process vs отдельный процесс?**
   - **Recommended:** in-process через `_spawn_bg`. UDS boundary всё равно делаем для phase 8.
   - Alt: уже сейчас разрезать на `daemon/main.py`. Rationale against: нет supervisor'а, crash любого процесса одинаково валит функционал, complexity сегодня — не оправдана.

2. **Q: OWNER_CHAT_ID или dedicated scheduler-chat?**
   - **Recommended:** OWNER_CHAT_ID + origin hint в system-note.
   - Alt A: dedicated chat_id=-1. Плюс: изоляция истории. Минус: бот не помнит контекст владельца в scheduler-turn'ах.
   - Alt B: dedicated chat_id= OWNER+1000000000 (synthetic but owner-like). Тот же минус.

3. **Q: Shared `assistant.db` или отдельный `scheduler.db`?**
   - **Recommended:** shared `assistant.db` (phase 1 migration pattern уже готов).
   - Alt: отдельный. Плюс: backup granularity. Минус: ещё один aiosqlite conn, ещё один migration runner.

4. **Q: Delivery semantics?**
   - **Recommended:** at-least-once с unique `(schedule_id, scheduled_for)` + LRU dedup.
   - Alt: at-most-once. Отказ от retry → миссы при crash'е недопустимы.

5. **Q: Cron — 5-field POSIX или human-readable?**
   - **Recommended:** 5-field only. Модель уже знает синтаксис.
   - Alt: поддержать `@daily`, `@weekly` alias'ы. +15 LOC в парсере, neutral tradeoff — не возражаю добавить.

6. **Q: Timezone — global `TZ` env или per-schedule?**
   - **Recommended:** per-schedule `tz` column + default из `SCHEDULER_TZ` env.
   - Alt: global only. Минус: владелец может захотеть один schedule в UTC (cron log rotation), другой в Moscow time (утреннее напоминание).

7. **Q: Ответ scheduler-turn'а — proactively в Telegram?**
   - **Recommended:** да, через `TelegramAdapter.send_text(OWNER_CHAT_ID, joined_text)`. Silent ack = scheduler бесполезен.
   - Alt A: только сохранить в conversations, не слать в Telegram. Владелец увидит при следующем сообщении. Минус: "напоминалки" не работают.
   - Alt B: отдельный chat_id в телеге (admin-chat). Overkill для single-user.

8. **Q: User активен — preempt, queue или parallel?**
   - **Recommended:** queue через phase-2 per-chat lock. Scheduler-turn ждёт user-turn, потом отрабатывает.
   - Alt: preempt (interrupt user-turn). Плохо.
   - Alt: parallel (отдельный conversation state). Overkill.

9. **Q: Delete policy `rm ID` — hard или soft?**
   - **Recommended:** soft-delete (`enabled=0`). Исторические triggers сохраняются, `history` работает.
   - Alt: hard-delete с `ON DELETE CASCADE` на triggers. Владелец теряет audit log.

10. **Q: Default seed schedules в phase 5 или phase 7?**
    - **Recommended:** **не сеять в phase 5**. Ежедневный vault-commit требует `gh` CLI (phase 7). Phase 5 поставляет механизм, phase 7 добавит seed.
    - Alt: seed simple ping-schedule в phase 5 для demo. Минус: лишний noise у владельца.

11. **Q: Scheduler-CLI доступен модели + из shell или только модели?**
    - **Recommended:** оба. Shell-доступ удобен для debug и ручного backup'а schedules. CLI не требует daemon running.
    - Alt: только через модель (e.g., pipe-check). Overkill.

12. **Q: Stat-threshold `dead_attempts_threshold`?**
    - **Recommended:** 5, с cooldown Telegram-notify аналогично phase-3 bootstrap marker.
    - Alt: infinite retry. Плохо — если скилл permanent broken, spam'ит вечно.

## 12. Dependencies on other phases

- **Phase 2:** `IncomingMessage.origin` enum (уже имеет `"scheduler"`), per-chat `asyncio.Lock`, `TurnStore.sweep_pending` (scheduler-turn пишет туда же).
- **Phase 3:** `_BASH_PROGRAMS` allowlist extended; `_spawn_bg` pattern; PostToolUse sentinel (scheduler-jobs могут писать скилы — hot-reload работает).
- **Phase 4:** memory CLI; `MemorySettings`; `_ensure_vault`. Scheduler-jobs "сделай саммари inbox" используют memory. **Не блокер**: scheduler работает и без memory (prompt "ping" → "pong").
- **Phase 6 (не сделана):** media. Scheduler-jobs не будут использовать ВХОДные media до phase 6 — prompt всегда текст.
- **Phase 7 (не сделана):** `tools/gh`. Phase 5 поставляет scheduler-механизм; phase 7 добавит default seed "ежедневный git commit vault" через `tools/gh`.

Не зависит от phase 8 (ops polish).

## 13. Tech debt явно отложенный

1. **Out-of-process daemon** — phase 8 ops. Unit'ы уже заготовлены.
2. **Retry failed Claude-turn** — ack гасит trigger независимо от success/error. Если turn упал — вошёл в `conversations` как `interrupted`. Повторить — manual.
3. **Dead-letter UI** — admin panel phase 8. Сегодня — только Telegram notify + `history` CLI.
4. **Web UI для списка schedules** — phase 8.
5. **Pause/resume whole scheduler** — feature toggle env `SCHEDULER_ENABLED=0`, без API.
6. **Backfill после long suspend** — `SCHEDULER_CATCHUP_WINDOW_S` простая фильтрация. Умный backfill (запустить самые свежие N миссов) — deferred.
7. **Prometheus metrics** — phase 8.
8. **History replay UX** — phase 4 `HISTORY_MAX_SNIPPET_TOTAL_CHARS` cap (tech debt #7 из phase 4 summary). Scheduler-turn'ы прибавят трафика tool_result snippet'ов → нужно total cap. **Делаем в phase 5 в `bridge/history.py` +20 LOC** (default 16 KB) — прямой follow-up phase-4 debt.
9. **`_memlib` → relative-imports refactor** (phase 4 debt #4). В phase 5 создаём `tools/schedule/_schedlib/` — третий `_lib`-like подкаталог. **Делаем в phase 5 `tools/__init__.py` + переход на `from tools.schedule._schedlib import …`** — 30 LOC.
10. **Obsidian FS watcher** (phase 4 debt #3) — deferred phase 8.

## 14. Risk register

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | Duplicate delivery (crash между sent и acked) | 🔴 | Unique `(schedule_id, scheduled_for)` + in-memory LRU dedup + revert-stale sweep. |
| 2 | Long user-turn blocks scheduler через per-chat lock | 🟡 | Monitor: warn если `pending` старше 5 минут. Accept as design (single-user бот). |
| 3 | DST skip / gap | 🟡 | `zoneinfo`-based matching; skip non-existent hours, log warn. Документировано в SKILL.md. |
| 4 | Suspend/resume длинный — миссы | 🟡 | `SCHEDULER_CATCHUP_WINDOW_S=3600` фильтр; старше пропускаем. |
| 5 | Prompt injection через memory vault в prompt-text | 🟡 | Prompt — snapshot at add time; модель не "crafts" prompt из vault в scheduler-turn'е. Size cap 2048 bytes. |
| 6 | Scheduler-turn зацикливается (модель пишет новые schedules в scheduler-turn'е) | 🟡 | `max_turns` cap (phase 2, default 20). Recursion detection: warn если `triggers WHERE schedule_id=X AND scheduled_for > now() - 1h` > 3. |
| 7 | UDS socket permissions loose (race между bind и chmod) | 🟡 | `os.umask(0o077)` перед bind или `asyncio.start_unix_server(..., start_serving=False)` + chmod + `start_serving()`. |
| 8 | Clock drift между daemon и user's `date` | 🟢 | Single-host, `time.time()` monotonic — не актуально для single-process. |
| 9 | DB `busy_timeout` exhausted (CLI + scheduler write одновременно) | 🟢 | WAL mode + 5s timeout достаточно; если SQLITE_BUSY — retry в loop 3 раза. |
| 10 | Scheduler-loop crash валит Daemon | 🟢 | `_bg_tasks` pattern — done_callback убирает из set, но не restart'ит. Принимаем: crash = full exit, systemd/launchd (phase 8) restart'нёт. В phase 5 — владелец увидит через ping "нет ответа". |
| 11 | Cron parser bug (false positive/negative due time) | 🔴 | 30+ unit-test кейсов, cross-check по известным источникам (crontab.guru fixtures). |
| 12 | UDS file сохранился после crash → повторный start `bind` fails EADDRINUSE | 🟡 | `Path(sock).unlink(missing_ok=True)` перед `start_unix_server`. Обязательная защита от двух одновременных daemon'ов — advisory `flock` на `<data_dir>/run/daemon.pid`. |

## 15. Скептические заметки к собственному дизайну

- **In-process scheduler** — удобно, но если когда-нибудь понадобится запускать один scheduler на нескольких ботов (multi-user rewrite) — придётся разрезать. Sunk cost принимается: single-user scope определён в `plan/README.md`.
- **Stdlib cron parser** — соблазн импортировать `croniter` (800 LOC тестированного кода). Контраргумент: phase-3/4 установили stdlib-only как дисциплину, `croniter` зависел бы только phase 5 → inconsistent. 150 LOC cron-парсера + 200 LOC тестов — дешевле debug'а чужой зависимости.
- **`assistant.db` shared** — риск: scheduler-loop делает `INSERT triggers` каждые 15s, WAL-файл растёт. `PRAGMA wal_autocheckpoint` = 1000 pages (default) должен справляться, но проверить под 30-дневным soak.
- **OWNER_CHAT_ID в scheduler-turn'е** — если владелец в отпуске и бот получает "вчерашнее напоминание" в длинном треде — модель может увидеть это как "пользователь попросил сделать саммари" и ответить бессмысленно. Mitigation: system-note "autonomous turn, origin=scheduler" делает контекст явным.
- **Per-turn `allowed_tools`** — не меняем (phase-4 S-A.3 ограничение), но документируем в SKILL.md: scheduler-job видит все скилы, включая skill-installer. Если `@daily "install random skill from marketplace"` — это legitimate feature для skill-creator автоматизации, но open attack surface. Phase-5 accept risk; phase-7+ возможна доп. изоляция.

---

### Критические файлы для реализации

- `/Users/agent2/Documents/0xone-assistant/src/assistant/main.py`
- `/Users/agent2/Documents/0xone-assistant/src/assistant/state/db.py`
- `/Users/agent2/Documents/0xone-assistant/src/assistant/bridge/hooks.py`
- `/Users/agent2/Documents/0xone-assistant/src/assistant/handlers/message.py`
- `/Users/agent2/Documents/0xone-assistant/src/assistant/config.py`
