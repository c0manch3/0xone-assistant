# Phase 6 — детальный план (Subagent: SDK-native thin layer)

Phase 6 вводит универсальный фоновый subagent pool как **тонкий слой над SDK-native primitives**. SDK сам управляет spawn'ом, lifecycle'ом, transcript persistence, OAuth, конкуррентностью. Мы только: (а) регистрируем `AgentDefinition` через `ClaudeAgentOptions.agents`, (б) цепляем `SubagentStart`/`SubagentStop` hooks для notify-канала, (в) держим тонкий ledger DB для cross-session correlation (scheduler→subagent→callback) и audit, (г) предоставляем CLI для explicit spawn'а из non-main контекстов.

---

## 0. Mental model — что делает SDK, что делаем мы

| Layer | Кто отвечает |
|---|---|
| Spawn subagent (новый process / async task внутри SDK) | **SDK** (через `agents={...}` registry + native Task tool) `[S-6-0 verifies]` |
| Lifecycle: start → progress → stop | **SDK** |
| Transcript persistence на диск (`agent_transcript_path`) | **SDK** |
| OAuth/auth sharing между main и subagent | **SDK** |
| Concurrency control / max parallel agents | **SDK** (cap may exist; `[S-6-0 Q8]`) |
| Session forking | **SDK** (`fork_session`) — phase 6 не использует |
| Subagent typed templates (system prompt, allowed tools, model, max_turns) | **SDK** через `AgentDefinition` |
| Per-kind tool narrowing | **SDK** через `AgentDefinition.tools` |
| Owner notify когда subagent finishes | **МЫ** — через `SubagentStop` hook + `adapter.send_text` |
| Cross-session correlation (scheduler-spawn → callback chat) | **МЫ** — DB ledger по `agent_id` |
| Recursion cap (depth ≤ 3) | **МЫ** — `SubagentStart` hook возвращает deny с `additionalContext` `[S-6-0 Q4]` |
| Cancellation (если SDK не предоставляет) | **МЫ** — flag в DB, PreToolUse hook poll'ит и deny'ит `[S-6-0 Q7]` |
| Orphan recovery после daemon restart | **МЫ** — boot scan `subagent_jobs WHERE status='started'` |
| Audit, listing, status reporting | **МЫ** — ledger DB |

---

## 1. Phase-5 invariants preserved

