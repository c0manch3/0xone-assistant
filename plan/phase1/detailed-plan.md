# Phase 1 — Detailed Plan (v2)

Обновлён после devil's advocate review — убрана спекулятивная инфраструктура, схема БД приведена под Claude SDK, техбаги в сниппетах исправлены.

## Что изменилось относительно v1

| Было | Стало | Причина |
|---|---|---|
| Вариант B: скачивание медиа в phase 1 | **Вариант A:** только text, медиа → "не поддерживаю" | Пути inbox в phase 6 всё равно другие; молчаливое "глотание" голосовых — антифича |
| `content TEXT` + `CHECK role IN (...)` | `content_json TEXT` + `turn_id` + role без CHECK | Claude SDK работает со списком блоков (TextBlock/ToolUseBlock/ToolResultBlock), иначе миграция 0002 в день 1 phase 2 |
| Свой миграционный раннер + `schema_migrations` | `CREATE TABLE IF NOT EXISTS` + `PRAGMA user_version` | Одна миграция — раннер избыточен, раннер в phase 2 когда появится вторая |
| Симлинк `.claude/skills → ../skills` | Создаём программно в phase 2 | Мёртвый груз до phase 2; симлинки в git проблемны на Windows |
| 3 smoke-теста (config, db, echo) | Только `test_db` | `test_echo` умрёт в phase 2, `test_config` дублирует pydantic |
| `data/*` + `.gitkeep`-трюки | `data/` целиком в `.gitignore` + `*.db-wal`, `*.db-shm` | Nested git в phase 7; db-wal/shm файлы утекали |
| `strftime('%Y-%m-%dT%H:%M:%fZ','now')` | `strftime('%Y-%m-%dT%H:%M:%SZ','now')` | SQLite не поддерживает `%f` |
| `get_settings()` без кэша | `@lru_cache` | Не перечитывать `.env` каждый вызов |
| Owner-filter через middleware | `F.chat.id == owner_id` на роутере | Проще, идиоматичнее aiogram 3 |
| `_split_message` в phase 1 | Отложено в phase 2 | В echo длинных сообщений не будет; нарезка рискует ломать Markdown |

## Сводка решений (итоговая)

| # | Вопрос | Решение |
|---|---|---|
| 1 | Менеджер пакетов | `uv`, пакет `assistant` (src-layout) |
| 2 | Объём фазы | Text-echo, запись в `conversations`, без медиа |
| 3 | Auth | Одно `OWNER_CHAT_ID` (int), filter на роутере |
| 4 | Логи | `structlog`, только stdout |
| 5 | `skills/` и симлинк | Пустая `skills/` есть; симлинк — в phase 2 |
| 6 | Конфиг | Только `.env`, `@lru_cache` |
| 7 | Тесты | pytest + 1 smoke (`test_db`) |
| 8 | CLAUDE.md | Phase 2 |
| 9 | Схема `conversations` | `turn_id` + `content_json` |
| 10 | Миграции | `CREATE TABLE IF NOT EXISTS` + `PRAGMA user_version` |
| 11 | Data-каталог | `./data/` целиком в `.gitignore` |
| 12 | Non-owner | Router-filter, не доходит до handler; отдельный debug-лог |
| 13 | Dev-запуск | `uv run python -m assistant` |
| 14 | Task runner | justfile |
| 15 | Lint/format | ruff + mypy |
| 16 | Python | 3.12 |
| 17 | Медиа в адаптере | **Вариант A:** text-only; voice/photo/doc → "пока не поддерживаю" |
| 18 | Длинные сообщения | Отложено в phase 2 |
| 19 | Git | init + `.gitignore` + первый коммит |
| 20 | Не-текст в `conversations` | Не пишем |
| 21 | Inbox-пути | Не трогаем в phase 1 |
| 22 | Лимит скачивания | N/A в phase 1 |
| 23 | Typing action | Да |
| 24 | README.md | Да, краткий |

## Дерево файлов к концу phase 1

