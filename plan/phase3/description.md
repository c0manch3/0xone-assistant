# Phase 3 — Skill-creator & skill-installer

**Цель:** бот умеет нативно расширять себя — создавать новые скилы с CLI-тулами и устанавливать скилы по ссылке.

**Вход:** phase 2.

**Выход:** две возможности:
1. Диалог "сделай скилл для X" → модель генерирует `skills/<name>/SKILL.md` + каркас `tools/<name>/` с `uv`-проектом.
2. Сообщение с URL (git-репо, GitHub-папка, gist, raw `SKILL.md`) → бот клонирует/скачивает, валидирует, ставит в `skills/` (и `tools/` если есть).

## Задачи

1. **`tools/skill-creator/`** — typer CLI:
   - `scaffold NAME --description --allowed-tools` → создаёт `skills/NAME/SKILL.md` (с frontmatter) + `tools/NAME/{pyproject.toml,main.py}` через `uv init`.
   - `validate PATH` — проверяет frontmatter (`name`, `description`, `allowed-tools`), синтаксис SKILL.md, исполняемость `tools/<name>/main.py`.
   - `list` — показывает все установленные скилы + статус валидации.
   - `remove NAME` — удаляет скилл и (опционально) связанный tool.
2. **`tools/skill-installer/`** — CLI для установки из внешних источников:
   - `install URL [--name]` — принимает:
     - git-репо (`https://github.com/user/repo` или `git@…`) — клонирует во временную папку, ищет `SKILL.md` в корне или в подпапке.
     - GitHub tree URL (`github.com/user/repo/tree/main/skills/foo`) — использует `gh api` или raw-download.
     - gist URL.
     - прямая ссылка на `SKILL.md` (raw) — скачивает один файл.
   - Шаги: download → sandbox-validate (запускает `skill-creator validate`) → copy в `skills/<name>/` → если есть `tool/` или `pyproject.toml` — копирует в `tools/<name>/` и делает `uv sync`.
   - Безопасность: dry-run по умолчанию показывает что будет установлено; финальная установка требует подтверждения (одобрение owner'а в Telegram-диалоге).
3. **`skills/skill-creator/SKILL.md`** — инструкция для модели: когда пользователь просит сделать/подключить скилл, использовать эти CLI.
4. **`skills/skill-installer/SKILL.md`** — триггер на URL в сообщении, с примерами.
5. **Auto-manifest в system prompt** (уточнение к phase 2): на каждый запрос `ClaudeBridge` пересканирует `skills/*/SKILL.md` и добавляет компактный манифест (name + description) в system prompt. Новый скилл сразу виден модели без рестарта.
6. **Reload-хук**: SDK и так заново читает `.claude/skills/` на каждый `query()`, но на всякий случай — опциональный `skill-creator reload` сигналит боту обновить manifest-кэш (если мы его заведём).
7. **Telegram-трюк для URL:** в `MessageHandler` детектить URL в сообщении и подсовывать модели хинт "возможно пользователь хочет установить скилл — проверь через skill-installer".

## Критерии готовности

- "сделай скилл для работы с погодой через OpenWeather" → появляется `skills/weather/` + `tools/weather/` с рабочим каркасом, модель сразу может им пользоваться в следующем сообщении.
- Скидываешь ссылку на публичный репо со `SKILL.md` → бот показывает preview → подтверждаешь → скилл установлен и доступен.
- Сломанный `SKILL.md` (битый frontmatter) отклоняется валидатором с понятной ошибкой.

## Зависимости

Phase 2 (skills plumbing).

## Риск

**Средне-высокий.** Главный — security: установка чужого скилла = исполнение чужого кода на хосте с полным Bash. Митигации:
- Обязательный preview+confirm перед install.
- Показывать diff: список файлов, размер, entry point.
- В `SKILL.md` высвечивать `allowed-tools` и предупреждать о sensitive (Bash, WebFetch, Write в tools/).
- (Future) запускать CLI-тулы нового скилла в отдельном sandbox'е — *не в MVP*, но задокументировать.