1. Single-daemon flock на `daemon.pid`.
2. `ConversationStore.lock` shared с phase-5 scheduler — никаких отдельных aiosqlite connections.
3. `Daemon.stop()` ordering: signal → drain bg-tasks → drain shielded updates → adapter.stop → conn.close → pid release. Phase-6 добавляет drain собственных shielded notify tasks между шагами 2.5 и 3.
4. Status-precondition SQL на каждую mutation.
5. `asyncio.shield` + `_pending_updates` set pattern для notify-tasks (lesson phase-5 HIGH #5).

---

## 2. Spike S-6-0 (BLOCKER) — эмпирическая валидация native API

**Артефакт:** `spikes/phase6_s0_native_subagent.py` + `spikes/phase6_s0_findings.md`. Один сводный спайк, до coder.

### 2.1. Эмпирические вопросы (Q1–Q8)

| # | Вопрос | Метод | Fallback if FAIL |
|---|---|---|---|
| Q1 | Регистрация `AgentDefinition(background=True)` через `ClaudeAgentOptions.agents={"general": ad}` действительно запускает subagent в фоне (main `query()` не блокируется на завершении subagent'а)? | Минимальный `query()` с зарегистрированным агентом + промпт «Use the `general` task to write 500 words; reply with task_id immediately». Замерить `(time of main ResultMessage) vs (time of TaskNotificationMessage)`. | Если main блокируется — переходим на `ClaudeSDKClient` + явный второй `query()` для subagent в отдельном `asyncio.Task`. |
| Q2 | Использует ли модель native Task tool при простом промпте (без явных инструкций), если в `agents` зарегистрирован `general`? Или нужно явно tools=["Task"] добавить в options? | Тот же спайк; смотрим, какие `tool_use` блоки эмитит модель. | Явно добавляем `Task` в `_GLOBAL_BASELINE`; SKILL.md для `task` инструктирует модель. |
| Q3 | `TaskStartedMessage / TaskProgressMessage / TaskNotificationMessage` эмитятся в SAME stream как main `query()`-iterator? Или отдельный канал? | Логируем все элементы main iter'а; матчим isinstance. | Если в отдельном канале — придётся хук-only; ledger update идёт через `SubagentStart`/`SubagentStop` hooks вместо message stream. |
| Q4 | Может ли subagent сам spawn'ить sub-subagent (recursion)? Какой максимум по умолчанию? Как distinguish'ить depth в `SubagentStartHookInput`? | Subagent с системным промптом «Now spawn a child task». Логируем `agent_id` + parent context. | Если recursion невозможна — depth cap N/A, но ledger ставит `parent_agent_id=None`. |
| Q5 | `SubagentStop` hook fires reliably ПОСЛЕ записи transcript? `agent_transcript_path` доступен и read-safe из main process? | В hook читаем file size + assistant blocks; verify finalized. | Если race — sleep(200ms) + retry с exponential backoff (max 5 attempts). |
| Q6 | Subagent, spawn'нутый из scheduler-инициированного main turn'а (другой `ClaudeBridge` instance), всё равно генерит `SubagentStop` hook events, которые наш central hook handler видит? | Спайк spawn'ит subagent из второго `query()` запущенного из второго bridge instance, проверяет глобально что hook fired. | Если hook isolated per-options → ставим один shared `SubagentStop` hook factory, передаваемый ВСЕМ bridge'ам через `Daemon.start()`. |
| Q7 | Cancel API: `task.cancel()` на main `query()` task стрелит и subagent'ам? Есть ли native subagent-level cancel? | Запустить subagent, через 3 сек `main_task.cancel()`, наблюдать что происходит с child. | Flag-based: PreToolUse hook на каждый Bash вызов проверяет `subagent_jobs.cancel_requested=1` для текущего `agent_id` (доступен в `HookContext`?) → возвращает deny → subagent падает с FailureMessage. |
| Q8 | Concurrency: SDK ставит cap на конкурентных subagent'ов? Как управляется (env, opt, hardcoded)? Если env — какое имя? | Параллельно запустить 8 subagent'ов из одного main; смотреть actual concurrency. Метрика — wallclock time. | Если cap < 8 — конфигурим через env (если опция есть) или принимаем как SDK contract. |

### 2.2. Spike exit criteria

- **PASS на Q1, Q3, Q5, Q6** — критические; без них переписываем план.
- **PARTIAL на Q2, Q4, Q7, Q8** — fallback задокументирован, продолжаем.
- **FAIL на любом критическом** → revision (новая планинговая итерация). Принимаем риск контролируемого второго rewrite (по решению владельца).

### 2.3. Дополнительные probes (cheap)

- `list_sessions(directory=str(project_root))` — какой формат session_id, можем ли матчить с `agent_id`?
- `get_session_messages(session_id)` — альтернативный путь для чтения transcript, если `agent_transcript_path` race'ит.
- `delete_session(session_id)` — для cleanup в phase 9; verify не нужно ли немедленно.

---

## 3. AgentDefinition design

### 3.1. Три названных агента

`src/assistant/subagent/definitions.py` (~100 LOC):

```python
from claude_agent_sdk import AgentDefinition

# Per-kind system prompt template. Field `prompt` ожидает full system prompt
# (по доке) ИЛИ appended на base — `[S-6-0 Q9 cheap]`. По умолчанию full.
_GENERAL_PROMPT = """\
You are a background subagent spawned by 0xone-assistant.
Your task is provided in the initial prompt.
You do NOT have direct access to the owner.
Your final assistant text will be delivered to them via Telegram verbatim.
Rules:
- Complete proactively. Do not ask clarifying questions.
- Reply with the FINAL result as your last assistant message.
- Be concise unless the task explicitly asks for long form.
- Available tools per registered list.
Owner project root: {project_root}
Vault: {vault_dir}
"""

_WORKER_PROMPT = """\
You are a worker subagent. Your task is to execute a single CLI invocation
or a tightly scoped tool sequence. Output the tool's result and stop.
Do not explore beyond the task's scope.
"""

_RESEARCHER_PROMPT = """\
You are a research subagent. Use Read/Grep/Glob/WebFetch to gather information.
Produce a concise structured summary. Do not modify files.
"""

def build_agents(project_root: Path, vault_dir: Path) -> dict[str, AgentDefinition]:
    base_ctx = {"project_root": str(project_root), "vault_dir": str(vault_dir)}
    return {
        "general": AgentDefinition(
            description="Generic background task: long writing, multi-step reasoning",
            prompt=_GENERAL_PROMPT.format(**base_ctx),
            tools=["Bash", "Read", "Write", "Edit", "Grep", "Glob", "WebFetch"],
            model="inherit",   # `[S-6-0 Q10 cheap]` — verify "inherit" valid
            maxTurns=20,
            background=True,   # `[S-6-0 Q1]`
            permissionMode="default",
        ),
        "worker": AgentDefinition(
            description="Run a single CLI tool and report its output",
            prompt=_WORKER_PROMPT,
            tools=["Bash", "Read"],
            model="inherit",
            maxTurns=5,
            background=True,
            permissionMode="default",
        ),
        "researcher": AgentDefinition(
            description="Read-only research and summarisation",
            prompt=_RESEARCHER_PROMPT,
            tools=["Read", "Grep", "Glob", "WebFetch"],
            model="inherit",
            maxTurns=15,
            background=True,
            permissionMode="default",
        ),
    }
```

### 3.2. Поля AgentDefinition (verified via grep)

```
description: str
prompt: str                                # full system prompt (assumed; [S-6-0 Q9])
tools: list[str] | None                    # narrows from main allowed_tools
disallowedTools: list[str] | None
model: str | None                          # alias or full ID; "inherit" likely
skills: list[str] | None                   # which manifest skills visible — [S-6-0 Q11 cheap]
memory: Literal["user","project","local"] | None
mcpServers: ...
initialPrompt: str | None
maxTurns: int | None
background: bool | None                    # CRITICAL — phase 6 needs True
effort: Literal["low","medium","high","max"] | int | None
permissionMode: PermissionMode | None
```

`skills` вероятно мапится один-к-одному на наши SKILL.md slug'и из `skills/` directory `[S-6-0 Q11]`. По default — None (subagent видит все).

### 3.3. Регистрация в ClaudeBridge

`bridge/claude.py::_build_options` расширяется:

```python
agents = build_agents(self._settings.project_root, self._settings.vault_dir)
return ClaudeAgentOptions(
    cwd=str(pr),
    setting_sources=["project"],
    max_turns=self._settings.claude.max_turns,
    allowed_tools=allowed_tools,
    hooks=hooks,                  # phase-6 extends with SubagentStart/Stop
    agents=agents,                # NEW phase 6
    system_prompt=system_prompt,
    **thinking_kwargs,
)
```

Изменение в `bridge/claude.py` ~15 LOC, не больше.

---

## 4. Hooks — SubagentStart + SubagentStop

`src/assistant/subagent/hooks.py` (~150 LOC). Альтернативно — продолжить добавлять в `bridge/hooks.py` ради консистентности. Решение: separate `subagent/hooks.py` (smaller blast radius).

### 4.1. Контракт hook input/output

Из SDK grep:

```python
class SubagentStartHookInput(BaseHookInput):
    hook_event_name: Literal["SubagentStart"]
    agent_id: str          # GUID из SDK
    agent_type: str        # "general" / "worker" / "researcher" — наш registered key

class SubagentStopHookInput(BaseHookInput):
    hook_event_name: Literal["SubagentStop"]
    stop_hook_active: bool
    agent_id: str
    agent_transcript_path: str   # path to transcript file
    agent_type: str

class SubagentStartHookSpecificOutput(TypedDict):
    hookEventName: Literal["SubagentStart"]
    additionalContext: NotRequired[str]   # injected как system message?
```

`BaseHookInput` имеет `session_id, transcript_path, cwd, permission_mode` — также доступны.

### 4.2. Factory

```python
def make_subagent_hooks(
    *,
    store: SubagentStore,
    adapter: TelegramAdapter,
    owner_chat_id: int,
    settings: Settings,
    pending_updates: set[asyncio.Task],   # phase-5 pattern
    log: structlog.BoundLogger,
) -> dict[str, list[HookMatcher]]:
    """Build SubagentStart + SubagentStop hooks, attached as ClaudeAgentOptions.hooks."""

    async def on_subagent_start(input_data, tool_use_id, ctx) -> dict:
        raw = cast(dict, input_data)
        agent_id = raw["agent_id"]
        agent_type = raw["agent_type"]
        parent_session = raw.get("session_id")

        depth = await store.compute_depth(agent_id, parent_session)
        if depth >= 3:
            log.warning("subagent_recursion_blocked", agent_id=agent_id, depth=depth)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "SubagentStart",
                    "additionalContext": "Recursion depth limit reached; do not spawn further subagents.",
                },
            }

        callback_chat_id = owner_chat_id  # single-user contract
        spawned_by_kind, spawned_by_ref = _infer_spawn_attribution(parent_session)

        await store.record_started(
            sdk_agent_id=agent_id,
            agent_type=agent_type,
            sdk_session_id=parent_session,
            callback_chat_id=callback_chat_id,
            spawned_by_kind=spawned_by_kind,
            spawned_by_ref=spawned_by_ref,
            parent_agent_id=...,
        )
        return {}

    async def on_subagent_stop(input_data, tool_use_id, ctx) -> dict:
        raw = cast(dict, input_data)
        agent_id = raw["agent_id"]
        transcript_path = raw["agent_transcript_path"]

        try:
            blocks = _read_transcript_assistant_blocks(transcript_path)
        except FileNotFoundError:
            log.warning("subagent_transcript_missing", path=transcript_path)
            blocks = []

        result_text = _extract_final_assistant_text(blocks)
        cost_usd, num_turns = _read_transcript_meta(transcript_path)

        try:
            await store.record_finished(
                sdk_agent_id=agent_id,
                status="completed",
                result_summary=result_text[:500],
                transcript_path=transcript_path,
                cost_usd=cost_usd,
            )
        except Exception:
            log.warning("subagent_record_finished_failed", exc_info=True)

        job = await store.get_by_agent_id(agent_id)
        if job is None:
            log.warning("subagent_job_unknown_on_stop", agent_id=agent_id)
            return {}

        formatted = _format_notification(result_text, job)
        update_task = asyncio.create_task(
            adapter.send_text(job.callback_chat_id, formatted)
        )
        pending_updates.add(update_task)
        update_task.add_done_callback(pending_updates.discard)
        try:
            await asyncio.shield(update_task)
        except asyncio.CancelledError:
            pass
        return {}

    from claude_agent_sdk import HookMatcher
    return {
        "SubagentStart": [HookMatcher(hooks=[on_subagent_start])],
        "SubagentStop": [HookMatcher(hooks=[on_subagent_stop])],
    }
```

### 4.3. Контекст hook (sync vs async)

Phase-3 hooks работают как async. Subagent hooks тоже async per SDK signature. `[S-6-0 Q3]` confirms event-loop access — мы делаем aiosqlite calls + adapter.send_text внутри hook (через store с lock).

### 4.4. Notification format (Q4 locked)

```python
def _format_notification(result_text: str, job: SubagentJob) -> str:
    duration_s = (job.finished_at - job.created_at).total_seconds()
    cost = f"${job.cost_usd:.4f}" if job.cost_usd else "$?"
    footer = f"\n\n---\n[job {job.id} {job.status} in {duration_s:.0f}s, kind={job.agent_type}, cost={cost}]"
    return result_text.strip() + footer
```

### 4.5. Throttle

Per-chat min 500 ms между notify'ами. Реализация — module-level `dict[chat_id, last_sent_at]`; sleep'им до min interval.

---

## 5. DB schema v4 — `subagent_jobs` ledger

`src/assistant/state/migrations/0004_subagent.sql`:

```sql
-- 0004_subagent.sql — phase 6: SDK-native subagent ledger
CREATE TABLE IF NOT EXISTS subagent_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sdk_agent_id TEXT NOT NULL UNIQUE,
    sdk_session_id TEXT,
    parent_session_id TEXT,
    agent_type TEXT NOT NULL,
    task_text TEXT,
    transcript_path TEXT,
    status TEXT NOT NULL DEFAULT 'started',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    result_summary TEXT,
    cost_usd REAL,
    callback_chat_id INTEGER NOT NULL,
    spawned_by_kind TEXT NOT NULL,
    spawned_by_ref TEXT,
    parent_agent_id TEXT,
    depth INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    finished_at TEXT
);
CREATE INDEX idx_subagent_jobs_status_created ON subagent_jobs(status, created_at);
CREATE INDEX idx_subagent_jobs_agent_id ON subagent_jobs(sdk_agent_id);
CREATE INDEX idx_subagent_jobs_parent ON subagent_jobs(parent_agent_id);
PRAGMA user_version = 4;
```

Гораздо тоньше чем wave-0 schema (12 columns vs 17, нет `pool_position`, `claim_lock`, `attempts`, `allowed_tools_json` — всё это SDK manages).

---

## 6. `SubagentStore` (aiosqlite)

`src/assistant/subagent/store.py` (~150 LOC). Использует `ConversationStore.lock` (shared, phase-5 pattern).

Методы:
- `record_started(sdk_agent_id, agent_type, ...) -> int` — INSERT
- `record_finished(sdk_agent_id, status, ...) -> None` — status-precondition UPDATE
- `set_cancel_requested(job_id) -> None` — UPDATE flag
- `is_cancel_requested(sdk_agent_id) -> bool` — SELECT
- `compute_depth(agent_id, parent_session_id) -> int` — heuristic via parent chain
- `recover_orphans() -> int` — boot scan: `UPDATE WHERE status='started' AND finished_at IS NULL → 'interrupted'`
- `get_by_agent_id(sdk_agent_id) -> SubagentJob | None`
- `get_by_id(job_id) -> SubagentJob | None`
- `list_jobs(status=None, kind=None, limit=20) -> list[SubagentJob]`

Все mutations under `async with self._lock`. Status-precondition в `WHERE status='started'` на каждом UPDATE.

---

## 7. CLI `tools/task/main.py` — тонкий

stdlib + `sys.path.append(<root>/src)` shim. ~250 LOC.

### 7.1. Subcommands

| Команда | Args | Output | Exit |
|---|---|---|---|
| `spawn` | `--kind K --task TEXT [--callback-chat-id N]` | `{"job_id": N, "status": "started"}` | 0/2/3 |
| `list` | `[--status S] [--kind K] [--limit 20]` | array | 0 |
| `status` | `<job_id>` | full row | 0/7 |
| `cancel` | `<job_id>` | `{"cancel_requested": true, "previous_status": ...}` | 0/3/7 |
| `wait` | `<job_id> [--timeout-s 60]` | poll DB until status terminal | 0/5/7 |

### 7.2. Spawn semantics

`spawn` нужен для:
- **Scheduler-инициированный subagent**: не из основной модели, а из shell (если scheduler-turn так решит) или напрямую из scheduler-loop по будущей конфиге.
- **Manual shell spawn** (debug, ops).

В main user turn'е модель использует **native Task tool** (зарегистрированные agents) — это автоматически создаёт subagent через SDK без CLI. Hook on_subagent_start пишет ledger row.

CLI `spawn` — это thin wrapper. **Recommended (Q13 cheap):** CLI `spawn` → INSERT request row → daemon-side `SubagentRequestPicker` (separate bg task) `claim_pending` → invokes ClaudeBridge with single user prompt + Task-tool nudge. SDK-native flow then takes over (TaskStartedMessage emits from subagent).

### 7.3. Cancel CLI semantics (devil GAP #3 fix)

```python
def cancel_handler(job_id: int) -> dict:
    job = store.get_by_id(job_id)
    if job is None:
        return exit_not_found()
    if job.status in ("completed", "failed", "stopped", "interrupted"):
        return {"already_terminal": job.status}
    store.set_cancel_requested(job_id)
    return {"cancel_requested": True, "previous_status": job.status}
```

### 7.4. Bash hook gate

`bridge/hooks.py` extension `_validate_task_argv` (~80 LOC, mirror `_validate_schedule_argv`):
- Subcommand whitelist
- Per-sub flag whitelist
- Dup-flag rejection (lesson B-W2-5)
- Size caps: `--task` ≤ 4096 bytes
- Range: `--callback-chat-id` int, `--timeout-s` 1–600

`_validate_python_invocation` extends:
```python
if script == "tools/task/main.py":
    return _validate_task_argv(argv[2:])
```

---

## 8. Skill `skills/task/SKILL.md`

```yaml
---
name: task
description: "Делегирование долгих задач в фоновый subagent. Используй когда задача может занять > 10 секунд: длинный пост, research, transcribe/genimage (phase 7), bulk tool invocation. Native Task tool из main turn'е возвращает task_id мгновенно; результат придёт владельцу проактивно в Telegram когда subagent завершится. CLI `python tools/task/main.py` — для list/status/cancel и shell-init spawn."
allowed-tools: [Bash, Read]
---
```

Body:
- Когда использовать (>10s tasks, research, deep writing).
- Когда НЕ (быстрые ответы, нужна clarification).
- Kinds: general/worker/researcher.
- Native Task tool — модель вызывает напрямую; `tools/task/main.py spawn` для shell init.
- `list`/`status`/`cancel` примеры.
- Границы: depth=3, single owner.

---

## 9. Bash hook gate

См. §7.4 — extend `bridge/hooks.py` с `_validate_task_argv`. ~80 LOC.

---

## 10. Daemon.start / stop integration

### 10.1. start() additions

```python
sub_store = SubagentStore(self._conn, lock=conv.lock)

recovered = await sub_store.recover_orphans()
if recovered:
    self._log.warning("subagent_orphans_recovered", count=recovered)
    self._spawn_bg(
        self._adapter.send_text(
            self._settings.owner_chat_id,
            f"daemon restart: {recovered} subagent(s) interrupted (status='interrupted'). respawn manually if needed.",
        ),
        name="subagent_orphan_notify",
    )

self._subagent_pending_updates: set[asyncio.Task] = set()
sub_hooks = make_subagent_hooks(
    store=sub_store,
    adapter=self._adapter,
    owner_chat_id=self._settings.owner_chat_id,
    settings=self._settings,
    pending_updates=self._subagent_pending_updates,
    log=self._log,
)

bridge = ClaudeBridge(self._settings, extra_hooks=sub_hooks)

self._spawn_bg(SubagentRequestPicker(sub_store, bridge).run(), name="subagent_picker")
```

### 10.2. ClaudeBridge constructor change

```python
class ClaudeBridge:
    def __init__(self, settings: Settings, *, extra_hooks: dict[str, list[HookMatcher]] | None = None):
        self._settings = settings
        self._sem = asyncio.Semaphore(settings.claude.max_concurrent)
        self._extra_hooks = extra_hooks or {}

    def _build_options(self, *, system_prompt: str) -> ClaudeAgentOptions:
        ...
        hooks: dict[Any, Any] = {
            "PreToolUse": make_pretool_hooks(pr),
            "PostToolUse": make_posttool_hooks(pr, dd),
            **self._extra_hooks,
        }
        agents = build_agents(pr, self._settings.vault_dir)
        return ClaudeAgentOptions(
            ...,
            hooks=hooks,
            agents=agents,
        )
```

### 10.3. stop() additions

В `Daemon.stop()` после step 2.5 (drain scheduler shielded) и до step 3 (adapter.stop):

```python
# Step 2.6 — phase-6: drain subagent SubagentStop hook shielded notifies
if self._subagent_pending_updates:
    updates = list(self._subagent_pending_updates)
    self._log.info("daemon_draining_subagent_notifies", count=len(updates))
    try:
        await asyncio.wait_for(
            asyncio.gather(*updates, return_exceptions=True),
            timeout=2.0,
        )
    except TimeoutError:
        self._log.warning(
            "daemon_subagent_drain_timeout",
            count=len([t for t in updates if not t.done()]),
        )
```

In-flight subagent processes themselves: SDK manages их lifecycle; на SIGTERM SDK либо drain'ит либо kill'ит. Наша responsibility — только update DB на recover (next boot).

---

## 11. Open questions for spike S-6-0 (consolidated)

| # | Q | Critical? |
|---|---|---|
| Q1 | `agents={...} + background=True` actually backgrounds main session | YES |
| Q2 | Native Task tool auto-discovered by model from `agents` registry | NO (fallback: explicit Task in baseline) |
| Q3 | `Task*Message` emitted in main `query()` iter | YES |
| Q4 | Recursion supported by SDK; depth attribution method | NO (cap can be metric-based via ledger) |
| Q5 | `SubagentStop` hook fires post-transcript-flush | YES |
| Q6 | Hooks fire across multiple bridge instances (scheduler-spawned) | YES |
| Q7 | Cancellation API or polling-required | NO (fallback: PreToolUse flag check) |
| Q8 | SDK concurrency cap | NO (accept SDK contract) |
| Q9 (cheap) | `prompt` field semantic — full vs append-base | accept full assumption |
| Q10 (cheap) | `model="inherit"` valid | grep CLI source if doc unclear |
| Q11 (cheap) | `skills` field maps to SKILL.md slugs | empirical: list visible skills in subagent system_prompt |
| Q12 (cheap) | `BaseHookInput.session_id` reliable parent ref | log + diff |
| Q13 (cheap) | CLI spawn path: subprocess vs daemon-pickup | architecture decision (recommended pickup) |

---

## 12. Risk register

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | Native API behavior unverified (Q1, Q3, Q5, Q6) | 🔴 | S-6-0 BLOCKER spike; second rewrite if FAIL |
| 2 | Cross-session subagent-from-scheduler unproven | 🔴 | S-6-0 Q6; shared hook factory across all bridges |
| 3 | Hook callback context — async vs sync | 🟡 | Phase-3 precedent confirms async; verify in S-6-0 |
| 4 | Transcript file race | 🟡 | sleep+retry fallback; alternative `get_session_messages` API |
| 5 | Cancel API undocumented | 🟡 | Flag-poll fallback via PreToolUse hook |
| 6 | `prompt` field semantic unclear | 🟢 | Q9 cheap; default to full prompt assumption |
| 7 | SDK upgrades change semantics | 🟢 | Version-pin to 0.1.59; phase-9 review on bump |
| 8 | depth heuristic flawed (parent_session not reliable) | 🟡 | Q12 + ledger-based heuristic; documented limitation |
| 9 | Notify spam | 🟢 | 500ms throttle (preserved) |
| 10 | Large result overflow Telegram | 🟢 | Existing split_for_telegram |
| 11 | `agent_id` mismatch between Start and Stop hooks | 🟡 | S-6-0 Q14 cheap: log both, verify identical |
| 12 | Daemon stop with in-flight subagent loses transcript | 🟡 | Recovery on next boot marks as 'interrupted' + notify |

---

## 13. Devil wave-1 fixes folded in

| GAP | Resolution |
|---|---|
| #5 synthetic chat_id | N/A — SDK uses `session_id` natively, no synthetic chat_id needed |
| #6 timeout layering | N/A — SDK manages via `AgentDefinition.maxTurns` and inherent timeouts |
| #11 dispatch_reply helper (phase-7) | N/A in phase-6; SubagentStop hook calls `adapter.send_text` directly |
| BLOCKER #2 N=8 spike | Folded into S-6-0 Q8 |
| BLOCKER #3 cancel+pending orphan | N/A — no DB-side state machine for queue; SDK manages execution |
| BLOCKER #4 dispatcher in worker pool | N/A — SDK manages execution; hook is pure async callback |
| BLOCKER #5 env propagation | N/A — SDK manages subagent context; depth via ledger |

---

## 14. Tech debt explicitly deferred (phase 9)

1. `_memlib` consolidation (phase-5 debt)
2. `HISTORY_MAX_SNIPPET_TOTAL_BYTES` cap (phase-5 debt)
3. Subagent health watchdog
4. Admin panel
5. Job retention sweeper
6. Skill-declared `subagent_kind` in SKILL.md frontmatter (phase 7)
7. `TaskProgressMessage` mid-run streaming
8. Refactor of `ClaudeBridge` if S-6-0 reveals options/hook coupling needs change
9. Cleanup via `delete_session` for old subagents

---

## 15. File tree

### 15.1. New

| Path | LOC |
|---|---|
| `src/assistant/state/migrations/0004_subagent.sql` | 30 |
| `src/assistant/subagent/__init__.py` | 5 |
| `src/assistant/subagent/definitions.py` | 100 |
| `src/assistant/subagent/store.py` | 150 |
| `src/assistant/subagent/hooks.py` | 180 |
| `src/assistant/subagent/picker.py` | 120 |
| `src/assistant/subagent/format.py` | 50 |
| `tools/task/main.py` | 250 |
| `skills/task/SKILL.md` | 80 |
| `spikes/phase6_s0_native_subagent.py` | 200 |
| `spikes/phase6_s0_findings.md` | (markdown report) |
| Tests (~10 files) | 600 |

### 15.2. Modified

| File | Δ LOC |
|---|---|
| `src/assistant/state/db.py` | +20 (`_apply_v4` + `SCHEMA_VERSION=4`) |
| `src/assistant/config.py` | +25 (`SubagentSettings`) |
| `src/assistant/main.py` | +50 (recover_orphans + sub_hooks wiring + drain) |
| `src/assistant/bridge/claude.py` | +25 (extra_hooks param + agents in options) |
| `src/assistant/bridge/hooks.py` | +100 (`_validate_task_argv` + dispatch in `_validate_python_invocation`) |
| `src/assistant/bridge/system_prompt.md` | +5 (note: Task tool available) |

**Total LOC:** ~1300 (vs old ~3300 — 60% reduction).

---

## 16. Testing plan

### 16.1. Unit (~250 LOC)

- `test_subagent_definitions.py`: build_agents returns 3, prompt template fills, tools narrowed.
- `test_subagent_store.py`: record_started + record_finished + status-precondition + recover_orphans + cancel flag.
- `test_subagent_hooks.py`: on_subagent_start records ledger, on_subagent_stop reads transcript + notifies. SDK hook input shape mock'аем.
- `test_task_cli.py`: all subcmds + edge cases + bash hook validation.

### 16.2. Integration (~250 LOC)

- `test_subagent_recovery.py`: seed `started` orphan → boot → status='interrupted'.
- `test_subagent_e2e_native.py`: only runs if env `RUN_SDK_INT=1` — actual `query()` with registered agent; verify hook fires; verify Telegram-send called.
- `test_subagent_scheduler_handoff.py`: scheduler-spawned subagent → ledger row has `spawned_by_kind='scheduler'` → notify delivered.
- `test_subagent_recursion_cap.py`: simulate 4 levels deep; SubagentStart hook returns deny-context.

### 16.3. Mock vs real-SDK testing

Все unit-тесты мокают SDK hook input dicts напрямую — используем `cast(SubagentStartHookInput, {...})` shape. Integration `test_subagent_e2e_native.py` единственный реально invoked'ит SDK; gated env var.

### 16.4. Failure modes

- Transcript file missing on stop → log warning, no notify.
- `agent_id` mismatch start/stop → ledger has orphan `started` row → next boot recovers.
- `record_started` fails → log + still allow subagent to proceed (SDK-side); notify won't fire correctly but daemon stable.
- Adapter.send_text raises → shielded task captures; pending_updates drain catches.

---

## 17. Invariants (slimmed from wave-0's 10 → 5)

1. **Status transitions guarded by SQL precondition.** Every UPDATE has `WHERE status='<expected>'`; rowcount=0 → log skew, не raise.
2. **`SubagentStop` hook records terminal state exactly once per subagent.** UNIQUE(`sdk_agent_id`) prevents duplicate ledger rows.
3. **`recover_orphans` runs ONCE at boot before bridge accepts new turns.**
4. **`Daemon.stop()` drains `_subagent_pending_updates` before `conn.close()`.**
5. **Subagent's Bash/file/web tools go through SAME PreToolUse hooks.** `[S-6-0 Q15 cheap]` — hooks attached to options propagate to subagent execution.

---

## 18. Skeptical notes

- **Высокая неопределённость:** план рисует SDK-native слой без runtime-verification. Принят риск второго rewrite.
- **CLI `spawn` vs native Task:** в основном диалоге модель почти всегда юзает native Task (если Q2 PASS); CLI spawn — escape-hatch и для scheduler/shell. Это нормальная асимметрия — SKILL.md инструктирует.
- **Depth=3 cap:** heuristic compute_depth опирается на parent_session мapping. Если Q12 FAIL — депт остаётся approximate, но уже ledger-based.
- **Hook fire reliability across bridge instances (Q6):** если SDK hook attachment per-options, нужно гарантировать ВСЕ bridge'ы получают тот же `subagent_hooks` factory. `Daemon.start()` создаёт ЕДИНЫЙ `bridge` instance для всего daemon'а (phase 5 уже шарит) — single-bridge ⇒ single-options ⇒ no problem. Если phase-6 захочет per-turn fresh bridge — переезд на global registry.
- **SubagentRequestPicker (CLI spawn pickup):** добавляет один bg task в Daemon'е, аналогично scheduler dispatcher. Нагрузка минимальна.
- **Manual respawn вместо retry:** preserved from phase-5 (lesson CRITICAL #1). Avoid orphan'ы.

---

## 19. Q&A locked decisions (translated)

| # | Q | Phase-6 incarnation |
|---|---|---|
| Q1 | N=8 pool size | SDK-managed cap; verify in S-6-0 Q8; default no override |
| Q2 | Fresh conversation per job | SDK-native: each subagent gets own session_id |
| Q3 | 3 built-in kinds | Translated to 3 `AgentDefinition` instances |
| Q4 | Notify format with footer | Preserved verbatim |
| Q5 | CLI-only cancel | CLI sets flag; SDK or PreToolUse hook delivers |
| Q6 | Recursion depth=3 | SubagentStart hook returns context-deny on 4th level |
| Q7 | Shared OAuth | SDK-managed |
| Q8 | Forever retention | SDK transcripts on disk; phase-9 cleanup |
| Q9 | `task` skill always available | Skills manifest unchanged; Task tool auto-registered |
| Q10 | No auto-retry | Preserved |
| Q11 | Schema v4 | Preserved (12-column ledger) |
| Q12 | Fresh bridge per job | N/A — SDK manages, single bridge instance shared |

---

## 20. Summary & exit checklist

Phase 6 = SDK-native subagent integration. Ledger-only DB, shared bridge, hook-based notify.

**Exit:**
- [ ] S-6-0 spike completed; Q1, Q3, Q5, Q6 PASS (or critical FAIL → revision)
- [ ] Migration v4 applied
- [ ] AgentDefinition registry covers general/worker/researcher
- [ ] SubagentStart + SubagentStop hooks integrated, ledger updated
- [ ] CLI tools/task/main.py covers spawn/list/status/cancel/wait
- [ ] Bash hook gate validates task argv
- [ ] Daemon orphan recovery + stop-drain integrated
- [ ] ~30 new tests passing (~900 total)
- [ ] mypy strict clean on new modules
- [ ] Phase-5 invariants preserved
- [ ] E2E spawn (CLI + native Task) → notify verified

---

### Critical Files for Implementation

- /Users/agent2/Documents/0xone-assistant/src/assistant/main.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/claude.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/hooks.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/state/db.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/config.py
