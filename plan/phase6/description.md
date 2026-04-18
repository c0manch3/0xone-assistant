# Phase 6 — Subagent infrastructure (universal parallel background pool)

## Цель

Бот умеет делегировать любую долгую задачу (>10 s потенциально) в фоновый **subagent**, который исполняется в отдельной SDK-сессии параллельно с основным диалогом. Основная сессия никогда не блокируется: владелец продолжает писать сообщения, пока subagent'ы работают в фоне. По готовности результат проактивно доставляется в Telegram владельцу через `TelegramAdapter.send_text(OWNER_CHAT_ID, ...)` — тот же proactive-канал, что и у scheduler'а phase-5. Инфраструктура универсальна: будет использована phase-7 (media) + phase-8 (GitHub research) + будущие skills без новых каркасов.

## Вход

- Phase 5 завершён и shipped: `SchedulerLoop + SchedulerDispatcher + SchedulerStore`, shared `assistant.db` через `ConversationStore.lock`, per-chat asyncio.Lock в `ClaudeHandler`, `_bg_tasks`/`_spawn_bg` pattern, flock на `data/run/daemon.pid`, `IncomingMessage(origin, meta)`, `Daemon.start/stop` ordering (wave-2 B-W2-2: bg-drain → adapter.stop → DB.close → pid release), status-precondition SQL, `asyncio.shield` pattern на mark-retry, LRU dedup, clean-slate recovery.
- Phase-5 технический долг на входе: (1) `_memlib` sys.path consolidation отложен; (2) `HISTORY_MAX_SNIPPET_TOTAL_BYTES` cap отсутствует; (3) test-mypy 99 ошибок. Phase 6 делает свою дисциплину (не закрывает долг, но и не наращивает его — новый `tools/task/` использует тот же `sys.path.append` shim).
- `spikes/sdk_probe*.py` как эмпирическая база для любых новых SDK-вопросов.

## Выход — пользовательские сценарии (E2E)

