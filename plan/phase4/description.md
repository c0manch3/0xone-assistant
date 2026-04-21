# Phase 4 — Memory tool + skill (Obsidian vault + FTS5)

> **Precondition (updated 2026-04-21 per phase-3 Q-D1=c):** Phase 4 memory skill will NOT be SKILL.md+CLI. Instead, memory_search/memory_write/etc. будут `@tool`-decorator custom SDK tools в `src/assistant/tools_sdk/memory.py`, registered via `create_sdk_mcp_server(name="memory", tools=[...])` и wired в `ClaudeAgentOptions.mcp_servers={"memory": ...}`. Skills останутся prompt-expansions only (midomis / phase-2 ping pattern). SKILL.md может существовать как guidance document ("to save something to memory, use memory_write tool..."), но tool invocation — first-class через SDK custom tools. Precondition для start: researcher spike RQ1 в phase 3 step 4 passes (verify `@tool`+`setting_sources` coexist). См. `plan/phase3/description.md` "@tool groundwork".

**Цель:** долговременная память через Obsidian-совместимый vault + FTS5 CLI,
доступный модели только через скилл `memory`. Новая сессия должна помнить факты
из прошлой.

**Вход:** phase 3 (ClaudeBridge + Write sandbox + per-skill allowed-tools
sentinel + PostToolUse hot-reload).

**Выход:** CLI `tools/memory` + `skills/memory/SKILL.md`; E2E: "remember wife's
birthday is April 3" → запись → новая сессия → "когда у жены день рождения?"
→ правильный ответ.

## Задачи

1. `tools/memory/main.py` (stdlib-only, **без** `pyproject.toml` — консистентно
   с skill-installer phase 3). Subcommands:
   - `search QUERY [--area AREA] [--limit N]` — FTS5 MATCH → JSON hits.
   - `read PATH` — frontmatter + body + wikilink-targets.
   - `write PATH --title T [--tags a,b] [--area inbox] --body -` (body из stdin).
   - `list [--area AREA]` — JSON listing с frontmatter.
   - `delete PATH` — единичное удаление + снятие из индекса.
   - `reindex` — полный пересбор `.index.db` (disaster recovery).
2. Vault в `<data_dir>/vault/` (XDG `~/.local/share/0xone-assistant/vault/`).
   Areas (директории): `inbox/`, `projects/`, `people/`, `daily/` — создаются
   lazily на первом write.
3. FTS5 индекс в **отдельной** БД `<data_dir>/memory-index.db` (vault в git,
   индекс — нет). Схема `notes_fts(path, title, tags, area, body)` +
   `content_rowid` mirror-таблица `notes` для reconstruct при reindex. Tokenizer
   `porter unicode61 remove_diacritics 2`.
4. Frontmatter YAML: `title` mandatory; `tags`/`area`/`created`/`related`
   опциональны. Body — произвольный markdown, Obsidian wikilinks `[[link]]`
   сохраняются как есть (никакого HTML-render).
5. Atomic write: tmp-файл → `fsync` → `os.rename` (tempdir на той же FS в
   `<data_dir>/vault/.tmp/`). Single-writer `fcntl.flock` на `.index.db`
   сериализует concurrent `memory write`.
6. `skills/memory/SKILL.md` с frontmatter `allowed-tools: [Bash, Read]` и
   примерами диалогов ("сохрани факт", "что мы знаем про X", wikilink-graph).
7. System prompt обновить: "Долговременная память — авторитетный стор через
   skill `memory`. Проактивно пиши в `inbox/` факты из диалога; структурируй
   при накоплении." Убрать fallback-текст "если скилла нет — тогда никак".
8. **Закрыть phase-3 техдолг:** per-skill `allowed-tools` enforcement в
   `ClaudeBridge._build_options` — intersection с global baseline (безопасно
   только ограничивать). `memory` скилл получит именно `{Bash, Read}`,
   отсутствующий `Write/Edit/Grep/WebFetch` для memory-turn'ов.
9. **Решение A (history replay):** **synthetic summary** — расширить
   `history.py` так, чтобы synthetic note содержала первые 2000 символов
   последнего tool_result'а. Блокер phase 2 закрыт минимальным riskом.

## Критерии готовности

- E2E как в §Выход: через Telegram → `inbox/wife-birthday.md` создан → новая
  сессия (рестарт `Daemon`) → `search "жена"` возвращает hit → модель
  отвечает.
- `memory search` с кириллицей возвращает hits (`porter unicode61`).
- `memory write` на existing path без `--overwrite` → `exit 6`
  ("collision"; модель должна решить имя).
- Concurrent `memory write A.md` + `memory write B.md` из двух turn'ов — оба
  видны после commit'а; `.index.db` не corrupt.
- Per-skill enforcement: `memory` skill получает `{Bash, Read}` в
  `allowed_tools`; `skill-installer` — `{Bash}`; остальные, где `None` —
  baseline с warning.

## Явно НЕ в phase 4

- Scheduler-driven daily vault commit (phase 5/7).
- Yandex disk sync, Obsidian Live Sync — ops polish (phase 8).
- Embeddings / semantic search — FTS5 достаточно для single-user.
- Full markdown render / wikilink graph visualisation.
- `memory edit` partial-update CLI — модель делает `read → write --overwrite`.
- Per-turn memory-write auto-inject — явный вызов скилла моделью.

## Риск

**Средний.** Vault corruption при unclean kill митигируется atomic rename
(same FS гарантия); FTS5 drift vs file system — покрывается `reindex`;
concurrent writes — `fcntl.flock`. История replay'а — synthetic summary
(решение A) снимает blocker phase 2.

> **Warning:** `vault_dir` должен быть на локальной POSIX FS. iCloud Drive,
> Dropbox Smart Sync, SMB mounts могут сломать `fcntl.flock` semantics (silent
> no-op или degraded locking → corruption при concurrent writes). Документировать
> в README phase 4.