```
0xone-assistant/
├── .env.example
├── .gitignore
├── .python-version               # 3.12
├── README.md
├── justfile
├── pyproject.toml
├── uv.lock
├── skills/.gitkeep               # пустая — симлинк создадим в phase 2
├── tools/.gitkeep
├── daemon/.gitkeep
├── plan/                          # уже есть
├── src/
│   └── assistant/
│       ├── __init__.py
│       ├── __main__.py
│       ├── main.py
│       ├── config.py
│       ├── logger.py
│       ├── adapters/
│       │   ├── __init__.py
│       │   ├── base.py           # IncomingMessage (только text), MessengerAdapter
│       │   └── telegram.py
│       ├── handlers/
│       │   ├── __init__.py
│       │   └── message.py        # EchoHandler
│       └── state/
│           ├── __init__.py
│           ├── db.py             # connect + apply_schema (CREATE IF NOT EXISTS)
│           └── conversations.py  # ConversationStore
└── tests/
    ├── __init__.py
    ├── conftest.py
    └── test_db.py
```

`data/` будет создан при первом запуске; в git его нет вовсе.

## Пошаговая реализация

### Шаг 1 — `pyproject.toml`

```toml
[project]
name = "assistant"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "aiogram>=3.13,<4",
  "pydantic>=2.9",
  "pydantic-settings>=2.5",
  "aiosqlite>=0.20",
  "structlog>=24.4",
]

[dependency-groups]
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=0.24",
  "ruff>=0.6",
  "mypy>=1.11",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/assistant"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "SIM", "RUF"]

[tool.mypy]
strict = true
python_version = "3.12"

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

### Шаг 2 — Каталоги + .gitignore

```bash
mkdir -p src/assistant/{adapters,handlers,state} skills tools daemon tests
touch skills/.gitkeep tools/.gitkeep daemon/.gitkeep
```

`.gitignore`:
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
```

`uv.lock` коммитим (репродусибл).

### Шаг 3 — `config.py`

```python
from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    owner_chat_id: int
    data_dir: Path = Path("./data")
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
```

`.env.example`:
```
TELEGRAM_BOT_TOKEN=
OWNER_CHAT_ID=
LOG_LEVEL=INFO
```

### Шаг 4 — `logger.py`

```python
import logging
import sys
import structlog


def setup_logging(level: str = "INFO") -> None:
    level_int = logging.getLevelNamesMapping()[level.upper()]
    logging.basicConfig(stream=sys.stdout, level=level_int, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
```

### Шаг 5 — `state/db.py`

```python
from pathlib import Path
import aiosqlite

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    turn_id      TEXT NOT NULL,
    role         TEXT NOT NULL,
    content_json TEXT NOT NULL,
    meta_json    TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_conversations_chat_time
    ON conversations(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_turn
    ON conversations(chat_id, turn_id);
"""


async def connect(path: Path) -> aiosqlite.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


async def apply_schema(conn: aiosqlite.Connection) -> None:
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    current = row[0] if row else 0
    if current >= SCHEMA_VERSION:
        return
    await conn.executescript(SCHEMA_SQL)
    await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    await conn.commit()
```

**Пояснение схемы:**
- `turn_id` (UUID/ULID): группирует блоки одного "хода" модели. В phase 1 — один блок на строку, но `turn_id` уже есть.
- `content_json`: JSON, совместимый со списком блоков Claude SDK. В phase 1 EchoHandler пишет `[{"type":"text","text":"..."}]`. В phase 2 сможет писать `[{"type":"tool_use", ...}, {"type":"text", ...}]` без миграции.
- `role` без CHECK — в будущем понадобится `tool` role или другое.

### Шаг 6 — `state/conversations.py`

