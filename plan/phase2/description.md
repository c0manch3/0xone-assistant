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

- Owner smoke: в Telegram `"use the ping skill"` → Claude invokes Skill tool → SDK injects body → Claude responds with marker `PONG-FROM-SKILL-OK` + Russian confirmation sentence.
- В логах SDK (SystemMessage `init`) видно, что `SKILL.md` подхватывается (поле `skills` содержит `ping`).
- Миграция 0002 применена: `sqlite3 ~/.local/share/0xone-assistant/assistant.db 'PRAGMA user_version'` → `2`; `ls ~/.local/share/0xone-assistant/assistant.db` — файл существует.
- Сообщения > 4096 символов корректно разбиваются.
- Полный список (unit-тесты, security smoke, EchoHandler удалён, parse_mode=None) — в `detailed-plan.md` §Критерии готовности.

## Зависимости

Phase 1.

## Риск

**Средний** — ранее неопределённость контракта SDK (skills auto-discovery + permission callback). **Spike выполнен** (`spike-findings.md` R1–R5 на `claude-agent-sdk==0.1.59`): `setting_sources=["project"]` действительно подхватывает `.claude/skills/*/SKILL.md`, permission-слой — через `hooks={"PreToolUse":[...]}` (7 матчеров), не `can_use_tool`.

**Остаточные неизвестные** (см. `unverified-assumptions.md`): U1 — реплей `tool_use`/`tool_result` из истории (митигация: synthetic system-note); U2 — cross-session `ThinkingBlock` реплей (митигация: фильтр `block_type='thinking'`); U3 — symlink path для `.claude/skills` (митигация: manual smoke); U5 — regex form `HookMatcher.matcher` (митигация: 7 explicit matchers вместо 2). Все четыре верифицируются owner'ом в live-QA; fallback'и заложены в `implementation.md`.

## Known Limitations (Phase 2)

**Bash-from-skill-body tool execution is NOT validated in phase 2 smoke.**

Phase 2 ping skill uses text-generation pattern (marker response). It validates SDK skill discovery, `Skill` tool invocation, skill body delivery, and Claude's response-generation path — but NOT model compliance with imperative body instructions like "Run `python tools/X` via Bash".

Opus 4.7 has known issues following imperative tool-invocation instructions from skill bodies (GitHub [#39851](https://github.com/anthropics/claude-code/issues/39851), [#41510](https://github.com/anthropics/claude-code/issues/41510)). Anthropic closes this as a "system_prompt / architecture question" ([SDK #544](https://github.com/anthropics/claude-agent-sdk-python/issues/544)) — no SDK fix forthcoming.

**Impact on later phases:**
- Phase 3 (skill-creator/installer) must deliver **PostToolUse tool-invocation enforcement** as a blocker for phase 4.
- Phase 4 (memory skill) requires Bash-from-skill-body execution (FTS5 SQLite query). Either phase 3 enforcement ships first, OR phase 4 refactors memory to use `@tool`-decorator (in-process SDK custom tool) or MCP server pattern instead of SKILL.md + CLI.

See `plan/phase2/known-debt.md` for full debt tracking.
