# Phase 6 — Subagent infrastructure (SDK-native, thin layer)

## Цель

Бот умеет делегировать любую долгую задачу (>10 s потенциально) в фоновый **subagent**, исполняющийся в отдельной SDK-сессии параллельно с основным диалогом. Основная сессия не блокируется. По готовности результат проактивно доставляется владельцу через `TelegramAdapter.send_text(OWNER_CHAT_ID, ...)` — тот же канал, что и у scheduler'а phase-5.

**Архитектурный поворот относительно wave-0 плана:** `claude-agent-sdk 0.1.59` ALREADY exposes полный native subagent API (`AgentDefinition`, `ClaudeAgentOptions.agents`, `SubagentStart/Stop` hook events, `TaskStartedMessage`/`TaskProgressMessage`/`TaskNotificationMessage` system messages, `TaskBudget`, `list_sessions/fork_session/get_session_messages`). Phase 6 = **тонкий слой над SDK-native primitives**, не roll-our-own pool. Принцип "проще = лучше" (явная установка владельца).

**Уровень неопределённости — высокий.** Мы рисуем план без предварительных эмпирических спайков по native subagent поведению. Каждое допущение помечено `[S-6-0 verifies]`; researcher'у поручен ОДИН blocker spike S-6-0, который параллельно проверит ~8 эмпирических вопросов до того как coder начнёт. Если spike обнаружит, что native API не подходит — план переписывается заново (контролируемый риск).

## Вход

- Phase 5 закрыт и shipped: `SchedulerLoop + SchedulerDispatcher + SchedulerStore`, shared `assistant.db` через `ConversationStore.lock`, per-chat asyncio.Lock в `ClaudeHandler`, `_bg_tasks`/`_spawn_bg`, flock на `data/run/daemon.pid`, `IncomingMessage(origin, meta)`, `Daemon.start/stop` ordering, status-precondition SQL, `asyncio.shield + _pending_updates` pattern, LRU dedup, clean-slate recovery.
- SDK 0.1.59 экспортирует: `AgentDefinition`, `ClaudeAgentOptions.agents`, `SubagentStartHookInput`, `SubagentStopHookInput`, `TaskStartedMessage`, `TaskProgressMessage`, `TaskNotificationMessage`, `TaskBudget`, `list_sessions / fork_session / get_session_messages / delete_session`. Поведение native подтверждено только грепом — НЕ runtime-spike'ом.
- Phase-5 техдолг (`_memlib` consolidation, `HISTORY_MAX_SNIPPET_TOTAL_BYTES` cap, 99 test-mypy errors) — на входе, не закрывается phase 6.

## Выход — пользовательские сценарии (E2E)

1. **Делегация "напиши длинный пост" — CLI path (non-blocking).**
   Владелец: *«напиши пост в Telegram про историю OAuth 2.0, глубоко,
   500+ слов»*. Модель в основном turn'е запускает
   `python tools/task/main.py spawn --kind general --task ...` через
   Bash → CLI возвращает `{"job_id": N, "status": "requested"}` сразу.
   Главный turn заканчивается за ~3 сек. Владелец продолжает чатиться.
   Daemon picker подхватывает `requested`-row'у, стартует SDK-subagent,
   Start hook patch'ит `sdk_agent_id`. Через ~2 мин SubagentStop hook
   вычитывает `agent_transcript_path`, экстрагирует финальный assistant
   text, форматирует с footer, дёргает `adapter.send_text(OWNER_CHAT_ID, ...)`.

   **Caveat для native Task tool.** S-6-0 Q1 / wave-2 Q1-BG FAIL: на
   SDK 0.1.59 + CLI 2.1.114 флаг `background=True` не асинхронизует
   — native Task tool остаётся синхронным RPC, и main turn блокируется
   до завершения subagent'а. Native Task ОК только для коротких (<30 s)
   делегаций, где блокировка приемлема. Для всего остального — CLI
   (scenario выше).