```python
import json
import uuid
import aiosqlite


class ConversationStore:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def append(
        self,
        chat_id: int,
        turn_id: str,
        role: str,
        blocks: list[dict],
        meta: dict | None = None,
    ) -> int:
        async with self._conn.execute(
            "INSERT INTO conversations(chat_id, turn_id, role, content_json, meta_json) "
            "VALUES (?,?,?,?,?) RETURNING id",
            (chat_id, turn_id, role, json.dumps(blocks, ensure_ascii=False),
             json.dumps(meta, ensure_ascii=False) if meta else None),
        ) as cur:
            row = await cur.fetchone()
        await self._conn.commit()
        return row[0]  # type: ignore[index]

    @staticmethod
    def new_turn_id() -> str:
        return uuid.uuid4().hex
```

### Шаг 7 — `adapters/base.py`

```python
from dataclasses import dataclass
from abc import ABC, abstractmethod


@dataclass
class IncomingMessage:
    chat_id: int
    message_id: int
    text: str


class MessengerAdapter(ABC):
    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...
    @abstractmethod
    async def send_text(self, chat_id: int, text: str) -> None: ...
```

Никаких `ImageAttachment`/`VoiceAttachment` в phase 1 — вернём в phase 6 вместе с реальной обработкой.

### Шаг 8 — `adapters/telegram.py`

- `aiogram 3`: `Bot(token=...)`, `Dispatcher()`.
- **Owner-filter на роутере:** `dp.message.filter(F.chat.id == settings.owner_chat_id)` — сообщения от не-owner вообще не доходят до хендлеров, aiogram пишет их только в debug-лог polling.
- Text-хендлер: `@dp.message(F.text)` → `chat.send_chat_action(ChatAction.TYPING)` → собираем `IncomingMessage` → `await handler.handle(msg)`.
- Медиа-хендлер: `@dp.message(~F.text)` → `await bot.send_message(chat_id, "медиа пока не поддерживаю, придёт в phase 6")`. Ничего не скачиваем.
- **Про shutdown**: проверить актуальный API aiogram 3.13 — в 3.x это `await dp.stop_polling()` либо отмена задачи `start_polling`. **Зафиксируем точный вызов во время реализации, не по памяти.** Точно работает `await bot.session.close()` в `stop()`.
- **Про download** — в phase 6 использовать `await bot.download(message.voice, destination=...)`. Здесь проверки API не нужно.

### Шаг 9 — `handlers/message.py`

```python
from assistant.adapters.base import IncomingMessage, MessengerAdapter
from assistant.state.conversations import ConversationStore


class EchoHandler:
    def __init__(self, conv: ConversationStore, adapter: MessengerAdapter):
        self._conv = conv
        self._adapter = adapter

    async def handle(self, msg: IncomingMessage) -> None:
        turn = ConversationStore.new_turn_id()
        await self._conv.append(
            msg.chat_id, turn, "user",
            [{"type": "text", "text": msg.text}],
            meta={"message_id": msg.message_id},
        )
        reply = f"echo: {msg.text}"
        await self._conv.append(
            msg.chat_id, turn, "assistant",
            [{"type": "text", "text": reply}],
        )
        await self._adapter.send_text(msg.chat_id, reply)
```

### Шаг 10 — `main.py` + `__main__.py`

`main.py`:
```python
import asyncio, signal
from assistant.config import get_settings
from assistant.logger import setup_logging, get_logger
from assistant.state.db import connect, apply_schema
from assistant.state.conversations import ConversationStore
from assistant.adapters.telegram import TelegramAdapter
from assistant.handlers.message import EchoHandler

log = get_logger("main")


class Daemon:
    def __init__(self):
        self._settings = get_settings()
        self._conn = None
        self._adapter: TelegramAdapter | None = None

    async def start(self) -> None:
        setup_logging(self._settings.log_level)
        self._conn = await connect(self._settings.db_path)
        await apply_schema(self._conn)
        store = ConversationStore(self._conn)
        self._adapter = TelegramAdapter(self._settings)
        handler = EchoHandler(store, self._adapter)
        self._adapter.set_handler(handler)
        await self._adapter.start()

    async def stop(self) -> None:
        if self._adapter:
            await self._adapter.stop()
        if self._conn:
            await self._conn.close()


async def main() -> None:
    d = Daemon()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    start_task = asyncio.create_task(d.start())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait({start_task, stop_task},
                                     return_when=asyncio.FIRST_COMPLETED)
        if start_task in done:
            start_task.result()  # re-raise
    finally:
        await d.stop()
```

