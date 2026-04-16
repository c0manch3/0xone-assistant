# Phase 1 — Summary

Документ подводит итоги завершённой фазы 1 проекта `0xone-assistant`. Источники: `plan/phase1/description.md`, `plan/phase1/detailed-plan.md`, `plan/phase1/implementation.md`, исходники `src/assistant/`, два коммита `ee5848a` и `6f9bbb3`.

## 1. TL;DR

Собран минимальный скелет single-user Telegram-бота: aiogram 3.26 polling, router-level owner-filter, text-echo, запись user+assistant блоков в SQLite (`conversations`), structlog JSON в stdout, graceful shutdown. Весь pipeline зелёный: `uv sync` → `just lint` (ruff + mypy strict) → `just test` (3 теста). Схема БД и контракт записи сразу совместимы с Claude SDK content blocks, чтобы phase 2 не стартовала с миграции. Переход к phase 2 (ClaudeBridge + Skills plumbing) разблокирован.

## 2. Что реализовано

Дерево `src/assistant/` (src-layout, пакет `assistant`) и инфраструктура проекта:

| Файл | Роль | Ключевые решения |
|---|---|---|
| `/Users/agent2/Documents/0xone-assistant/src/assistant/__init__.py` | Маркер пакета | Пустой. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/__main__.py` | Entrypoint для `python -m assistant` | Ловит `pydantic.ValidationError` и выходит с кодом 2, чтобы missing env не падал traceback-ом. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/main.py` | `Daemon` + сигнальный loop | `asyncio.Event` + `loop.add_signal_handler` на SIGTERM/SIGINT. `setup_logging` зовётся до `Daemon()`. `stop()` оборачивает шаги в try/except и логгирует `stop_step_failed`. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/config.py` | Settings via pydantic-settings | `@lru_cache(maxsize=1)`, `SettingsConfigDict(env_file=".env", extra="ignore")`, `db_path` как computed property. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/logger.py` | structlog bootstrap | JSONRenderer + `make_filtering_bound_logger`, stdlib `basicConfig(force=True)` чтобы перебить aiogram/aiohttp-конфиги. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/adapters/base.py` | Интерфейс транспорта | `@dataclass(frozen=True, slots=True) IncomingMessage`; `MessengerAdapter(ABC)` с `start/stop/send_text`. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/adapters/telegram.py` | aiogram 3 polling adapter | Router-filter `F.chat.id == owner_chat_id`; два handler'а (`F.text` + catch-all fallback) вместо `~F.text`; `ChatActionSender.typing(...)` around handler; `handle_signals=False` — сигналы ведёт Daemon; polling запущен как task; `stop_polling` в try/except от `RuntimeError/LookupError` (double-stop guard); outer middleware логгирует non-owner updates на DEBUG. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/handlers/message.py` | `EchoHandler` | На каждое сообщение — новый `turn_id`, две строки (user + assistant) с одинаковым `turn_id`, content в виде списка `{type: "text", text}` — SDK-совместимо. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/state/db.py` | Bootstrap БД | `connect()` ставит `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`. `apply_schema()` обёрнут в `BEGIN IMMEDIATE` + rollback-on-error; `PRAGMA user_version` вместо миграционного раннера. |
| `/Users/agent2/Documents/0xone-assistant/src/assistant/state/conversations.py` | `ConversationStore` | `append(blocks)` сериализует в JSON с `default=str` (Path и т.п.); `asyncio.Lock` вокруг INSERT/commit (single-writer guard для aiosqlite); `load_recent(chat_id, limit)` возвращает oldest-first список dict'ов с распакованным content/meta; `new_turn_id()` через `uuid4().hex`. |
| `/Users/agent2/Documents/0xone-assistant/tests/test_db.py` | smoke + round-trip | 3 теста: `test_schema_bootstrap` (WAL/user_version/idempotency), `test_append_roundtrip` (non-JSON-native типы выживают через `default=str`), `test_apply_schema_reopen` (пере-открытие БД не ломает версию). |
| `/Users/agent2/Documents/0xone-assistant/tests/conftest.py` | Пустой | `asyncio_mode=auto` в pyproject покрывает. |
| `/Users/agent2/Documents/0xone-assistant/pyproject.toml` | Манифест пакета | `uv_build` (не hatchling); пины под 2026-04 stable: aiogram 3.26, pydantic-settings 2.6, structlog 25.1, pytest-asyncio 1.0, ruff 0.8, mypy 1.13. Ruff select включает `ASYNC` правила. |
| `/Users/agent2/Documents/0xone-assistant/justfile` | Task runner | `run / test / lint / fmt`; `lint` прогоняет ruff check + format-check + mypy strict. |
| `/Users/agent2/Documents/0xone-assistant/.env.example` | Шаблон конфига | `TELEGRAM_BOT_TOKEN`, `OWNER_CHAT_ID`, `LOG_LEVEL=INFO`. |
| `/Users/agent2/Documents/0xone-assistant/README.md` | Краткий README | Три команды на запуск, ссылка на `plan/README.md`. |
| `/Users/agent2/Documents/0xone-assistant/.gitignore` | | `data/` целиком, `*.db-wal`, `*.db-shm`, стандартные кэши. |

