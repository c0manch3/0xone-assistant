# Phase 2 — ClaudeBridge + Skills plumbing

**Цель:** owner разговаривает с Claude Code SDK через бота; путь загрузки скилов проверен тривиальным smoke-скилом.

**Вход:** phase 1.

**Выход:** рабочий чат через SDK, история диалогов сохраняется, демо-скилл `skills/ping/SKILL.md` успешно вызывается моделью.

## Задачи

1. Портировать `src/bridge/claude.py` из midomis; добавить `setting_sources=["project"]`, сделать `cwd` конфигурируемым, оставить path-guard только для файловых тулов (Bash и WebFetch разрешены без ограничений).
2. `src/state/conversations.py` — порт без изменений.
3. `src/handlers/message.py` — load conversation → call bridge → append → reply; нарезка длинных сообщений.
4. Bootstrap: идемпотентный симлинк `.claude/skills` → `../skills` при старте.
5. `skills/ping/SKILL.md` с frontmatter (`allowed-tools: Bash`); `tools/ping/main.py` печатает `{"pong": true}`.
6. Smoke-тест: сообщение "use the ping skill" → модель запускает CLI → ответ содержит `pong`.
7. System prompt template `src/bridge/system_prompt.md`: identity + манифест скилов (автосборка из frontmatter `skills/*/SKILL.md` — **пересобирается на каждый запрос**, не кэшируется, чтобы новые скилы подхватывались сразу) + правило "долговременная память — только через skill `memory`".

## Критерии готовности

- Smoke-тест зелёный.
- В логах SDK видно, что `SKILL.md` подхватывается.
- Сообщения > 4096 символов корректно разбиваются.

## Зависимости

Phase 1.

## Риск

**Средний** — неопределённость контракта автозагрузки скилов в Claude Agent SDK.

**Митигация:** если `setting_sources=["project"]` не подхватывает `.claude/skills/` — fallback: инжектить описания скилов прямо в system prompt. Сделать 30-минутный spike в начале фазы.
