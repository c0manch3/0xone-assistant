# Phase 5 — Scheduler daemon + UDS IPC

**Цель:** cron-задачи хранятся в SQLite, исполняются отдельным демоном, доставляются в бота через Unix domain socket.

**Вход:** phase 4.

**Выход:** пользователь говорит "запланируй ежедневное саммари в 9 утра" → модель зовёт `tools/schedule add` → демон срабатывает в 09:00 → бот обрабатывает виртуальное сообщение и отвечает.

## Задачи

1. Миграция DB: таблицы
   - `schedules(id, cron, prompt, enabled, created_at, last_fire)`
   - `triggers(id, schedule_id, prompt, scheduled_for, status, attempts)` c уникальным индексом `(schedule_id, scheduled_for)`.
2. `tools/schedule/main.py` CLI: `add --cron --prompt`, `list`, `rm ID`, `enable/disable ID`.
3. `skills/scheduler/SKILL.md` с примерами cron-выражений.
4. `daemon/main.py` — APScheduler читает `schedules`, материализует due-строки в `triggers`, затем шлёт JSON-line в `data/run/bot.sock`. Supervised retries с бэкоффом если сокета нет.
5. `src/scheduler/ipc.py` — UDS-сервер на стороне бота; принятый JSON → синтезированный `IncomingMessage(origin="scheduler")` → тот же `MessageHandler`.
6. Примеры systemd/launchd unit'ов для демона.
7. Демон использует тот же Python-venv, что и бот (шарит модуль DB-схемы).

## Критерии готовности

- Запланированный job срабатывает ±5 секунд, сообщение приходит в Telegram.
- Перезапуск бота не теряет триггеры (`status` в DB транзиционирует: `pending` → `delivered`).
- Дубликатов нет (unique index).

## Зависимости

Phase 2 (handler), phase 4 (память — опционально).

## Риск

**Средний-высокий.** IPC reliability, clock drift, дубликаты срабатываний.

**Митигация:** `triggers.status` transitions (`pending` → `sent` → `acked`), unique `(schedule_id, scheduled_for)` index, at-least-once семантика с идемпотентностью по `trigger.id`.
