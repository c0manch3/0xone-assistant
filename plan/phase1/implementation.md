# Phase 1 — Implementation (research-verified, 2026-04)

Этот документ — итог верификации всех развилок из `detailed-plan.md` против актуальных (апрель 2026) источников. Все неопределённости сняты, код приведён к рабочему виду. Выполняется сверху вниз.

Версии, на которые мы пинимся (known-good на 2026-04-15):

| Пакет | Пин | Выпуск |
|---|---|---|
| Python | 3.12.x | stable |
| aiogram | `>=3.26,<4` | 3.26.0 — latest stable docs |
| aiosqlite | `>=0.20,<0.22` | |
| pydantic | `>=2.9` | |
| pydantic-settings | `>=2.6` | 2.6+ enforces `extra=` strictly |
| structlog | `>=25.1` | 25.x серия |
| pytest-asyncio | `>=1.0` | 1.0 released 2025-05-25 |
| ruff | `>=0.8` | |
| mypy | `>=1.13` | |
| uv | `>=0.8` | включает `uv_build` как дефолтный backend |

---

## 1. Верифицированные решения

| # | Вопрос | Окончательное решение | Источник |
|---|---|---|---|
| 1 | Graceful shutdown `start_polling` | **Не** оборачиваем в собственный signal-handler. Передаём `handle_signals=True` (default) и `close_bot_session=True` (default) в `dp.start_polling(bot)`. Для программной остановки — `await dp.stop_polling()` (устанавливает `_stop_signal`). Кастомную логику вешаем через `@dp.shutdown()` decorator. | [aiogram/dispatcher.py @ dev-3.x](https://github.com/aiogram/aiogram/blob/dev-3.x/aiogram/dispatcher/dispatcher.py), [Discussion #1201](https://github.com/aiogram/aiogram/discussions/1201) |
| 2 | Router-level filter | `dp.message.filter(F.chat.id == owner_id)` — валидно, потому что `Dispatcher` сам является `Router` и у него есть `.message` observer. Эквивалент — создать `Router()`, навесить filter, `dp.include_router(r)`. Для single-user достаточно первого. | [aiogram Router docs](https://docs.aiogram.dev/en/latest/dispatcher/router.html), [dispatcher docs](https://docs.aiogram.dev/en/latest/dispatcher/dispatcher.html) |
| 3 | Typing action | `ChatActionSender.typing(bot=bot, chat_id=chat_id)` как `async with` — современный идиом (пингует каждые 5 с пока блок открыт). `bot.send_chat_action()` — низкоуровневый, гасится через 5 с, использовать не нужно в phase 1. | [Chat action sender docs](https://docs.aiogram.dev/en/latest/utils/chat_action.html) |
| 4 | `bot.download()` | `await bot.download(message.voice, destination=path_or_buffer)` — первый аргумент `File \| str` (file_id или объект с `file_id`), `destination: BinaryIO \| Path \| str \| None`. В phase 1 не используется. | aiogram Bot API (3.26) |
| 5 | Negation filter для не-текста | `~F.text` — валидно (MagicFilter поддерживает bitwise `~`), **НО** надёжнее использовать `F.text.is_(None)` чтобы явно поймать "поле отсутствует". На практике проще и идиоматичнее: **два handler'а — первый с `F.text`, второй без фильтра (fallback)**, регистрировать в таком порядке. Это канон в aiogram 3. | [Magic filters docs](https://docs.aiogram.dev/en/latest/dispatcher/filters/magic_filters.html) |
| 6 | aiosqlite + WAL | Порядок `aiosqlite.connect(path)` → `PRAGMA journal_mode=WAL` верный. WAL **персистентен** — записывается в заголовок БД и сохраняется между соединениями (в отличие от DELETE/TRUNCATE/PERSIST). Гоча: WAL требует `PRAGMA journal_mode` **вне** транзакции; aiosqlite.connect не открывает tx неявно — OK. Второй гоча: на read-only ФС WAL упадёт с `SQLITE_IOERR` — неактуально для нас. | [SQLite WAL](https://sqlite.org/wal.html), [Simon Willison TIL](https://til.simonwillison.net/sqlite/enabling-wal-mode) |
| 7 | structlog pattern | `make_filtering_bound_logger(level_int)` + `JSONRenderer()` + `cache_logger_on_first_use=True` — остаётся каноном в 25.x. `logging.getLevelNamesMapping()` (py3.11+) — корректный способ резолвить имя→int. Для prod ускорения можно заменить JSON сериализатор на `orjson.dumps` (не делаем в phase 1). | [structlog 25.5 Best Practices](https://www.structlog.org/en/stable/logging-best-practices.html), [Performance](https://www.structlog.org/en/stable/performance.html) |
| 8 | uv + build backend | **Убираем hatchling.** `uv init --package` в 0.8+ генерит `uv_build` как backend — zero-config, быстрее, src-layout из коробки. `[build-system] requires = ["uv_build>=0.8,<0.9"]`, `build-backend = "uv_build"`. | [uv build backend docs](https://docs.astral.sh/uv/concepts/build-backend/), [uv init docs](https://docs.astral.sh/uv/concepts/projects/init/) |
| 9 | pydantic-settings | `SettingsConfigDict(env_file=".env", extra="ignore")` — правильно. В 2.6+ при наличии в .env переменных без соответствующего поля и без `extra="ignore"` — `ValidationError`. По умолчанию `case_sensitive=False`, поэтому `TELEGRAM_BOT_TOKEN` в .env мэпится на поле `telegram_bot_token`. | [pydantic-settings docs](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) |
| 10 | Claude SDK content blocks | Схема `content_json = list[dict]` корректна. Канонические shape'ы: `TextBlock{text}`, `ThinkingBlock{thinking, signature}`, `ToolUseBlock{id, name, input}`, `ToolResultBlock{tool_use_id, content, is_error}`. Все сериализуются в JSON без потерь. `AssistantMessage.content` — всегда list, `UserMessage.content` — str ИЛИ list. В нашей схеме оборачиваем user-strings в `[{"type":"text","text": str}]` для единообразия — подтверждено. | [claude-agent-sdk-python/types.py](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/types.py) |
| 11 | pytest-asyncio mode | `asyncio_mode = "auto"` под `[tool.pytest.ini_options]` валиден в 1.0+. В 1.0 сохранена обратная совместимость; но появилось замечание об явности — в долгосроке лучше `"strict"` + `@pytest.mark.asyncio`. Для phase 1 оставляем `"auto"`. | [pytest-asyncio PyPI](https://pypi.org/project/pytest-asyncio/), [1.0 migration guide](https://thinhdanggroup.github.io/pytest-asyncio-v1-migrate/) |

---

## 2. Скорректированные сниппеты

Сниппеты из `detailed-plan.md`, которые остаются без изменений:
- Шаг 3 (config.py) — OK как есть. `@lru_cache`, `SettingsConfigDict(env_file=".env", extra="ignore")` — канон.
- Шаг 4 (logger.py) — OK как есть; `structlog.make_filtering_bound_logger(level_int)` + `JSONRenderer()` подтверждены для structlog 25.x.
- Шаг 5 (db.py) — OK как есть.
- Шаг 6 (conversations.py) — OK как есть.
- Шаг 7 (adapters/base.py) — OK.
- Шаг 9 (handlers/message.py) — OK.
- Шаг 12 (test_db.py) — OK.

Ниже — то, что **меняется**.

### 2.1 `pyproject.toml` (Шаг 1, detailed-plan — полная замена)

```toml
[project]
name = "assistant"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "aiogram>=3.26,<4",
  "pydantic>=2.9",
  "pydantic-settings>=2.6",
  "aiosqlite>=0.20,<0.22",
  "structlog>=25.1",
]

[dependency-groups]
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=1.0",
  "ruff>=0.8",
  "mypy>=1.13",
]

[build-system]
requires = ["uv_build>=0.8,<0.9"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "assistant"
module-root = "src"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "SIM", "RUF", "ASYNC"]

[tool.mypy]
strict = true
python_version = "3.12"

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

Замены:
- `hatchling` → `uv_build` (быстрее, ноль конфига, нативный для uv).
- Added `[tool.uv.build-backend]` чтобы явно зафиксировать `src/assistant/` layout (страховка — дефолт тот же, но делаем явным).
- Apped `ASYNC` в ruff — ловит `asyncio` антипаттерны (sleep без await, blocking-calls).
- pins подняты под current stable серии.

### 2.2 `adapters/telegram.py` (Шаг 8 — полный код, заменяет прозу)

```python
from __future__ import annotations

import asyncio
from typing import Protocol

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from assistant.adapters.base import IncomingMessage, MessengerAdapter
from assistant.config import Settings
from assistant.logger import get_logger

log = get_logger("adapters.telegram")


class _Handler(Protocol):
    async def handle(self, msg: IncomingMessage) -> None: ...


class TelegramAdapter(MessengerAdapter):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bot = Bot(
            token=settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._dp = Dispatcher()
        self._handler: _Handler | None = None
        self._polling_task: asyncio.Task[None] | None = None

        # Router-level owner filter: сообщения не-owner вообще не доходят до handler'ов.
        self._dp.message.filter(F.chat.id == settings.owner_chat_id)

        # Order matters: text handler first, then fallback for everything else.
        self._dp.message.register(self._on_text, F.text)
        self._dp.message.register(self._on_non_text)  # catch-all, no filter

        self._dp.shutdown.register(self._on_shutdown)

    def set_handler(self, handler: _Handler) -> None:
        self._handler = handler

    async def _on_text(self, message: Message) -> None:
        if self._handler is None:
            log.warning("text_received_without_handler")
            return
        assert message.text is not None  # guaranteed by F.text
        assert message.from_user is not None
        incoming = IncomingMessage(
            chat_id=message.chat.id,
            message_id=message.message_id,
            text=message.text,
        )
        async with ChatActionSender.typing(bot=self._bot, chat_id=message.chat.id):
            await self._handler.handle(incoming)

    async def _on_non_text(self, message: Message) -> None:
        log.info("non_text_rejected", content_type=message.content_type)
        await message.answer("Медиа пока не поддерживаю — это будет в phase 6.")

    async def _on_shutdown(self) -> None:
        log.info("telegram_shutdown")

    async def start(self) -> None:
        # handle_signals=True (default) регистрирует SIGTERM/SIGINT сам.
        # close_bot_session=True (default) закроет session на выходе.
        # Запускаем polling в task, чтобы Daemon мог await'ить отдельно.
        self._polling_task = asyncio.create_task(
            self._dp.start_polling(self._bot, handle_signals=False),
            name="aiogram-polling",
        )

    async def stop(self) -> None:
        # Программная остановка: set _stop_signal внутри Dispatcher.
        await self._dp.stop_polling()
        if self._polling_task is not None:
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        await self._bot.session.close()

    async def send_text(self, chat_id: int, text: str) -> None:
        await self._bot.send_message(chat_id=chat_id, text=text)
```

Ключевые отличия от плана:
- `handle_signals=False` — signal-обработку делает `Daemon` в `main.py`, иначе aiogram перехватит SIGINT до нас (известная проблема, [aiogram#1301](https://github.com/aiogram/aiogram/issues/1301)).
- Два handler'а вместо `~F.text` — надёжнее при комбинациях типа "caption без текста".
- `ChatActionSender.typing(...)` оборачивает вызов handler'а — пинг каждые 5 с пока Claude думает (актуально в phase 2+, но API ставим сразу).
- `DefaultBotProperties(parse_mode=HTML)` — корректный 3.x способ задать дефолтный parse_mode (в 3.7+ убрали `parse_mode=` из `Bot(...)`).

### 2.3 `main.py` (Шаг 10 — полная замена)

```python
from __future__ import annotations

import asyncio
import signal

from assistant.adapters.telegram import TelegramAdapter
from assistant.config import get_settings
from assistant.handlers.message import EchoHandler
from assistant.logger import get_logger, setup_logging
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect

log = get_logger("main")


class Daemon:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._conn = None  # type: ignore[var-annotated]
        self._adapter: TelegramAdapter | None = None

    async def start(self) -> None:
        self._conn = await connect(self._settings.db_path)
        await apply_schema(self._conn)
        store = ConversationStore(self._conn)
        self._adapter = TelegramAdapter(self._settings)
        handler = EchoHandler(store, self._adapter)
        self._adapter.set_handler(handler)
        await self._adapter.start()
        log.info("daemon_started", owner=self._settings.owner_chat_id)

    async def stop(self) -> None:
        log.info("daemon_stopping")
        if self._adapter is not None:
            await self._adapter.stop()
        if self._conn is not None:
            await self._conn.close()
        log.info("daemon_stopped")


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    d = Daemon()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    try:
        await d.start()
        await stop_event.wait()
    finally:
        await d.stop()
```

Отличия:
- `setup_logging()` вынесен в `main()` **до** `Daemon()`, чтобы даже ошибки `get_settings()` логировались.
- Убран гимнаст с `asyncio.wait({start_task, stop_task})` — `start()` теперь быстрый (polling запущен внутри adapter'а task'ом), просто ждём `stop_event`. Сигналы ставим **до** aiogram, так что aiogram их не перехватит (`handle_signals=False` в adapter'е).

---

## 3. Step-by-step execution recipe

Все команды выполняются из `/Users/agent2/Documents/0xone-assistant/`. Плановые ожидания в скобках.

```bash
cd /Users/agent2/Documents/0xone-assistant
```

### 3.1 Init

```bash
uv init --package --python 3.12 --name assistant .
```

Ожидаем: создан `pyproject.toml` с `uv_build`, `src/assistant/__init__.py`, `.python-version` = `3.12`, `README.md` (перезапишем).

Проверка:
```bash
grep build-backend pyproject.toml    # → build-backend = "uv_build"
cat .python-version                   # → 3.12
ls src/assistant                      # → __init__.py
```

### 3.2 Структура

```bash
mkdir -p src/assistant/{adapters,handlers,state} tests skills tools daemon
touch src/assistant/adapters/__init__.py \
      src/assistant/handlers/__init__.py \
      src/assistant/state/__init__.py \
      tests/__init__.py \
      skills/.gitkeep tools/.gitkeep daemon/.gitkeep
```

### 3.3 Перезаписать pyproject.toml

Положить содержимое из §2.1.

### 3.4 `.gitignore`

```
.venv/
__pycache__/
*.pyc
.env
data/
*.db
*.db-wal
*.db-shm
.mypy_cache/
.ruff_cache/
.pytest_cache/
dist/
*.egg-info/
```

### 3.5 `.env.example`

```
TELEGRAM_BOT_TOKEN=
OWNER_CHAT_ID=
LOG_LEVEL=INFO
```

### 3.6 Написать исходники

1. `src/assistant/config.py` — из detailed-plan §3 (без изменений).
2. `src/assistant/logger.py` — из detailed-plan §4 (без изменений).
3. `src/assistant/state/db.py` — из detailed-plan §5 (без изменений).
4. `src/assistant/state/conversations.py` — из detailed-plan §6 (без изменений).
5. `src/assistant/adapters/base.py` — из detailed-plan §7 (без изменений).
6. `src/assistant/adapters/telegram.py` — §2.2 выше.
7. `src/assistant/handlers/message.py` — из detailed-plan §9 (без изменений).
8. `src/assistant/main.py` — §2.3 выше.
9. `src/assistant/__main__.py` — из detailed-plan §10 (без изменений).
10. `tests/conftest.py` — пустой файл (mode=auto в pyproject.toml достаточно).
11. `tests/test_db.py` — из detailed-plan §12 (без изменений).

### 3.7 `justfile`

```
default: run

run:
    uv run python -m assistant

test:
    uv run pytest -q

lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy src

fmt:
    uv run ruff format .
```

### 3.8 README.md

Короткий:

```markdown
# 0xone-assistant

Персональный Telegram-бот (single-user) на Claude Code SDK. Phase 1 — text echo.

## Запуск
    uv sync
    cp .env.example .env   # заполнить TELEGRAM_BOT_TOKEN, OWNER_CHAT_ID
    just run

Архитектура и фазы — `plan/README.md`.
```

### 3.9 Sync & lint

```bash
uv sync
# ожидаем: creates .venv, resolves lockfile, no errors

uv run ruff format .
just lint
# ожидаем: All checks passed. Success: no issues found in N source files.

just test
# ожидаем: 1 passed in <1s
```

### 3.10 Ручной smoke-тест (чек-лист)

1. `cp .env.example .env` + заполнить реальные значения.
2. `just run` — в логах JSON `{"event":"daemon_started", "owner": <id>, ...}`.
3. Отправить текст owner-аккаунтом → приходит `echo: <текст>`; в `data/assistant.db`:
   ```
   uv run python -c "import sqlite3,json; c=sqlite3.connect('data/assistant.db'); \
     print(list(c.execute('SELECT role, turn_id, content_json FROM conversations ORDER BY id')))"
   ```
   Ожидаем две строки с одинаковым `turn_id`, roles user/assistant.
4. Отправить голосовое/фото/документ owner-аккаунтом → ответ "Медиа пока не поддерживаю — это будет в phase 6." В БД — ничего нового.
5. Отправить текст с другого аккаунта → тишина, в логах JSON уровня DEBUG от aiogram про отфильтрованный update (если LOG_LEVEL=DEBUG).
6. `Ctrl+C` → видим `daemon_stopping` → `daemon_stopped`, процесс завершается с кодом 0 в течение ≤ 2 секунд.
7. `sqlite3 data/assistant.db 'PRAGMA journal_mode;'` → `wal`.
8. `sqlite3 data/assistant.db 'PRAGMA user_version;'` → `1`.

### 3.11 Git

```bash
git add .
git commit -m "phase 1: telegram echo skeleton"
```

---

## 4. Known gotchas (чего нет в плане, но укусит)

1. **aiogram перехватывает SIGINT.** `start_polling(handle_signals=True)` регистрирует свои handler'ы поверх наших и может проглотить Ctrl+C неожиданным образом ([aiogram#1301](https://github.com/aiogram/aiogram/issues/1301)). Решение: передаём `handle_signals=False` и регистрируем signal handler сами (см. §2.3). Отражено в коде.

2. **`DefaultBotProperties` обязателен** если хочешь установить parse_mode. В 3.7+ `Bot(parse_mode=...)` выкинули; `ParseMode.HTML` ставится только через `default=DefaultBotProperties(parse_mode=...)`. Иначе каждый `send_message` придётся вызывать с `parse_mode=` явно.

3. **`~F.text` ловит пустую строку как True** в некоторых edge-cases (caption-only сообщения). Регистрация двух handler'ов (text first, fallback second) детерминистичнее.

4. **`F.chat.id == owner_id` + channel posts.** Dispatcher.message не слушает `channel_post` по умолчанию — ОК. Но `edited_message` — слушаем или нет? По дефолту нет. Для phase 1 не важно.

5. **`aiosqlite.connect()` + WAL race**. Если приложение упало после `PRAGMA journal_mode=WAL` но до `commit` — WAL-режим уже записан в header (persistent), но `-shm`/`-wal` файлы могут остаться. При рестарте SQLite их подхватит корректно. Но если на следующий старт БД открывается read-only, WAL просто переключится в DELETE в памяти — записи не получится. В наших условиях — не проблема.

6. **pytest-asyncio 1.0 deprecation warnings.** В 1.0 появились warning'и про scope event loop. `asyncio_mode=auto` работает, но в консоли могут всплыть `PytestDeprecationWarning`. Добавить в `[tool.pytest.ini_options]` при необходимости:
   ```toml
   filterwarnings = ["ignore::DeprecationWarning:pytest_asyncio"]
   ```

7. **`Settings()` вызов без параметров + mypy strict.** `BaseSettings` в pydantic-settings 2.6 наследуется от `BaseModel`, и mypy видит поля как required args. `# type: ignore[call-arg]` в `get_settings()` — необходим. План это уже учитывает.

8. **`asyncio.create_task(dp.start_polling(...))` и SIGTERM**. Когда мы делаем `await self._dp.stop_polling()`, polling task завершается чисто. Но если main.py падает **до** `adapter.stop()`, task остаётся висеть. `finally: await d.stop()` покрывает, но если `start()` бросит exception — мы уже отловим в `stop()` потому что `self._adapter` проставлен в `start()` *до* возможного падения polling. Если хочется параноидально чище — обернуть `start()` в try/except с cleanup.

9. **uv lockfile**. `uv sync` создаст `uv.lock` — его **коммитим** (репродусибл сборка), это явно документировано astral-sh. План это уже упоминает.

10. **SQLite `strftime('%Y-%m-%dT%H:%M:%SZ','now')`**. Возвращает UTC с секундной точностью. Если понадобится миллисекундная — `%f` в современной SQLite 3.42+ поддерживается (`strftime('%Y-%m-%dT%H:%M:%fZ')` вернёт `SS.SSS`). План умышленно использует секунды — OK.

11. **structlog и stdlib logging interop**. `make_filtering_bound_logger` **игнорирует** stdlib logging — фильтрация идёт по `level_int` в сам wrapper. `logging.basicConfig(level=...)` тут нужен только чтобы stdlib-логи (например из aiogram/aiohttp) тоже шли в stdout. План делает правильно.

12. **Claude SDK round-trip.** `ToolResultBlock.content` может быть `str | list[dict] | None`. Если записать `None` в `content_json` через `json.dumps` — получим строку `"null"`, но при восстановлении получим `None` обратно. Всё сериализуется без потерь. Для phase 1 пишем только TextBlock — так что вопрос теоретический, но схема к этому готова.

---

## 5. Citations

- aiogram dispatcher source (graceful shutdown, stop_polling): https://github.com/aiogram/aiogram/blob/dev-3.x/aiogram/dispatcher/dispatcher.py
- aiogram SIGINT issue: https://github.com/aiogram/aiogram/issues/1301
- aiogram shutdown discussion: https://github.com/aiogram/aiogram/discussions/1201
- aiogram Chat Action Sender docs: https://docs.aiogram.dev/en/latest/utils/chat_action.html
- aiogram Magic filters docs: https://docs.aiogram.dev/en/latest/dispatcher/filters/magic_filters.html
- aiogram Router docs: https://docs.aiogram.dev/en/latest/dispatcher/router.html
- Claude Agent SDK types: https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/types.py
- Claude Agent SDK docs: https://docs.claude.com/en/api/agent-sdk/python
- uv build backend: https://docs.astral.sh/uv/concepts/build-backend/
- uv init: https://docs.astral.sh/uv/concepts/projects/init/
- SQLite WAL: https://sqlite.org/wal.html
- Simon Willison on WAL: https://til.simonwillison.net/sqlite/enabling-wal-mode
- structlog best practices: https://www.structlog.org/en/stable/logging-best-practices.html
- structlog performance: https://www.structlog.org/en/stable/performance.html
- pytest-asyncio 1.0 migration: https://thinhdanggroup.github.io/pytest-asyncio-v1-migrate/
- pytest-asyncio PyPI: https://pypi.org/project/pytest-asyncio/
- pydantic-settings: https://docs.pydantic.dev/latest/concepts/pydantic_settings/