2. **Параллельные задачи.** Владелец последовательно просит 3 задачи в отдельных turn'ах. SDK сам запускает все три в фоне (concurrency cap проверим в S-6-0); каждая получает свой `agent_id` + `session_id`; результаты приходят по мере готовности.
3. **Spawn from scheduler.** Cron-trigger в 09:00 → scheduler-инициированный turn → модель решает делегировать в `researcher` subagent → SDK запускает; результат доставляется владельцу через тот же SubagentStop hook (callback chat_id хранится в нашем job ledger по `agent_id`).
4. **Subagent failure.** SDK эмитит `TaskNotificationMessage(status='failed')` → SubagentStop hook читает, помечает `subagent_jobs.status='failed'`, отправляет владельцу: *«job failed: <summary>»*. Никакого auto-retry (lesson phase-5 retry-pipeline CRITICAL).
5. **Daemon restart с активными jobs.** Job в SDK на момент SIGTERM. SDK будет реюзать или потеряет — TBD `[S-6-0]`. Наша ledger DB записывает `started`, `started_at`, `parent_chat_id`. На boot мы сканируем `subagent_jobs WHERE status='started' AND finished_at IS NULL` → помечаем `interrupted` и notify'им владельца. Re-spawn — manual.
6. **Cancel.** Владелец: *«отмени job 42»* → CLI `tools/task/main.py cancel 42` → выставляет `cancel_requested=1` в DB. SDK-native cancel API `[S-6-0]` (если есть — используем; если нет — flag-poll-based с задержкой ≤ ticking).
7. **Recursion.** Subagent сам вызывает Task tool → SDK создаёт sub-subagent (depth поведение `[S-6-0]`). Hard cap = 3 в нашем policy: SubagentStart hook отказывает на 4-м уровне.

## Задачи (ordered)

1. **Spike S-6-0 (BLOCKER, researcher).** Эмпирически проверить 8 вопросов про native API (см. detailed-plan §2). Если PASS — proceed; если FAIL на конкретном Q — задействовать соответствующий fallback.
2. **DB migration v4** (`0004_subagent.sql`): минимальная таблица `subagent_jobs` (~12 колонок vs old ~17), `PRAGMA user_version = 4`.
3. **`AgentDefinition` registry** (`subagent/definitions.py`): 3 named agents — `general / worker / researcher`.
4. **SDK-native hook handlers** (`subagent/hooks.py` или extension to `bridge/hooks.py`): `on_subagent_start` (insert ledger row), `on_subagent_stop` (read transcript, format, deliver via adapter).
5. **Ledger store** (`subagent/store.py`): `record_started`, `record_finished`, `set_cancel_requested`, `is_cancel_requested`, `recover_orphans`, `list_jobs`, `get`.
6. **CLI `tools/task/main.py`**: `spawn` (для scheduler/shell-init), `list`, `status`, `cancel`, `wait`. Гораздо тоньше чем в wave-0 — основная инстанциация subagent'ов идёт через native Task tool из основного turn'а.
7. **Skill `skills/task/SKILL.md`**: гайд "когда модель должна юзать Task tool".
8. **Bash hook gate `_validate_task_argv`** в `bridge/hooks.py`: subcmd whitelist + dup-flag rejection (lesson B-W2-5).
9. **`Daemon.start/stop` integration**: register subagent hooks через `ClaudeAgentOptions.hooks["SubagentStart"]/["SubagentStop"]`; orphan recovery; drain shielded notify tasks.
10. **Интеграция с phase-5**: scheduler-spawned subagent должен правильно адресовать callback. Ledger row хранит `callback_chat_id`, hook читает.
11. **Тесты** — ~600 LOC (~20 файлов unit + integration); все native-API contracts mock'аем под результаты S-6-0.

## Критерии готовности