1. **Delegation "напиши длинный пост".** Владелец: *«напиши пост в Telegram про историю OAuth 2.0, глубоко, 500+ слов»*. Модель (в основном turn'е): вызывает `python tools/task/main.py spawn --kind general --task "..."` → получает `{"job_id": 42}`. Отвечает владельцу: *«запустил subagent job 42, пришлю результат когда готово»*. Turn завершается за ~3 сек. Владелец сразу пишет: *«а пока напомни, что у меня в inbox»*. Второй turn отрабатывает штатно (memory → ответ). Через ~2 минуты subagent 42 завершается; `SubagentDispatcher` делает `adapter.send_text(OWNER_CHAT_ID, "<long post>\n\n---\n[job 42 completed in 2m14s, general, 42 turns, $0.08]")`.
2. **Pool concurrency.** Владелец подряд просит 3 задачи в отдельных turn'ах: транскрибация 1 ч аудио, генерация картинки, деградация 500-страничного PDF. Все три попадают в pending очередь, pool pick'ает их параллельно (cap=4 по умолчанию → все три стартуют), владелец продолжает чатиться. Результаты приходят по мере готовности.
3. **Queue-when-pool-full.** При cap=2 и 5 подряд spawn'ах: первые 2 идут в `running`, остальные 3 сидят в `pending`; по мере завершения pool автоматически подтягивает следующий pending (FIFO по `created_at`).
4. **Subagent failure.** Subagent падает: `ClaudeBridgeError`, timeout, Bash hook deny, или SDK oom. Dispatcher пишет `status='failed'` + `error_text`, proactively notify'ит: *«job 42 failed: <error snippet>. повторный запуск по запросу»*. Никакого automatic retry — владелец вручную решает respawn'ить или нет (урок из phase 5 retry-pipeline CRITICAL — retry без explicit спроса создаёт orphan'ы).
5. **Daemon restart с активными jobs.** Job в `status='running'` на момент SIGTERM → `Daemon.stop()` cancel'ит worker → shielded `mark_running_interrupted` → статус становится `interrupted`. На следующем boot'е: `SubagentStore.recover_orphans()` сканирует `status IN ('running','interrupted')` и revert'ит → `pending`. Pool забирает после startup.
6. **Cancellation.** Владелец: *«отмени job 42»* → модель вызывает `python tools/task/main.py cancel 42` → CLI выставляет `cancel_requested=1`; worker периодически проверяет (между блоками SDK-ответа) и выбрасывает `asyncio.CancelledError`, status → `cancelled`.
7. **Recursive subagent.** Subagent (kind=`general`) сам вызывает `tools/task/main.py spawn ...` → создаёт sub-subagent с `depth=parent_depth+1`. Hard cap depth=3: 4-й уровень → CLI exit 3 `depth_cap_exceeded`.

## Задачи (ordered)

1. **Spike S-6-1 (BLOCKER):** проверить — может ли `claude-agent-sdk` 0.1.59 запускать N конкурентных `query()` / `ClaudeSDKClient` в одном процессе без shared-state коллизий. OAuth сессия — one per daemon или per bridge? `spikes/phase6_s1_parallel_sdk.py`.
2. **Spike S-6-2:** поведение SDK при `asyncio.CancelledError` в середине query (cancel мид-стрима): корректный ли aclose? `spikes/phase6_s2_sdk_cancel.py`.
3. **DB migration v4** (`0004_subagent.sql`): таблица `subagent_jobs`, `PRAGMA user_version = 4`. Phase 7 (media) при необходимости возьмёт v5.
4. **`SubagentStore`** (aiosqlite + `ConversationStore.lock`) — CRUD + status-precondition transitions + `claim_pending` + `recover_orphans`.
5. **`SubagentPool`** (N worker tasks, each calling `store.claim_pending` в цикле; `_run_job` = новый `ClaudeBridge` + отдельный `conversation_id` + query stream).
6. **`SubagentDispatcher`** (observes terminal-state transitions и proactively notify'ит владельца через `adapter.send_text`).
7. **Config `SubagentSettings`** (pool_size=4, default_timeout=300, max_depth=3, max_pending=64, poll_interval=1s, per-type allowed-tools).
8. **CLI `tools/task/main.py`** (subcommands: `spawn`, `status`, `list`, `cancel`, `wait`).
9. **Skill `skills/task/SKILL.md`** (когда модель должна спавнить вместо inline-исполнения).
10. **Bash hook gate** для `tools/task/main.py` в `bridge/hooks.py` (`_validate_task_argv`, lesson из B-W2-5 dup-флаги).
11. **Daemon lifecycle** — spawn pool + dispatcher, `recover_orphans` до старта, stop-drain preserves phase-5 order.
12. **Built-in type registry** (`subagent/types.py`) — 3 default kinds (`general`, `worker`, `researcher`), system-prompt templates per kind.
13. **Тесты** (~30 файлов unit + integration, ~800 LOC).

## Критерии готовности

- `spawn --kind general --task "ping"` завершается < 1 s, возвращает `{"job_id": N, "status": "pending|running"}`.
- 5 параллельных spawn'ов при cap=2: первые 2 `running`, остальные 3 `pending`; после завершения первых — следующие забираются автоматически (p95 "pick up delay" < 3 s).
- Subagent получает отдельный `conversation_id` (новая строка в `conversations`); история не пересекается с main-chat'ом.
- `cancel <job_id>` на `running` job: status→`cancelled`, `CancelledError` пропагируется через `asyncio.shield(update-task)` (no `ProgrammingError: closed database` при shutdown).
- Daemon crash с `running` job: boot → `recover_orphans` → status becomes `pending` → pool picks up. Unique-idempotence: один раз delivered (no double-notify).
- Recursion cap: subagent depth=2 спавнит sub → OK (depth=3); depth=3 спавнит → CLI exit 3 `depth_cap_exceeded`.
- Dead-pending cap: 64 `pending` jobs → 65-й spawn → CLI exit 6 `quota_exhausted`.
- Subagent fail (`ClaudeBridgeError`) → `status='failed'`, proactive notify в Telegram, НО нет автоматического retry.
- Phase-5 scheduler + новый subagent pool работают параллельно, interleave'но; ни один не starve'ит второй (spike S-6-1 подтверждает N parallel SDK sessions ok).
- `Daemon.stop()` с 3 in-flight jobs: shielded updates drain'ятся до `conn.close()`; no `ProgrammingError`.
- 619 → ~1000 passing tests, mypy strict зелёный на новых модулях.

## Явно НЕ в phase 6 (defer list)

- **Специфические media/github skills** (transcribe, genimage, render-doc, gh-commit) — phase 7/8. Subagent infra готова, phase 7 просто регистрирует новые `kind`'ы.
- **UI / admin panel** для listing и cancellation jobs — phase 9 ops.
- **Prometheus метрики pool'а** (utilisation, queue depth) — phase 9.
- **Per-user quotas** — single-user бот.
- **Subagent→Telegram streaming** (progress updates mid-run) — не нужно; final-result only достаточно по contract'у. Можно добавить в phase 9.
- **Cross-daemon cancellation** (kill job из другого процесса) — вся инфра in-process, flock гарантирует single daemon.
- **Automatic retry** failed jobs — manual respawn (explicit lesson из phase 5).
- **Sweeper старых done/failed jobs** — пока оставляем forever (SQLite row cost ~1 KB × 1000 jobs = 1 MB); phase 9 если станет шумно.
- **`_memlib` consolidation + HISTORY snippet cap** (phase-5 techdebt #1, #2) — всё ещё на phase 9 (phase 6 специально узко scoped).
- **Skill-declared `subagent_kind` в SKILL.md frontmatter** — phase 7 когда media skills реально нужны.

## Зависимости

- **Phase 5:** `_spawn_bg`, `ConversationStore.lock`, `IncomingMessage`/handler contract, `TelegramAdapter.send_text` chunker, flock mutex, status-precondition SQL, `asyncio.shield` pattern, `Daemon.stop` ordering.
- **Phase 4:** `sys.path.append(<root>/src)` import shim для CLI.
- **Phase 3:** `_BASH_PROGRAMS` allowlist + `_validate_python_invocation` dispatch.
- **Phase 2:** ClaudeBridge (нужна ревизия: инстанциируется ли bridge безопасно параллельно).

## Риск + митигация

| Severity | Risk | Mitigation |
|---|---|---|
| 🔴 | SDK OAuth session — concurrency unknown | **Spike S-6-1 (BLOCKER)**: 3 parallel queries на одной сессии. Fallback: per-instance subprocess isolation если shared session race'ит. |
| 🔴 | `CancelledError` mid-query leaves zombie CLI | Spike S-6-2 + `ClaudeBridge.ask` уже делает aclose в finally (phase-2) — пере-проверить на cancel-injection. |
| 🟡 | Recursive spawning infinite loop (subagent спавнит subagent) | Hard depth cap = 3; CLI validates; DB column `depth`. |
| 🟡 | Pending queue → unbounded | `MAX_PENDING_JOBS=64` cap in CLI spawn (lesson phase-5 GAP #11). |
| 🟡 | Notify spam (10 jobs завершаются за 2 сек) | Throttle per-chat: min 500 ms между notify'ами (batch-friendly); не merge'им (каждый job — отдельное сообщение). |
| 🟡 | OAuth rate-limit при N parallel | Pool cap=4 по умолчанию, env `SUBAGENT_POOL_SIZE` для снижения. |
| 🟢 | Long jobs hold DB writer lock | Only around short CRUD операций; long SDK-time — отдельный `bridge.ask` не держит lock. |
| 🟢 | shutdown mid-job creates `interrupted` orphan | `recover_orphans` at boot переводит в `pending` (mirrors phase-5 clean_slate_sent). |
