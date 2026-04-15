# Phase 4 — Memory tool + skill

**Цель:** функциональная долговременная память через Obsidian-vault + FTS5, исключительно через `tools/memory`.

**Вход:** phase 3.

**Выход:** модель умеет `search`, `read`, `write`, `list`, `delete` заметок; новая сессия помнит факты из прошлой.

## Задачи

1. `tools/memory/pyproject.toml` (собственный venv; `python-frontmatter`, `sqlite-utils`).
2. `tools/memory/main.py` (typer CLI):
   - `search QUERY [--area] [--limit]`
   - `read PATH`
   - `write PATH --title --tags --body -` (body из stdin)
   - `list [--area]`
   - `delete PATH`
   - `reindex`
3. FTS5 индекс в `data/vault/.index.db`; reindex при write/delete; content-таблица зеркалит frontmatter + body.
4. Markdown-файлы Obsidian-compatible, wikilinks сохраняются.
5. `skills/memory/SKILL.md` с богатыми примерами: "сохрани факт", "вспомни про X", структура area (`inbox/`, `projects/`, `people/`).
6. Обновить system prompt: память — авторитетный долговременный стор; модель должна проактивно писать в inbox.
7. Vault путь — единый `data/vault/` (single-user).

## Критерии готовности

End-to-end диалог:
1. "remember that my wife's birthday is April 3" → write → файл создан, индекс обновлён.
2. Новая сессия → "когда у жены день рождения?" → search → правильный ответ.

## Зависимости

Phase 2. Рекомендуется phase 3 (чтобы модель могла при желании модифицировать сам memory-скилл).

## Риск

**Средний.** Staleness индекса и concurrent writes → single-writer lock в CLI (`fcntl.flock` на `.index.db`).
