# 0xone-assistant — план реализации

Персональный Telegram-бот (single-user) на базе Claude Code SDK, где весь функционал за пределами "чата с Claude" реализован как самостоятельные CLI-микропрограммы, доступные модели через Claude Code Skills. Референс — `/Users/agent2/Documents/midomis-bot`, но HTTP-сайдкары (whisper/flux/document servers) заменены на локальные CLI-инструменты, вызываемые через `Bash`.

## Ключевые архитектурные решения

1. **Single-user.** Бот обслуживает одного владельца (`OWNER_CHAT_ID`). Никакой multi-user изоляции, per-user vault, rate-limit per user и т.п.
2. **CLI-first модель возможностей.** `tools/<name>/` — самостоятельные исполняемые скрипты (каждый со своим venv / `uv`-проектом). Бот никогда не импортирует их код. Claude вызывает их через `Bash`. Вход = stdin/argv, выход = JSON на stdout.
3. **Skills как слой обнаружения.** `skills/<name>/SKILL.md` содержит frontmatter (`name`, `description`, `allowed-tools`) и примеры. `ClaudeBridge` передаёт `setting_sources=["project"]` и ставит SDK `cwd` в корень проекта, так что SDK автоподхватывает `.claude/skills/` (делаем симлинк `.claude/skills` → `../skills` на старте).
4. **Bash + WebFetch разрешены полностью** (в отличие от midomis). `can_use_tool` сохраняет path-guard только для файловых тулов (`Read/Write/Edit/Glob/Grep`), ограниченных cwd проекта + `tools/`.
5. **Долговременная память = Obsidian-хранилище в `data/vault/`.** Доступ строго через CLI `tools/memory` (FTS5 индекс, заметки с frontmatter). Системный промпт явно говорит модели: "твоя долговременная память живёт в Obsidian-хранилище, достучаться можно только через скилл `memory`".
6. **Scheduler = отдельный демон.** `data/assistant.db` содержит таблицы `schedules` и `triggers`. `tools/schedule` мутирует `schedules`. Демон опрашивает, материализует due-задачи в `triggers` и инжектит виртуальное user-сообщение в `MessageHandler` бота через **Unix domain socket** `data/run/bot.sock` (line-delimited JSON `{prompt, origin:"scheduler"}`). UDS выбран вместо shared-DB polling для instant delivery и чистого backpressure; DB всё равно хранит историю срабатываний.
7. **GitHub через `tools/gh` CLI** (тонкая обёртка над `gh`). Scheduler сеет дефолтную ежедневную задачу: `gh`-коммит+пуш всего `data/vault/` в сконфигурированный репозиторий.
8. **Портируем из midomis:** `ClaudeBridge` (Semaphore + metrics + path-guard), `ConversationStore`, aiogram 3 адаптер, owner-auth по `OWNER_CHAT_ID`.
9. **Нативное расширение.** Бот умеет сам расширять себя через skill-creator (диалоговое создание новых скилов) и skill-installer (установка по URL репо/gist/raw SKILL.md с preview+confirm). Manifest скилов собирается в system prompt при каждом запросе — новый скилл виден модели без рестарта.
10. **Отложено / опционально:** Yandex-адаптер, FastAPI admin panel, расширенный health dashboard, Docker/Caddy deploy.

## Критические файлы (будущие)

- `src/bridge/claude.py` — адаптированный ClaudeBridge с `setting_sources=["project"]` и загрузкой скилов.
- `src/handlers/message.py` — унифицированный handler для Telegram + scheduler-инжектированных сообщений.
- `src/scheduler/ipc.py` — UDS-сервер, принимающий trigger-сообщения.
- `daemon/main.py` — автономный cron-демон.
- `tools/memory/main.py` — memory CLI; де-факто контракт memory API.
- `skills/memory/SKILL.md` — описание как Claude пользуется памятью.

## Риски / tradeoffs

- **Контракт автозагрузки скилов.** Поведение Claude Agent SDK для `setting_sources=["project"]` + `cwd` + `.claude/skills/` нужно проверить эмпирически — если SDK не читает `SKILL.md` ожидаемым образом, fallback — инжектировать описания скилов прямо в system prompt. Рекомендую 30-минутный spike до старта фазы 2.
- **Per-tool venv vs single venv.** У каждого CLI свой venv избегает конфликтов (mlx-whisper vs mflux torch-пины), но утраивает install time и место на диске. Используем `uv` с per-tool `pyproject.toml`.
- **Scheduler IPC.** UDS проще всего; если бот крашится — триггеры не должны теряться: демон сперва пишет в DB, потом шлёт сокет, при разрыве ретраит.
- **Bash без ограничений = большая зона поражения.** Path-guard применяется только к файловым SDK-тулам; у `Bash` нет guard. Поскольку бот single-user и запускается локально под владельцем — приемлемо, но задокументировать явно.
- **Memory через CLI — латентность.** Каждый search спавнит подпроцесс — приемлемо для одного юзера.

## Порядок фаз

1. Skeleton + Telegram echo
2. ClaudeBridge + Skills plumbing (ping smoke skill)
3. **Skill-creator & skill-installer** — бот сам создаёт новые скилы и ставит по URL
4. Memory tool + skill (Obsidian vault + FTS5)
5. Scheduler daemon + UDS IPC
6. Media tools (transcribe / genimage / extract-doc / render-doc)
7. GitHub tool + daily vault auto-commit
8. Ops polish: health metrics / admin panel / Yandex (opt-in)

## Развилки для обсуждения до старта

- **SDK skill auto-discovery** — нужен spike перед фазой 2.
- **Per-tool venvs** — подтвердить согласие на `uv`-managed env под каждый CLI vs один монолитный venv с аккуратными пинами.