## 3. Ключевые архитектурные решения, зафиксированные фазой

1. **src-layout + пакет `assistant`.** `src/assistant/...`, `module-root = "src"`, запуск через `python -m assistant`. Отсекает ложные импорты из cwd, стандарт для Python-пакетов.
2. **`uv_build` вместо hatchling.** Zero-config backend, быстрее, нативный для `uv`. `[tool.uv.build-backend]` фиксирует layout явно.
3. **Single-user через router-filter, не middleware.** `dp.message.filter(F.chat.id == owner_chat_id)` — сообщения не-owner просто не доходят до handler'ов. Отдельный outer-middleware только логгирует отфильтрованные updates на DEBUG.
4. **Схема `conversations(turn_id, content_json)`.** Колонка `content_json` — JSON-список блоков Claude SDK (`TextBlock`/`ToolUseBlock`/`ToolResultBlock`/`ThinkingBlock`). `turn_id` группирует блоки одного хода модели. Никакого `CHECK role IN (...)` — в phase 2+ понадобится роль `tool`.
5. **`PRAGMA user_version` вместо миграционного раннера.** На одну миграцию раннер избыточен; `CREATE TABLE IF NOT EXISTS` идемпотентен, `apply_schema` атомарен через `BEGIN IMMEDIATE`. Раннер появится в phase 2+ вместе со второй миграцией.
6. **structlog JSON в stdout.** `make_filtering_bound_logger` + `JSONRenderer` + `cache_logger_on_first_use`. Stdlib `basicConfig(force=True)` чтобы подавить любые предшествующие конфиги (aiogram/aiohttp могут настраивать logging при импорте).
7. **aiogram `handle_signals=False`.** Сигналы перехватывает `Daemon` сам через `asyncio.Event` — иначе aiogram глотает SIGINT нестабильно (aiogram#1301). Polling запущен как отдельный `asyncio.Task`, `stop()` делает `await dp.stop_polling()`.
8. **`ChatActionSender.typing` как async-context** вокруг handler'а — пинг каждые 5 с пока обработка длится, готово к phase 2 (Claude думает дольше чем 5 с).
9. **Text-only в phase 1.** Медиа-handler отвечает "Пока принимаю только текст." и ничего не скачивает. Inbox-пути, лимиты, storage — всё в phase 6.
10. **`data/` целиком в `.gitignore`** + явные `*.db-wal`, `*.db-shm`. Nested git-репо для vault будет в phase 7.
11. **`asyncio.Lock` вокруг `ConversationStore.append`.** aiosqlite однопоточен, но несколько await-писателей могут влезть между `INSERT RETURNING` и `commit()`. Lock даёт детерминированный last-write.

## 4. Процесс и что сработало

**Участники (6 ролей агентов):** planner → devil's-advocate reviewer → planner v2 → researcher → coder (фоновой) → parallel code-reviewer + devil's-advocate → coder (fix pack).

**Параллельность принесла выигрыш** на этапе review: code-reviewer и devil's-advocate запускались одновременно и дали непересекающиеся замечания (первый — про concurrency/atomic schema/shutdown guards, второй — про архитектурный долг типа singleton settings, nested config, streaming contract). Coder же остался последовательным — у него цикл "правки под один review bundle".

**Решения, изменившиеся после review:**

| Этап | Что изменилось | Причина |
|---|---|---|
| v1 → v2 плана | Вариант B (скачивание медиа) → Вариант A (отказ) | Inbox-пути всё равно переделаем в phase 6, молчаливое проглатывание голосовых — антифича. |
| v1 → v2 плана | `content TEXT` + `CHECK role` → `content_json` + `turn_id` без CHECK | Иначе миграция 0002 была бы в день 1 phase 2. |
| v1 → v2 плана | Свой migration runner → `PRAGMA user_version` | На одну миграцию runner избыточен. |
| v1 → v2 плана | Симлинк `.claude/skills` → отложен в phase 2 | Мёртвый груз, проблемы со сборкой на Windows. |
| v1 → v2 плана | 3 smoke-теста → 1 (`test_db`) | `test_echo` умрёт в phase 2, `test_config` дублирует pydantic. |
| v2 → implementation | `hatchling` → `uv_build`; aiogram 3.13 → 3.26 с `DefaultBotProperties` | Researcher обновил пины под 2026-04 stable и сверил SDK-контракты. |
| v2 → implementation | Один `Settings()` без кэша → `@lru_cache(maxsize=1)` | Не перечитывать `.env` каждый вызов. |
| v2 → implementation | `~F.text` → два handler'а (F.text + fallback) | Надёжнее на caption-only сообщениях. |
| initial → fix pack (`6f9bbb3`) | Добавлены `asyncio.Lock` в store, `BEGIN IMMEDIATE` в schema, `load_recent`, guard на double-stop в adapter, try/except вокруг cleanup шагов, `default=str` в json.dumps, 2 новых теста (round-trip, reopen) | Review показал гонки записи, уязвимость `Path`-сериализации, отсутствие symmetric load, риск падения `stop_polling` если он уже вызван. |

**Размер fix-pack diff:** 8 файлов, +153 / -37 строк.

## 5. Отложенный технический долг (для phase 2+)

| # | Приоритет | Замечание | Файл:строка | Фаза закрытия |
|---|---|---|---|---|
| 1 | 🟡 | Nested Settings секции | `src/assistant/config.py:9` — все поля в одном классе. Пригорит когда добавим Claude/Scheduler/GitHub секции — получим 20+ top-level env vars. | Phase 2 при добавлении `CLAUDE_*` env ввести `TelegramSettings`, `ClaudeSettings` как sub-models. |
| 2 | 🟡 | Singleton `get_settings()` vs DI | `src/assistant/config.py:22` — `@lru_cache` делает его глобальным; каждый модуль, читающий config, становится нетестируемым без `get_settings.cache_clear()`. | Phase 2: `Daemon.__init__` принимает `Settings` параметром, модули не зовут `get_settings()` напрямую. |
| 3 | 🔴 | `Handler.handle` не стримит | `src/assistant/handlers/message.py:12` — сейчас `async def handle(msg) -> None`. Claude SDK выдаёт stream ассистент-блоков — нужен контракт `async def handle(msg) -> AsyncIterator[str]` (или `AsyncIterator[Block]`) + adapter должен уметь `send_text` по частям. | Phase 2 обязательно — это меняет сигнатуру `MessengerAdapter` тоже. |
| 4 | 🟡 | Нормализация `turns` / `messages` | `src/assistant/state/conversations.py:26` — сейчас `conversations` это плоская таблица с `turn_id TEXT`. По мере роста истории JOIN'ы по `turn_id` без FK будут болеть. | Phase 2+: выделить `turns(id PK, chat_id, started_at, ...)` и `messages(turn_id FK, role, content_json, ...)`. Делать вместе с миграционным раннером. |
| 5 | 🟡 | Owner-filter только на `message` | `src/assistant/adapters/telegram.py:38` — фильтр навешен на `dp.message`. `edited_message`, `callback_query`, `inline_query` его не наследуют. Пока мы их не слушаем — не пригорит, но как только phase 3 (skill-installer) потянет inline-кнопки / confirm-callbacks — non-owner сможет их триггерить. | Phase 3: либо аналогичные filter'ы на каждый observer, либо auth-middleware с центральной проверкой. |
| 6 | 🟢 | DefaultBotProperties(HTML) без обоснования | `src/assistant/adapters/telegram.py:30` — HTML parse_mode взяли по умолчанию, но в echo он не нужен. Когда phase 2 начнёт возвращать Markdown от Claude — этот выбор пригорит (придётся либо эскейпить HTML, либо менять на MarkdownV2). | Phase 2: решение "HTML vs MarkdownV2" с учётом того, что Claude любит markdown. |

## 6. Метрики

- **LOC исходников (без тестов):** 393 строки в 13 `.py`-файлах.
- **LOC тестов:** 76 строк, 3 теста в одном файле.
- **Файлов по папкам:**
  - `src/assistant/` — 3 модуля верхнего уровня + 3 подпакета (`adapters/`, `handlers/`, `state/`).
  - `adapters/` — 2 файла (`base.py`, `telegram.py`).
  - `handlers/` — 1 (`message.py`).
  - `state/` — 2 (`db.py`, `conversations.py`).
  - `tests/` — 1 тестовый модуль.
- **Коммиты:** 2 в `main` — `ee5848a phase 1: telegram echo skeleton` (initial) и `6f9bbb3 phase 1: apply review fixes (concurrency, atomic schema, load_recent, shutdown guards, tests)`.
- **Размер fix-pack diff:** 8 файлов, +153 / -37.
- **CI-gates:** `uv sync` OK, `just lint` зелёный (ruff check + ruff format --check + mypy strict), `just test` — 3/3 passed.

## 7. Готовность к phase 2

**Можно начинать без новых архитектурных решений:**

- ClaudeBridge модуль (`src/assistant/bridge/claude.py`) — точка для `ClaudeAgentSDKClient` с `setting_sources=["project"]`, semaphore, path-guard для `Read/Write/Edit/Glob/Grep`.
- Симлинк `.claude/skills → ../skills` (создаётся программно в `main.py` до запуска adapter'а).
- SKILL.md parsing для manifest'а в system prompt (frontmatter: `name`, `description`, `allowed-tools`).
- Smoke-skill `skills/ping/` + CLI `tools/ping/` — подтвердить auto-discovery SDK.
- Замена `EchoHandler` на `ClaudeHandler` — contract сохраняется (`async def handle(msg)`), store.append остаётся API-совместимым т.к. `content_json` уже принимает SDK блоки.

**Требует новых решений до коммита кода phase 2:**

- 🔴 **Streaming handler contract.** Решить — `AsyncIterator[str]` или один финальный `send_text`? От этого зависит batching в Telegram (4096-лимит) и UX "тайпинга". См. долг #3.
- 🔴 **SDK error handling.** Стратегия при `ClaudeError`, rate-limit'ах, MCP-тулах, падающих посреди стрима. Что пишем в `conversations` при частичном ответе?
- 🟡 **System prompt manifest** — собирается из `skills/*/SKILL.md` при каждом запросе или раз при старте и invalidate-on-file-change? Влияет на latency.
- 🟡 **Parse mode решение** (HTML vs MarkdownV2) — см. долг #6.
- 🟡 **Spike на auto-discovery скилов SDK** (30 мин в plan/README.md §риски) — подтвердить, что `setting_sources=["project"]` + `cwd` подхватывает `.claude/skills/` без ручного инжекта в system prompt.
- 🟡 **Config sectioning** (долг #1) — если добавляются >4 новых env vars, вводим sub-models сразу.
- 🟡 **Длинные ответы** — `_split_message` для >4096 символов, отложенный из v1 плана. К phase 2 уже не теоретический.

---

Phase 1 закрыт. Каркас стабилен, решения верифицированы и задокументированы, явный техдолг расписан по фазам. Готов к phase 2 после принятия пяти решений выше.
