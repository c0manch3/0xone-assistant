# Phase 6 — Summary

Документ подводит итоги завершённой фазы 6 проекта `0xone-assistant`
(SDK-native subagent infrastructure). Источники: `plan/phase6/{description,
detailed-plan,implementation,spike-findings}.md`, исходники
`src/assistant/subagent/` + `tools/task/` + `skills/task/`, **14 коммитов**
`493eb04 → 558d94e` поверх phase-5 HEAD `5cf36e5`, **765 passed** (+145 vs
phase 5's 620), lint + mypy strict зелёные на новых модулях.

## 1. Что было сделано

- **Делегация долгих задач в фоновый SDK-subagent E2E.** Владелец: *«напиши
  длинный пост про OAuth 2.0»*. Модель в основном turn'е запускает
  `python tools/task/main.py spawn --kind general --task ...` через Bash →
  CLI пишет row в `subagent_jobs` (status=`requested`) и возвращает
  `{"job_id": N, "status": "requested"}` мгновенно. Главный turn
  заканчивается за ~3 с. Picker bridge подхватывает row'у, дисптчит
  SDK-subagent; Start hook патчит `sdk_agent_id`; через N минут
  SubagentStop hook читает `last_assistant_message` (runtime-поле SDK) +
  JSONL fallback, форматирует с footer и отправляет владельцу через
  `adapter.send_text(OWNER_CHAT_ID, ...)`.
- **AgentDefinition registry** (`src/assistant/subagent/definitions.py`) —
  3 named agents: `general` (full tool access), `worker` (CLI-focused
  Bash/Read/Write/Edit/Grep/Glob), `researcher` (read-only Read/Grep/Glob/
  WebFetch/WebSearch). Все три: `model="inherit"`, `background=True`
  (forward-compat; эффекта на main-turn wall не имеет — Q1 FAIL), `tools`
  list omits `"Task"` → рекурсия невозможна без явной пропагации
  (depth-cap=1 бесплатно из spike Q4).
- **DB migration v4** (`src/assistant/state/migrations/0004_subagent.sql`):
  таблица `subagent_jobs` (~15 колонок), `sdk_agent_id TEXT NULL` +
  partial unique index (`WHERE sdk_agent_id IS NOT NULL`) — `requested`
  row'ы вставляются с NULL до Start hook'а, полный UNIQUE constraint был
  бы невозможен. Columns: `id, kind, task, parent_chat_id, callback_chat_id,
  spawned_by_kind, status, sdk_agent_id, session_id, agent_transcript_path,
  cancel_requested, requested_at, started_at, finished_at, cost_usd,
  error_message, last_assistant_message`. Status FSM:
  `requested → started → (finished|failed|stopped|interrupted)`.
- **SubagentStart/Stop hooks** (`src/assistant/subagent/hooks.py:94`,
  `hooks.py:182`). Start: читает ContextVar `_pending_request_id`
  (установленный picker'ом до `query()`), UPDATE'ит `subagent_jobs.row`
  по pending id → патчит `sdk_agent_id = agent_id`. Stop: вычитывает
  `raw["last_assistant_message"]` (primary) или `agent_transcript_path`
  JSONL (fallback через `asyncio.to_thread` — pitfall #12), форматирует
  footer `[job N, <status>, in Xs, kind=K(, cost=$Y)?]` (NULL cost
  омит-ится), chunk'ит через существующий splitter, шлёт через
  shielded notify task → drain в `Daemon.stop()`.
- **Cancel gate через PreToolUse hook**
  (`src/assistant/subagent/hooks.py` — `on_pretool_cancel_check`). CLI
  `tools/task/main.py cancel <id>` выставляет `cancel_requested=1` в
  ledger. Когда subagent делает tool call, PreToolUse hook с
  `agent_id`-контекстом (S-2 PASS: subagent tool calls traverse
  parent's phase-3 sandbox с заполненным `agent_id`) проверяет флаг →
  возвращает deny с reason "cancelled" → стек разматывается. Known
  limitation: subagent без tool calls флаг не видит (Q7 corner, принято).
- **SubagentRequestPicker** (`src/assistant/subagent/picker.py`) —
  **выделенный picker bridge** с собственным `asyncio.Semaphore` на
  ~4 concurrent subagents, отдельный от user-chat bridge'а. Poller
  каждые 2 s читает `subagent_jobs WHERE status='requested'`, устанавливает
  ContextVar, вызывает `bridge.run_turn(...)`, ждёт Start+Stop. Без
  дедикейтид bridge'а user-chat turn'ы конкурировали бы с subagent-ами
  за семафор.
- **ClaudeBridge `extra_hooks` + `agents` + `baseline_extras`**
  (`src/assistant/bridge/claude.py`). Task tool добавляется в
  `_GLOBAL_BASELINE` allowed_tools ТОЛЬКО когда registry non-empty
  (B-W2-8). Hooks attach'атся через merge `ClaudeAgentOptions.hooks`, не
  переписывая phase-3 валидаторы.
- **Daemon integration** (`src/assistant/main.py`): orphan recovery на
  `Daemon.start()` — `SubagentStore.recover_orphans()` помечает
  зависшие `started` row'ы как `interrupted` + старые `requested` как
  `stale`; split-notify шлёт владельцу один сводный chunk про оба класса
  (interrupted + stale). `Daemon.stop()`: останавливает picker, дрейнит
  picker's in-flight dispatches (fix-pack B / devil C-3), дрейнит
  shielded notify tasks, потом закрывает DB — unit-тест
  `test_main_subagent_recovery.py` на `recover_orphans` runtime path
  (fix-pack C / CR I-2, anti-phase-5-repeat).
- **CLI `tools/task/main.py`** (387 LOC, stdlib-only) + skill
  `skills/task/SKILL.md` (172 LOC). Subcommands `spawn / list / status /
  cancel / wait`. Bash hook gate `_validate_task_argv` в
  `bridge/hooks.py` (subcmd whitelist + dup-flag rejection, lesson
  B-W2-5). Async UX документирован как primary path в description +
  detailed-plan + skill (fix-pack A / devil C-1/C-2 — контракт-дрифт).
  Native Task tool остался как sync RPC для коротких (<30 s) делегаций.
- **SDK version pin на startup** — если
  `claude_agent_sdk.__version__ != "0.1.59"`, Daemon логирует громкое
  warning и Stop hook сваливается на JSONL fallback (
  `last_assistant_message` — runtime-only поле, НЕ в TypedDict
  `SubagentStopHookInput`).

## 2. Ключевые архитектурные решения

1. **SDK-native vs roll-our-own pool** (plan wave-1 rewrite). Devil wave-1
   5-секундным grep'ом на `from claude_agent_sdk import …` обнаружил, что
   SDK 0.1.59 уже экспортирует `AgentDefinition`, `ClaudeAgentOptions.agents`,
   `SubagentStart/Stop` hook events, `list_sessions / fork_session /
   get_session_messages`. Wave-0 plan'овал 3300 LOC roll-our-own subagent
   pool; wave-1 rewrite — ~1300 LOC тонкий слой. **Экономия ~2000 LOC.**
   (Actual shipped — 2100 source + 1400 tests = 3500 LOC из-за тестов и
   fix-pack'ов, но source-layer 40% меньше.)
2. **CLI picker стал primary async path** (Q1 FAIL). S-6-0 Q1 re-run с
   explicit `background=True/False` подтвердил: native Task tool
   блокирует main turn до завершения child'а в обоих режимах. Это
   killer-факт для "типа асинхронного" native path'а. CLI spawn через
   daemon-pickup (Q13) — единственный способ, который main turn реально
   отпускает за ~3 с. Description + skill переписаны после post-ship
   devil catch (C-1/C-2).
3. **ContextVar для picker→Start-hook correlation** (S-1 PASS).
   Альтернатива — синтетический prompt marker ("prompt: [req-42] ...") —
   хрупкая и утекает в transcript. `asyncio.ContextVar`
   (`src/assistant/subagent/context.py:8`) прокидывается через
   SDK-dispatched hook callback без костылей. S-1 spike эмпирически
   подтвердил на двух последовательных query-запусках.
4. **Dedicated picker bridge с отдельным Semaphore.** Без него user-chat
   turn'ы конкурировали бы за 4-slot пул с subagent dispatch'ами — при
   4 запущенных subagent'ах владелец не мог бы отправить сообщение.
   Отдельный bridge instance + shared hooks factory (Q6 PASS — hooks
   fire across multiple `ClaudeAgentOptions` instances).
5. **`sdk_agent_id` nullable + partial unique index** (B-W2-3). На момент
   INSERT row'а мы ещё не знаем `agent_id` (он присваивается SDK'ом в
   Start hook). Полный UNIQUE constraint отклонял бы multiple pending
   row'ы с NULL. Partial index `WHERE sdk_agent_id IS NOT NULL`
   обеспечивает uniqueness только для заполненных.
6. **Task tool в `baseline_extras` — conditionally** (B-W2-8). Если
   `agents={}` пустой, Task tool НЕ добавляется в allowed_tools. Иначе
   модель видела бы tool без definitions и SDK бы падал. Логика:
   `claude.py` собирает `agents` dict, если non-empty — добавляет Task
   в baseline merge.
7. **Subagent Bash → parent PreToolUse** (S-2 PASS — SECURITY). 5 из 5
   subagent-emitted Bash calls денаты parent-side phase-3 валидаторами
   (`_validate_bash_argv`, `_validate_ls_invocation` и т. д.). `agent_id`
   заполнен в HookContext при subagent-origin fire (SDK
   `_SubagentContextMixin.types.py:246-262`). **Никакой регрессии
   безопасности** — phase-3 sandbox работает прозрачно. Subagent НЕ
   обходит allowlist, НЕ инжектит shell-метасимволы, NE читает outside
   project_root. Без этого spike'а phase-6 был бы security-blocker.
8. **`last_assistant_message` runtime-only + SDK pin + JSONL fallback**
   (Q5 PARTIAL→PASS). Поле есть на runtime (Q1 raw — 495 chars), но НЕ
   в `SubagentStopHookInput` TypedDict (grep `.venv/.../claude_agent_sdk/`
   вернул 0 matches). Хрупко across upgrade'ов. Startup SDK version pin
   на `0.1.59` + JSONL fallback reader (через `asyncio.to_thread`
   чтобы не блокировать event loop — CR I-6 / pitfall #12) контейнят
   фрагильность.

## 3. Что поймали пайплайн-wave'ы

Одиннадцать стадий (plan wave-0 → Q&A → devil wave-1 → plan wave-1
rewrite → researcher S-6-0 → devil wave-2 → researcher fix-pack v2 →
coder → parallel CR+devil final → coder fix-pack → summary). Каждая
волна поймала свой класс ошибок.

**Devil wave-1 на plan wave-0** — нашёл, что SDK уже экспортирует
`AgentDefinition`, `SubagentStart/Stop` hooks, `list_sessions /
fork_session`. Wave-0 проектировал 3300 LOC roll-our-own pool; 5-секундный
grep изменил архитектуру → 60% LOC reduction. **Без этой волны мы бы
shipнули неоправданно толстый layer.**

**Researcher S-6-0 wave-1** — 8 эмпирических вопросов через один сводный
спайк к реальному OAuth-authenticated CLI. Q1 FAIL (main turn blocks)
переломил весь deployment strategy — описание с "~3 сек turn" было
aspirational; CLI picker стал mandatory. Q5 PARTIAL (JSONL 0 blocks at
hook-fire time) нашёл скрытую race.

**Devil wave-2 на spike-findings** — **поймал фабрикации**. Q5 в raw
report был "PARTIAL" (JSONL читался пустым), но researcher в prose drift'ил
в "PASS" без читающего notation'а про hook-input field. Devil wave-2
вычитал raw JSON (`q1_q2_q3_raw.hook_observations.stop_events[0].
last_assistant_message_from_hook`) и увидел 495 chars реального текста.
Вынес verdict **PARTIAL_FILESYSTEM_PASS_HOOK_FIELD** с honest caveat
про отсутствие поля в TypedDict. Также: 8 blockers + 10 gaps.

**Researcher fix-pack v2** — re-ran Q1 с explicit `background=True`
(previous v1 silently omitted flag), добавил S-1 ContextVar spike (B-W2-4)
+ S-2 subagent sandbox spike (B-W2-5 — security). Оба PASS'нули.
Верифицировали, что plan'ируемые примитивы реально работают.

**Code-reviewer + devils-advocate final на shipped code (parallel)** —
0 critical от CR, но 3 critical от devil + 6 items от CR:
- **I-2** (CR): `Daemon.start` recovery path имеет unit-test стора, но
  RUNTIME-пути `recover_orphans` на startup нет — **anti-phase-5-repeat**.
  Phase 5 словил ровно эту же ошибку на `pending_retry` re-enqueue.
  Починено fix-pack C.
- **I-6** (CR): JSONL fallback читал файл synchronously в async hook —
  **violation pitfall #12**. Нулевой throughput при 200ms+ reads.
  Починено fix-pack D: `asyncio.to_thread`.
- **C-1/C-2** (devil): description.md обещал "~3 сек async UX"; skill +
  detailed-plan default path был native Task tool (блокирующий). **Spec-code
  drift.** Починено fix-pack A: async UX через CLI задекларирован как
  primary во всех трёх документах.
- **C-3 / I-1** (оба): picker dispatch tasks orphaned on `Daemon.stop()`.
  Никто не ждал их drain'а → `ProgrammingError: closed database` на
  shutdown mid-dispatch. Починено fix-pack B.
- **HIGH #1/#2** (devil): cancelled `requested` row'ы оставались в DB
  (засоряли `list`); footer включал `cost=$None` при NULL cost. Починено
  fix-pack E.
- **Fix-pack F**: CLI cancel DRY + preserve CLI attribution в
  `cancel_requested` audit + `is_cancel_requested` lock (гонки на
  parallel reads).

**Урок**. Review waves catch different classes — каждая необходима:
- **Plan wave-0** — набросок.
- **Devil wave-1** — assumptions-level (grep SDK перед тем, как
  роллить свой пул).
- **Researcher** — эмпирические factchecks (Q1 FAIL пересобрал архитектуру).
- **Devil wave-2** — researcher-level fabrications (PARTIAL→PASS drift
  на prose layer). **Lesson: всегда cross-check raw JSON против prose
  summary.**
- **Devil post-ship на код** — spec-code alignment (описание ≠ код).
- **CR post-ship на код** — anti-pattern enforcement (phase-5
  post-ship повторения — unit-stub без runtime-теста; sync I/O в async
  context).

**Q1 FAIL** — pivotal empirical finding фазы. Без него phase-6 ship'нул бы
с unusable default UX (native Task блокирует turn, user ждёт 2 минуты
перед reply).

## 4. Что НЕ делали в phase 6 (scope discipline)

- **media / github skills.** Phase 7/8 (`tools/gh` + ежедневный vault
  git-commit из phase-5 defer-list'а тоже).
- **Admin panel, Prometheus metrics, retention sweeper для старых jobs.**
  Phase 9 (Q8 locked: forever retention в phase 6).
- **Subagent→Telegram progress streaming mid-run.** Final-result-only
  достаточно на phase 6; `TaskProgressMessage` в итераторе есть, но не
  доставляется. Phase 9.
- **Cross-daemon cancellation.** Single daemon через flock mutex —
  остаётся из phase 5.
- **Auto-retry on subagent failure.** Manual respawn. Lesson phase-5:
  retry pipeline — это фича, которая ломает в подслоях (pending_retry
  orphan CRITICAL в phase 5). Не добавляем без явного use-case'а.
- **`_memlib` consolidation + relative imports.** Phase-4 tech-debt, по-прежнему
  отложен (phase-5 summary #1). CLI `tools/task/main.py` импортирует
  через тот же `sys.path.insert` pattern — debt масштабируется.
- **`HISTORY_MAX_SNIPPET_TOTAL_BYTES` cap.** Phase-4 tech-debt, всё ещё phase 9.
- **`test_subagent_e2e.py` RUN_SDK_INT=1 gated.** Тот же pattern, что у
  phase-5 real-OAuth тестов — accepted (gate `RUN_SDK_INT=1` требует
  OAuth creds; в CI по default'у не гоняем).
- **`skill`-declared `subagent_kind` в frontmatter.** Phase 7 (defer
  из description wave-0).
- **N=8+ concurrency probe.** S-6-0 Q8 PASS at N=4; phase-6 traffic
  (scheduler + 2-3 user) comfortably ниже. SDK throttle не исследован.

## 5. Технический долг (для phase 7+)

| # | Pri | Замечание | Файл | Фаза закрытия |
|---|-----|-----------|------|---------------|
| 1 | 🟡 | `cost_usd` не аккаунтится для child turn'ов — в ledger пишется только главный SDK cost, не sub-session (GAP #11 deferred) | `src/assistant/subagent/hooks.py:182` (on_subagent_stop) | Phase 7+ |
| 2 | 🟡 | Starvation test — tautological (проверяет poll, но не реальное изголодание под нагрузкой); H-2 deferred | `tests/test_subagent_picker.py` | Phase 7 |
| 3 | 🟡 | Recovery false-positive window на 30 s boundary — `recover_orphans(stale_requested_after_s=3600)` возвращает stale row'ы, которые могли стартовать в последние 30 s | `src/assistant/subagent/store.py:454` | Phase 7/8 если шумно |
| 4 | 🟡 | Throttle drift: `_NOTIFY_MIN_INTERVAL_S = 0.5` — hardcoded; под burst'ом 10 jobs за 2 с adjacent message'ы могут смерж'иться на Telegram side (H-8) | `src/assistant/subagent/hooks.py` | Phase 7+ если видно |
| 5 | 🟢 | `RUN_SDK_INT=1` e2e test не гоняется в CI по default'у (H-1) | `tests/test_subagent_e2e.py` | Phase 9 ops |
| 6 | 🟢 | Все LOW/Nit items из final review | везде | по мере касаний |
| 7 | 🟢 | Phase-5 carryover: `_memlib` rename + HISTORY cap + 99 test-only mypy errors | `tools/{task,memory,schedule}/main.py:1-20`, `src/assistant/bridge/history.py`, `tests/` | Phase 9 |

## 6. Метрики

**Tests:** 620 → **765** (+145). 19 новых тест-файлов: `test_subagent_*`,
`test_task_*`, `test_main_subagent_recovery.py`.

**Commits phase 6:** **14** (`493eb04..558d94e`): 8 initial main-pass + 6
fix-pack (post-review). Дифф `src/` + `tools/` + `skills/` +
`tests/`: ~**+6361 / −28** в 35 файлах.

**LOC исходников phase 6:**
- `src/assistant/subagent/` — **NEW 1414 LOC** в 7 `.py`:
  `store.py` 577, `hooks.py` 396, `picker.py` 193, `definitions.py` 118,
  `format.py` 87, `context.py` 23, `__init__.py` 20.
- `src/assistant/state/migrations/0004_subagent.sql` — миграция v4.
- `tools/task/main.py` — **387 LOC** (stdlib-only CLI).
- `skills/task/SKILL.md` — **172 LOC**.
- Edits в `src/assistant/`: `main.py` (Daemon integration + orphan recovery),
  `config.py` (SubagentSettings), `bridge/claude.py` (extra_hooks +
  baseline_extras), `bridge/hooks.py` (`_validate_task_argv` + PreToolUse
  cancel gate), `bridge/system_prompt.md`.

**LOC планирования phase 6:** `description.md` 102, `detailed-plan.md`
733, `implementation.md` 1936, `spike-findings.md` 363 — **3134 LOC** plan.

**Spike artifacts:** 3 скрипта (`phase6_s0_native_subagent.py`,
`phase6_s1_contextvar_hook.py`, `phase6_s2_subagent_sandbox.py`) + 3 JSON
reports + `phase6_s0_findings.md`.

**Plan vs actual LOC:** ~3300 projected (wave-0) / 1300 projected (wave-1
rewrite) vs **~2100 source + ~1400 tests = ~3500 shipped.** Wave-1 rewrite
недо-projected-ил тесты; source-layer попал в цель.

**Pipeline waves:** 11 stages, 2 plan revisions, 3 spike rounds (S-0 v1 +
v2 fix-pack с S-1 + S-2), 12 Q&A (N=8 pool override на Q1).

**CI-gates:** `uv sync` OK, `just lint` зелёный (ruff + format + mypy src
strict), `just test` — 765 passed в ~16 s.

**Calendar:** ~1 день (pipeline, 2026-04-17).

## 7. Уроки

1. **Всегда grep SDK exports перед тем, как планировать roll-our-own.**
   Wave-0 plan projected 3300 LOC subagent pool. Wave-1 devil 5-секундным
   grep'ом по `from claude_agent_sdk` нашёл полный native API. **60% LOC
   reduction** + документированное поведение от SDK-авторов вместо наших
   догадок. Сохраняем как правило: перед любым новым слоем — `grep -r
   "from X import" .venv/`.
2. **Researchers fabricate under pressure — cross-check raw artifacts.**
   Devil wave-2 поймал Q5 verdict drift: raw report = PARTIAL, prose
   summary = PASS. Researcher пропустил nuance про "PASS только через
   runtime hook field, NOT в TypedDict". Lesson: в следующих фазах
   future review waves ДОЛЖНЫ иметь явный step "verify prose verdict
   against raw JSON evidence". Trust raw reports, не prose summaries.
3. **Empirical facts override plan optimism.** Q1 `background=True` FAIL
   (main turn блокируется) переписал весь deployment strategy phase-6.
   Description wave-1 обещал "~3 сек turn"; spike показал 19 s для
   trivial case'а, ~2 мин для реального. CLI picker из nice-to-have стал
   mandatory primary path. **Без spike'а phase 6 shipнул бы с unusable
   default UX.** Инвестиция в S-6-0 (~4 часа) окупилась сразу.
4. **Spec-code drift compounds after ship.** Description.md обещал
   async UX; default code path был blocking native Task. Post-ship devil
   C-1/C-2 поймал. Lesson: **spec-code alignment — review criterion, не
   "does it compile"**. В будущих phase'ах включаем явный step: diff'им
   description promises против shipped code default paths.
5. **Security check is cheap if you design for it.** S-2 spike (subagent
   Bash → parent PreToolUse) ушёл 1 час; подтвердил **zero security
   regression** — 5/5 subagent Bash calls денаты parent-side validators
   с `agent_id` заполненным. Без S-2 phase-6 был бы blocker: subagent
   мог бы обойти phase-3 sandbox. Adversarial security-check —
   мандатный explicit spike в будущих фазах, не "assumed safe".

---

Phase 6 закрыт. SDK-native subagent infrastructure работает E2E: владелец
делегирует долгую задачу через `tools/task/main.py spawn`, main turn
заканчивается за ~3 с, picker bridge дисптчит SDK-subagent асинхронно,
SubagentStop hook доставляет результат через Telegram с footer'ом.
Scheduler-spawned subagent'ы работают через тот же hook (callback
`OWNER_CHAT_ID` в ledger). Cancel через flag-poll PreToolUse gate.
Recursion cap=1 бесплатно (tools list без Task). Subagent Bash traverses
parent phase-3 sandbox — **нулевой security regression.** Daemon restart
с активными jobs: `recover_orphans` помечает `started→interrupted` +
`requested→stale`, split notify'им владельцу. 14 коммитов, 765 тестов
зелёные. Phase 7 (media / github skills) разблокирован; унаследованный
phase-5 техдолг (`_memlib` + HISTORY cap + test-mypy) всё ещё на phase 9.
