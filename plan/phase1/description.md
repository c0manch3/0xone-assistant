# Phase 1 — Skeleton

**Цель:** каркас проекта, Telegram text-echo от owner, запись диалога в SQLite. Claude ещё не подключаем.

**Вход:** пустой репозиторий.

**Выход:** запущенный бот, эхает текст от `OWNER_CHAT_ID`, пишет в `conversations`, медиа вежливо отклоняет.

> Детальный план со всеми решениями, кодом и обоснованиями — в `detailed-plan.md` (версия 2, после devil's advocate review).

## Задачи (по порядку)

1. `pyproject.toml` + `uv` (python 3.12; deps: `aiogram`, `pydantic-settings`, `aiosqlite`, `structlog`).
2. Дерево каталогов: `src/assistant/{adapters,handlers,state}`, `skills/`, `tools/`, `daemon/`, `tests/`. **Без** симлинка `.claude/skills` — он в phase 2.
3. `src/assistant/config.py` (pydantic-settings + `@lru_cache`; env: `TELEGRAM_BOT_TOKEN`, `OWNER_CHAT_ID`, `DATA_DIR`, `LOG_LEVEL`).
4. `src/assistant/logger.py` (structlog + JSON в stdout).
5. `src/assistant/state/db.py` — aiosqlite WAL, `CREATE TABLE IF NOT EXISTS` + `PRAGMA user_version` (без собственного миграционного раннера).
6. Схема `conversations(id, chat_id, turn_id, role, content_json, meta_json, created_at)` — совместима с блочной моделью Claude SDK.
7. `src/assistant/state/conversations.py` — `ConversationStore.append(blocks: list[dict])`.
8. `src/assistant/adapters/base.py` + `adapters/telegram.py` — aiogram 3 polling, owner-filter `F.chat.id == owner_id` на роутере, text-only. Медиа → ответ-отказ, ничего не скачиваем.
9. `src/assistant/handlers/message.py` — EchoHandler, пишет user+assistant блоками в одном `turn_id`.
10. `src/assistant/main.py` + `__main__.py` (asyncio entry, SIGTERM/SIGINT).
11. `justfile`, `.env.example`, `.gitignore` (data/ целиком + `*.db-wal`, `*.db-shm`), краткий `README.md`.
12. 1 smoke-тест `tests/test_db.py` (WAL, user_version, append идемпотентность).
13. `git init` + первый коммит.

## Критерии готовности

- `uv sync` без ошибок, `just lint` (ruff + mypy strict) зелёный, `just test` проходит.
- `just run` с корректным `.env` запускает бота.
- Text от owner → echo + 2 строки в `conversations` c общим `turn_id`.
- Voice/photo/document от owner → "медиа пока не поддерживаю", в `conversations` ничего.
- Сообщение от non-owner не доходит до хендлеров.
- `SIGTERM` → graceful shutdown.
- `data/` отсутствует в git.

## Зависимости

Нет.

## Риск

**Низкий.** Единственная точка для верификации — точный API aiogram 3.13 для shutdown (5 минут в docs перед реализацией `Daemon.stop()`).

## Явно НЕ в phase 1

- Claude SDK, скилы, симлинк `.claude/skills` → phase 2.
- Скачивание/обработка медиа → phase 6.
- Нарезка длинных сообщений → phase 2.
- Миграционный раннер с версионированием → phase 2+.
- Тесты конфига и echo-хендлера (умрут в phase 2) → пропускаем.