- Spawn через CLI (`python tools/task/main.py spawn`) возвращает `{"job_id": N, "status": "requested"}` мгновенно; picker подхватывает row'у и дисптчит subagent'а async. Native Task tool — sync RPC, main turn блокируется до завершения subagent'а (S-6-0 Q1 FAIL; documented caveat — для коротких задач приемлемо).
- 3 параллельных subagent'а от 3 последовательных turn'ов работают взаимонезависимо; каждая имеет уникальный `session_id` и `agent_id`.
- SubagentStop hook получает callback с `agent_transcript_path` → читает финальный assistant text → доставляет в Telegram владельцу через `adapter.send_text` (chunked).
- Footer формат: `[job N, <status>, in Xs, kind=K(, cost=$Y)?]` (Q4 locked; fix-pack HIGH #2 / devil H-7: когда `cost_usd IS NULL` — сегмент `cost=` ОМИТ-ится, сегменты разделены `, `).
- Scheduler-spawned subagent: ledger row хранит `callback_chat_id=OWNER_CHAT_ID`, `spawned_by_kind='scheduler'`; SubagentStop hook доставляет результат корректно.
- `subagent_jobs.recover_orphans` на boot переводит unfinished `status='started'` в `'interrupted'` + notify владельца.
- CLI `cancel <id>`: выставляет `cancel_requested=1`; subagent видит флаг (механизм по результатам S-6-0) → завершается с `status='stopped'`.
- Recursion cap=3: SubagentStart hook на 4-м уровне deny'ит (через `additionalContext` или возврат deny — поведение `[S-6-0]`).
- Daemon stop с in-flight subagent'ами: shielded notify tasks drain'ятся до `conn.close`. Никакого `ProgrammingError: closed database`.
- 619 → ~900 passing tests; mypy strict зелёный на новых модулях.

## Явно НЕ в phase 6 (defer list)

- Специфические media/github skills — phase 7/8.
- UI / admin panel — phase 9.
- Prometheus метрики — phase 9.
- Per-user quotas — single-user бот.
- Subagent→Telegram streaming `TaskProgressMessage` (mid-run) — phase 9 (final-result-only достаточно).
- Cross-daemon cancellation — single daemon (flock).
- Auto-retry — manual respawn (lesson phase-5).
- SQLite sweeper старых jobs — forever (Q8 locked).
- `_memlib` consolidation + HISTORY snippet cap — всё ещё phase 9.
- Skill-declared `subagent_kind` в frontmatter — phase 7.

## Зависимости

- **Phase 5:** `_spawn_bg`, `ConversationStore.lock`, `IncomingMessage`/handler contract, `TelegramAdapter.send_text` chunker, flock mutex, status-precondition SQL, `asyncio.shield` + `_pending_updates` pattern, `Daemon.stop` ordering.
- **Phase 4:** `sys.path.append(<root>/src)` shim для CLI.
- **Phase 3:** `_BASH_PROGRAMS` allowlist + `_validate_python_invocation` dispatch.
- **Phase 2:** ClaudeBridge — phase 6 СОВСЕМ не модифицирует bridge (hooks attach в options).
- **SDK 0.1.59:** native `agents`, `SubagentStart/Stop` hook events, `Task*Message` system messages.

## Риск + митигация

| Severity | Risk | Mitigation |
|---|---|---|
| 🔴 | Native API behavior unknown — все 8 эмпирических допущений могут провалиться | **Spike S-6-0 BLOCKER**: один сводный спайк перед coder'ом; на каждое FAIL — задокументирован fallback (см. detailed-plan §2). |
| 🔴 | Cross-session subagent-from-scheduler unproven | S-6-0 Q6 эмпирически проверит, что SubagentStop hook fires когда subagent был spawn'нут из scheduler-bridge (другой ClaudeBridge instance). Fallback: централизованный hook factory с captured `daemon_state`. |
| 🟡 | Hook callback context — sync vs async, event loop access | S-6-0 Q3 проверит. Phase-3 hooks работают как async; ожидаем то же. |
| 🟡 | Transcript file race — когда `agent_transcript_path` готов | S-6-0 Q5: hook fires AFTER transcript flush'нут. Если нет — добавляем 200ms sleep + retry. |
| 🟡 | Cancellation API не документирован | S-6-0 Q7. Fallback: CLI выставляет flag в DB; subagent (через PreToolUse hook на каждый Bash call) проверяет flag → возвращает deny с reason "cancelled" → стек разматывается. |
| 🟢 | Recursion infinite loop | Hard cap depth=3 в SubagentStart hook (deny через `additionalContext` или return deny shape — TBD S-6-0 Q4). |
| 🟢 | Notify spam (10 jobs за 2 сек) | Throttle 500ms между notify-вызовами (preserve from wave-0). |
| 🟢 | Large result overflow Telegram | Существующий `split_for_telegram`. |
