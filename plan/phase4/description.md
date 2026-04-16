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

## Известные риски из phase 2

- **History replay strategy теряет tool_result при multi-turn.** Phase 2 зафиксировал решение не реплеить `tool_use`/`tool_result` блоки в SDK history (sidestep orphan tool_use проблемы B2), заменяя их синтетической русской system-note `[system-note: в прошлом ходе были вызваны инструменты: X, Y. Результаты получены.]`.
  
  **Конкретный сценарий провала в phase 4:** turn 1 "что мы знаем про X" → memory.search возвращает 2KB markdown → assistant "нашёл...". Turn 2 "суммируй это" → SDK видит только note + user text, **не видит поисковый результат и свой же ответ**. Модель либо повторит поиск (переплата), либо галлюцинирует.
  
  **Решение (на выбор планировщика phase 4):**
  1. Подтвердить U1 empirically — проверить что SDK 0.1.59+ принимает tool_use/tool_result в async-gen prompt (phase 2 пометил это xfail). Если принимает — включить replay для всех или выборочно для memory-тулов.
  2. Перейти на `resume=session_id` режим SDK — вся история хранится внутри SDK, мы не реконструируем. Требует ревизии `ConversationStore.load_recent` и turns lifecycle.
  3. Расширить synthetic note кратким summary tool_result'а (первые N символов + hint "для деталей вызови инструмент снова"). Компромисс.
  
  Планировщик phase 4 **обязан** явно выбрать решение в первую волну, иначе memory skill будет работать только в рамках одного turn'а.

- **Manifest cache invalidation race с phase 3 installer.** Текущий mtime-based cache (max dir + files) может пропустить новый SKILL.md в течение 1-секундного окна (FS granularity). Phase 3 обязан вызывать `invalidate_manifest_cache()` (экспортирован в `bridge/skills.py`) после каждой установки + делать `os.utime(skills_dir, None)` для бамп mtime.

## Риск

**Средний.** Staleness индекса и concurrent writes → single-writer lock в CLI (`fcntl.flock` на `.index.db`).
