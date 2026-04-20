# Phase 2 — ClaudeBridge + Skills plumbing

**Starts from:** commit `6f2d8d4` (phase 1 shipped — skeleton + Telegram echo + `ConversationStore` row-based schema 0001).

**Цель:** owner разговаривает с Claude Code SDK через бота; путь загрузки скилов проверен тривиальным smoke-скилом.

**Вход:** phase 1.

**Выход:** рабочий чат через SDK, история диалогов сохраняется, демо-скилл `skills/ping/SKILL.md` успешно вызывается моделью.

## Scope

Phase 2 **deletes `EchoHandler`**, introduces **`ClaudeHandler`**, wires Claude Agent SDK through **`ClaudeBridge`** (OAuth via the local `claude` CLI session — никаких API-ключей), and ships one smoke skill (`ping`). Параллельно phase 2 переносит `.env` в `~/.config/0xone-assistant/.env` и `data_dir` в `~/.local/share/0xone-assistant/` (XDG), применяет миграцию схемы 0002 (новая таблица `turns` + колонка `conversations.block_type`) и переходит с `parse_mode=HTML` на `parse_mode=None` (plain text).

**Security:** Assert no custom `.claude/settings*.json` at startup (warning log if present — SDK может переопределять наши hooks/env через эти файлы). История диалогов обрезается по **row-limit** (`CLAUDE_HISTORY_LIMIT=20`, configurable); token-budget стратегия отложена до phase 4+.

## Задачи

1. Портировать `src/assistant/bridge/claude.py` из midomis; добавить `setting_sources=["project"]`, сделать `cwd` конфигурируемым, path-guard только для файловых тулов (Read/Write/Edit/Glob/Grep). Bash/WebFetch разрешены, но проходят через `PreToolUse`-hooks (Bash: allowlist-first prefilter; WebFetch: SSRF-guard).
2. `src/assistant/state/conversations.py` — turn-based `load_recent`, новый Turn API (`start_turn`/`complete_turn`/`interrupt_turn`), миграция 0002.
3. `src/assistant/handlers/message.py` — **удалить `EchoHandler`**, заменить на `ClaudeHandler`: load history (complete turns only) → call bridge → append blocks → emit текст в адаптер; `try/finally` маркирует turn interrupted при сбое.
4. Bootstrap: идемпотентный симлинк `.claude/skills` → `../skills` при старте, идемпотентный `data_dir.mkdir(parents=True, exist_ok=True)`.
5. `skills/ping/SKILL.md` с frontmatter (`allowed-tools: [Bash]`); `tools/ping/main.py` (plain stdlib, без `pyproject.toml`) печатает `{"pong": true}`.
6. Smoke-тест: сообщение "use the ping skill" → модель запускает CLI → ответ содержит `pong`.
7. System prompt template `src/assistant/bridge/system_prompt.md`: identity + манифест скилов (собирается из frontmatter `skills/*/SKILL.md`; mtime-cached по `max(skills_dir.stat().st_mtime, *SKILL.md.st_mtime)`, инвалидация при atomic rename из skill-installer) + правило "долговременная память — только через skill `memory`".

## Критерии готовности

- Owner smoke: в Telegram `"use the ping skill"` → Claude вызывает `tools/ping/main.py` через Bash → ответ содержит `{"pong": true}`.
- В логах SDK (SystemMessage `init`) видно, что `SKILL.md` подхватывается (поле `skills` содержит `ping`).
- Миграция 0002 применена: `sqlite3 ~/.local/share/0xone-assistant/assistant.db 'PRAGMA user_version'` → `2`; `ls ~/.local/share/0xone-assistant/assistant.db` — файл существует.
- Сообщения > 4096 символов корректно разбиваются.
- Полный список (unit-тесты, security smoke, EchoHandler удалён, parse_mode=None) — в `detailed-plan.md` §Критерии готовности.

## Зависимости

Phase 1.

## Риск

**Средний** — ранее неопределённость контракта SDK (skills auto-discovery + permission callback). **Spike выполнен** (`spike-findings.md` R1–R5 на `claude-agent-sdk==0.1.59`): `setting_sources=["project"]` действительно подхватывает `.claude/skills/*/SKILL.md`, permission-слой — через `hooks={"PreToolUse":[...]}` (7 матчеров), не `can_use_tool`.

**Остаточные неизвестные** (см. `unverified-assumptions.md`): U1 — реплей `tool_use`/`tool_result` из истории (митигация: synthetic system-note); U2 — cross-session `ThinkingBlock` реплей (митигация: фильтр `block_type='thinking'`); U3 — symlink path для `.claude/skills` (митигация: manual smoke); U5 — regex form `HookMatcher.matcher` (митигация: 7 explicit matchers вместо 2). Все четыре верифицируются owner'ом в live-QA; fallback'и заложены в `implementation.md`.