`__main__.py`:
```python
import asyncio
from assistant.main import main

if __name__ == "__main__":
    asyncio.run(main())
```

### Шаг 11 — `justfile`

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

### Шаг 12 — Тесты

Только `test_db.py`:

```python
import pytest
from pathlib import Path
from assistant.state.db import connect, apply_schema, SCHEMA_VERSION
from assistant.state.conversations import ConversationStore


async def test_schema_bootstrap(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = await connect(db)
    await apply_schema(conn)

    async with conn.execute("PRAGMA journal_mode") as cur:
        assert (await cur.fetchone())[0] == "wal"

    async with conn.execute("PRAGMA user_version") as cur:
        assert (await cur.fetchone())[0] == SCHEMA_VERSION

    # Idempotency
    await apply_schema(conn)

    store = ConversationStore(conn)
    turn = ConversationStore.new_turn_id()
    row_id = await store.append(42, turn, "user", [{"type":"text","text":"hi"}])
    assert row_id == 1
    await conn.close()
```

`conftest.py` — минимальный (asyncio_mode уже в `pyproject.toml`).

### Шаг 13 — README.md

Краткий:
- Что это: персональный Telegram-бот на Claude Code SDK (single-user).
- Как поднять: `uv sync` → `cp .env.example .env` → заполнить → `just run`.
- Структура папок — ссылка на `plan/README.md`.

### Шаг 14 — Git

```bash
git init
git add .
git commit -m "phase 1: telegram echo skeleton"
```

## Критерии готовности phase 1

1. `uv sync` без ошибок.
2. `just lint` зелёный (ruff + mypy strict).
3. `just test` — 1 тест проходит.
4. `just run` с корректным `.env` запускает бота.
5. Text от owner → эхо + 2 строки (user + assistant) в `conversations`, одинаковый `turn_id`.
6. Voice/photo/document от owner → ответ "медиа пока не поддерживаю". В `conversations` ничего не пишется.
7. Сообщение от non-owner → не доходит до хендлеров (router-filter), только debug-лог.
8. `SIGTERM` → graceful shutdown.
9. `PRAGMA journal_mode` = `wal`, `PRAGMA user_version` = 1.
10. `data/` нет в git.

## Явные не-цели

- Claude SDK, скилы, ClaudeBridge, симлинк `.claude/skills` — phase 2.
- Обработка медиа и скачивание файлов — phase 6.
- Нарезка длинных сообщений (>4096) — phase 2.
- Scheduler, cron, IPC — phase 5.
- Миграционный раннер с версионированием (сейчас достаточно `user_version`) — phase 2+.

## Риски

- **aiogram 3 shutdown API** — `Dispatcher.stop_polling()` vs альтернативы. **Перед реализацией Daemon.stop() — 5 минут на docs/quickstart aiogram 3.13.**
- **WAL и concurrent tests** — если понадобится несколько тестов с БД, использовать новую `tmp_path` в каждом.

## Чек-лист исполнения

1. `uv init --package --python 3.12` → расширить `pyproject.toml`.
2. Каталоги + `.gitignore` + `.gitkeep`.
3. `config.py` + `.env.example`.
4. `logger.py`.
5. `db.py` + `conversations.py`.
6. `adapters/base.py` + `adapters/telegram.py` (сверить shutdown API с docs aiogram 3).
7. `handlers/message.py`.
8. `main.py` + `__main__.py`.
9. `justfile` + README.
10. `test_db.py`.
11. Ручной прогон: text echo, медиа-отказ, non-owner тишина, SIGTERM.
12. `git init` + первый коммит.
