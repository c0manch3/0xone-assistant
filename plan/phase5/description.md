# Phase 5 — Scheduler daemon + UDS IPC

**Цель:** cron-задачи хранятся в SQLite, исполняются отдельным демоном, доставляются в бота через Unix domain socket `data/run/bot.sock`. Новая сессия видит успешно сработавший trigger в `conversations` как обычный turn с `origin="scheduler"`.

**Вход:** phase 4 (memory CLI + vault + MemorySettings + `IncomingMessage.origin` enum + per-chat `asyncio.Lock` в `ClaudeHandler` + `_bg_tasks` pattern в `Daemon` + write-first stage-dir pattern).

**Выход:** пользователь говорит "запланируй ежедневное саммари в 9 утра" → модель вызывает `python tools/schedule/main.py add --cron "0 9 * * *" --prompt "сделай саммари inbox"` → в 09:00 локального времени демон материализует `triggers` row → отправляет JSON-line в UDS → бот проксирует как `IncomingMessage(origin="scheduler", chat_id=<owner>, text=<prompt>)` → `ClaudeHandler` крутит turn → ответ проактивно летит владельцу через `TelegramAdapter.send_text(owner_chat_id)`.

## Задачи

1. **DB schema v3 (migration `0003_scheduler.sql`)** в shared `data/assistant.db`:
   - `schedules(id INTEGER PK, cron TEXT NOT NULL, prompt TEXT NOT NULL, tz TEXT NOT NULL DEFAULT 'UTC', enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT, last_fire_at TEXT)`.
   - `triggers(id INTEGER PK, schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE, prompt TEXT NOT NULL, scheduled_for TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT, sent_at TEXT, acked_at TEXT)` + `UNIQUE(schedule_id, scheduled_for)` + `INDEX(status, scheduled_for)`.
2. **`tools/schedule/main.py`** — stdlib-only CLI (паттерн phase-3/4). Subcommands: `add`, `list`, `rm`, `enable`, `disable`, `history`. JSON-line output.
3. **`skills/scheduler/SKILL.md`** — `allowed-tools: [Bash]`, cron-primer (5-field POSIX), примеры диалогов.
4. **`src/scheduler/` package** с компонентами: `daemon.py` (tick-loop), `cron.py` (stdlib cron parser), `store.py` (aiosqlite обёртка над schedules/triggers), `ipc_client.py` (UDS writer), `ipc_server.py` (UDS reader на стороне Daemon'а).
5. **Интеграция в `src/assistant/main.py::Daemon`** — scheduler стартует как `_spawn_bg(SchedulerLoop(...).run(), name="scheduler_tick")` в том же процессе; UDS-сервер — ещё одна bg-задача.
6. **IPC-протокол** — line-delimited JSON: `{"trigger_id", "schedule_id", "prompt", "scheduled_for", "attempt"}` → `{"ok": true, "trigger_id": …}` ack. At-least-once: `pending → sent → acked` с recovery на рестарте (все `sent without acked` ревёрнутся в `pending`).
7. **Bash allowlist** в `bridge/hooks.py` — разрешить `python tools/schedule/main.py <sub> …` с argv-валидацией cron-выражения, prompt size cap, enum-валидация подкоманд.
8. **Пример launchd/systemd unit** (для будущего out-of-process режима) кладём в `docs/ops/` — в phase 5 используется in-process, но unit заготовлен для phase 8.
9. **Scheduler-injected turn** — `ClaudeHandler` уже знает `origin`; добавляем в `system_prompt` одноразовую system-note "сработал cron, пользователь не активен, отвечай проактивно в owner chat" (но сам chat_id = `OWNER_CHAT_ID`, tгрaf = `TelegramAdapter.send_text`).

## Критерии готовности

- `add --cron "*/5 * * * *" --prompt "ping"` → через ≤300 сек в Telegram приходит ответ Claude'а.
- Рестарт Daemon'а посреди срабатывания (между `sent` и `acked`): trigger revert'ится в `pending` на startup, повторно доставляется, финальный ack гасит дубликат по `(schedule_id, scheduled_for)` unique-index.
- `rm ID` → soft-delete (`enabled=0`); исторические `triggers` остаются. `history --schedule-id ID` показывает их.
- Malformed cron → `exit 3` с JSON `{"ok": false, "error": "cron parse: …"}`.
- Bash hook rejects `python tools/schedule/main.py add --cron "0 9 * * *" --prompt "$(cat /etc/passwd)"` по `$(` metachar.
- Параллельный user-turn + scheduler-trigger на том же OWNER_CHAT_ID сериализуются per-chat lock'ом (phase 2); второй ждёт.
- Daily `memory search` из scheduler-job'а работает (per-turn allowed_tools = union memory + scheduler skills ∩ baseline).

## Явно НЕ в phase 5

- APScheduler / croniter как зависимости — stdlib-only (свой минимальный парсер 5-field cron с Sunday=0 и специальными символами `* , - /`).
- Out-of-process daemon — phase 8 ops polish (launchd/systemd).
- Default seed schedules (ежедневный vault git-commit) — phase 7 (`tools/gh`).
- Retry failed Claude-turn — ack гасит trigger независимо от ok/error turn'а; retry — manual через `add` заново.
- Admin panel / web UI для списка расписаний — phase 8.
- Human-friendly cron ("every day at 9am") — модель генерит 5-field строку сама, согласно SKILL.md примерам.
- Per-schedule allowed-tools narrowing — scheduler-turn получает тот же allowed_tools union что и user-turn.
- Observability / Prometheus metrics — phase 8.

## Зависимости

- **phase 2:** `IncomingMessage(origin="scheduler")`, per-chat `asyncio.Lock`, `TurnStore.sweep_pending`.
- **phase 3:** Bash allowlist механика, PreToolUse hook, `_bg_tasks` паттерн.
- **phase 4:** memory CLI (scheduler-jobs пишут/читают факты), `_ensure_vault`, `MemorySettings` — опционально (scheduler работает и без memory).
- **Не зависит от phase 6 (media), phase 7 (gh CLI).**

## Риск

**Средний-высокий.** IPC reliability, clock drift при suspend/resume (MacBook закрыл крышку на 8 часов), дубликаты при рестарте, возможность scheduler-turn'а перебить ongoing user-turn через lock-starvation.

**Митигация:** at-least-once семантика на базе `(schedule_id, scheduled_for)` unique + трёхфазный `pending/sent/acked` + recovery-sweep на startup; миссы при suspend — backfill опционально (сделать поведение "выкидывать миссы > 1 час" — S-2 catchup discipline); per-chat lock уже сериализует turn'ы — scheduler просто wait'ит.
