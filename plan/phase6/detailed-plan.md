# Phase 6 — детальный план (Subagent infrastructure — universal parallel pool)

Phase 6 вводит универсальный **субагентский фоновый pool**, в который основная Claude-сессия может делегировать любую долгую задачу без блокировки чата с владельцем. В отличие от phase-5 scheduler'а (who creates autonomous turns *on a clock*), phase-6 subagent pool создаёт autonomous SDK-сессии *on-demand from the main model*. Результат proactively доставляется в Telegram через тот же `adapter.send_text(OWNER_CHAT_ID, ...)` канал.

## Mental model

- **Main session** — одна ClaudeBridge-сессия, запускаемая `ClaudeHandler` на каждый user/scheduler turn, с per-chat lock. Держит conversation `kind='user'` (main history).
- **Subagent pool** — N independent ClaudeBridge-сессий (default N=4), каждая со своим `conversation_id` (`kind='subagent'`), работает в отдельном `asyncio.Task`. Main-session не ждёт их; только триггерит spawn.
- **Spawn API** — CLI `python tools/task/main.py spawn --kind K --task T` (modeled after `tools/schedule/main.py`). Модель в main-turn'е вызывает через Bash (скрещивается с phase-4 hook gate).
- **Terminal state notify** — когда job transit'ит в `done|failed|cancelled`, `SubagentDispatcher` доставляет результат владельцу. Main-session может не видеть результат inline; видит в следующем turn'е через memory или по запросу (`status 42`).

---

## 0. Changes from wave-0 и критические исходные точки

Phase 6 стартует с плана-прототипа, исправленного по следующим принципам:
- **Universal, not media-specific** (user request).
- **Fresh conversation_id per job** (изоляция, не пересекается с main).
- **Manual respawn, no auto-retry** (explicit lesson из phase-5 retry-pipeline CRITICAL #1).
- **In-process pool, one daemon** (никакого out-of-process, пока single-user — тот же tradeoff, что и phase-5 scheduler).
- **Status-precondition SQL на каждую transition** (lesson phase-5 G-W2-6).
- **`asyncio.shield` + `_pending_updates` tracking на shutdown** (lesson phase-5 HIGH #5).

## 1. Spikes (BLOCKER + auxiliary)

### 1.1. S-6-1 (BLOCKER) — parallel SDK sessions feasibility

**Вопрос:** поддерживает ли `claude-agent-sdk` 0.1.59 N параллельных `query()` / `ClaudeSDKClient` в одном процессе без gobal-state collision? Что с OAuth session?

**Артефакт:** `spikes/phase6_s1_parallel_sdk.py`. Сценарий: 3 parallel queries через `asyncio.gather`. Измеряем session_id уникальность, parallel time vs sequential, ps subprocess count. Outcomes: PASS (truly parallel) / PARTIAL (shared OAuth, serialised) / FAIL (shared-state crash → fallback per-instance subprocess).

### 1.2. S-6-2 — CancelledError mid-query cleanup

`spikes/phase6_s2_sdk_cancel.py`: start query, after 2 seconds `task.cancel()`. Verify: aclose, no zombie subprocess, no aiofile leak.

### 1.3. S-6-3 — SDK Task tool native? (cheap)

Grep для confirm: SDK не имеет native subagent — roll our own.

### 1.4. Spike exit criteria

Все три spike'а PASS/PARTIAL → coder. Любой FAIL → revision.

---

## 2. Архитектурные решения с tradeoff'ами

### 2.1. Pool size cap (`SUBAGENT_POOL_SIZE`)

**Опции:** N=1 (serialise — defeat purpose), N=2 (conservative), **N=4 Recommended** (balance), N=8+ (rate-limit risk). Override env. Hard ceiling 16.

### 2.2. Conversation isolation

**(A) Fresh `conversation_id` per job (Recommended).** `chat_id = -(1000000 + job_id)`. История изолирована. Main session видит subagent через CLI `status N`.
**(B) Shared:** pollutes context. Rejected.

### 2.3. Subagent type registry

3 built-in kinds:

| Kind | allowed_tools | Use-case |
|---|---|---|
| `general` | Bash, Read, Write, Edit, Grep, Glob, WebFetch | "напиши пост", generic |
| `worker` | Bash, Read | CLI wrappers (phase 7 media) |
| `researcher` | Bash, Read, Grep, Glob, WebFetch | research, summaries |

Per-job `--allowed-tools` narrowing only (intersected with type's set).

### 2.4. Spawn API surface

**(Recommended) CLI.** `python tools/task/main.py spawn ...` — sync INSERT, returns `job_id`. Worker pool обнаруживает next poll. Consistent с phase-4/5 pattern, LLM-discoverable.

### 2.5. Job lifecycle states

```
pending ──► running ──► done        (success)
                  │
                  ├──► failed        (exception)
                  │
                  └──► cancelled     (cancel_requested observed)

{running|pending} ──► interrupted (on SIGTERM); recovered to pending on boot.
```

Status-precondition SQL on every UPDATE. rowcount=0 → log skew, no raise.

### 2.6. Concurrency control & queueing

Pool cap N. CLI `spawn` всегда INSERT в `pending`. Workers poll. Atomic `claim_pending`:

```sql
UPDATE subagent_jobs SET status='running', started_at=?
WHERE id = (SELECT id FROM subagent_jobs WHERE status='pending' AND cancel_requested=0
            ORDER BY created_at ASC LIMIT 1)
AND status='pending'
RETURNING id, kind, task_text, ...
```

FIFO. Race-safe (SQLite write-lock).

### 2.7. Cancellation

`UPDATE SET cancel_requested=1`. Worker checks между блоками SDK-стрима, raises CancelledError. Shielded mark_cancelled (phase-5 HIGH #5 pattern):

```python
except asyncio.CancelledError:
    update_task = asyncio.create_task(self._store.mark_cancelled(job_id))
    self._pending_updates.add(update_task)
    update_task.add_done_callback(self._pending_updates.discard)
    await asyncio.shield(update_task)
    raise
```

### 2.8. OAuth / session sharing

**(Recommended after S-6-1):** shared OAuth, multiple bridges OK. Fallback: per-instance subprocess if shared race.

### 2.9. Owner notification policy

On terminal: `adapter.send_text(owner_chat_id, formatted)` chunked. Format: `<result>\n\n---\n[job N status in Xs, kind=K, cost=$Y]`. Throttle 500ms between notifies.

### 2.10. Conversation history of subagent

Stored in `conversations` with synthetic `chat_id = -(1000000 + job_id)`. CLI `status N --with-history` opt-in.

### 2.11. System prompt for subagent

Per-kind template. General example:

```
You are a background subagent spawned by the main 0xone-assistant session.
Your task is provided below. You do NOT have access to the owner — your result
will be delivered to them verbatim via Telegram when you finish.
Rules: complete proactively; return result as FINAL message; concise unless task
demands long form; tools available per kind.
Owner project root: {project_root}; vault: {vault_dir}.
Task: {task_text}
```

### 2.12. Resource caps per job

| Cap | Default | Env | |
|---|---|---|---|
| max_turns | 20 | SUBAGENT_MAX_TURNS or per-spawn | |
| timeout_s | 300 | SUBAGENT_TIMEOUT_S or per-spawn | |
| task_text bytes | 4096 | hardcoded | |
| Pool cap | 4 | SUBAGENT_POOL_SIZE | |
| Max pending | 64 | SUBAGENT_MAX_PENDING | |
| Max depth | 3 | SUBAGENT_MAX_DEPTH | |

### 2.13. Recursive subagents

Env `SUBAGENT_PARENT_JOB_ID` set by pool pre-run. CLI reads, looks up parent.depth+1, validates against cap. Depth tracked in DB column.

---

## 3. DB schema v4 — `subagent_jobs`

```sql
-- 0004_subagent.sql — phase 6
CREATE TABLE IF NOT EXISTS subagent_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    task_text TEXT NOT NULL,
    allowed_tools_json TEXT,
    conversation_id INTEGER,
    parent_job_id INTEGER REFERENCES subagent_jobs(id) ON DELETE SET NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    callback_chat_id INTEGER NOT NULL,
    max_turns INTEGER NOT NULL DEFAULT 20,
    timeout_s INTEGER NOT NULL DEFAULT 300,
    status TEXT NOT NULL DEFAULT 'pending',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    result_text TEXT,
    error_text TEXT,
    cost_usd REAL,
    sdk_session_id TEXT,
    num_turns INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX idx_subagent_jobs_status_created ON subagent_jobs(status, created_at);
CREATE INDEX idx_subagent_jobs_parent ON subagent_jobs(parent_job_id);
PRAGMA user_version = 4;
```

Status codes: `pending → running → done | failed | cancelled`. `interrupted` = SIGTERM mid-run, recovered on boot to `pending`.

---

## 4. CLI contract — `tools/task/main.py`

Stdlib-only. `sys.path.append(<root>/src)` shim.

| Command | Args | Output | Exits |
|---|---|---|---|
| spawn | --kind --task [--max-turns --timeout-s --allowed-tools] | {job_id, kind, status, depth} | 0/2/3/6 |
| status | <job_id> [--with-history] | full row | 0/7 |
| list | [--status --kind --limit] | array | 0 |
| cancel | <job_id> | {cancel_requested, previous_status} | 0/7/3 |
| wait | <job_id> [--timeout-s] | blocks polling 500ms | 0/5/7 |

Spawn validation: kind in registry; task ≤ 4096 bytes; pending count < cap; depth ≤ max_depth; allowed_tools narrowing.

Exit: 0 ok / 2 usage / 3 validation / 4 IO / 5 wait-timeout / 6 quota / 7 not-found.

---

## 5. Skill `skills/task/SKILL.md`

```yaml
---
name: task
description: "Делегирование долгих задач в фоновый subagent. Используй когда задача может занять > 10 секунд: длинный пост, research, transcribe/genimage (phase 7), bulk tool invocation. Spawn возвращает job_id; результат придёт владельцу проактивно в Telegram когда subagent завершится. CLI `python tools/task/main.py`."
allowed-tools: [Bash, Read]
---
```

Body: когда использовать (>10s tasks); когда НЕ (быстрые ответы, нужна clarification); kinds (general/worker/researcher); пример spawn; status/cancel/list; границы (64 pending, depth 3, 4096 byte task).

---

## 6. `SubagentStore` (aiosqlite)

Methods: `claim_pending`, `mark_done/failed/cancelled/interrupted`, `is_cancel_requested`, `set_cancel_requested`, `recover_orphans`, `get`, `list_jobs`, `list_pending`. All mutations under `async with self._lock`. Status-precondition on every UPDATE.

`claim_pending` uses atomic UPDATE...RETURNING (SQLite 3.35+). Race-safe.

---

## 7. `SubagentPool` component

```python
class SubagentPool:
    def __init__(self, *, settings, store, conv, adapter, dispatcher, bridge_factory=None): ...
    def stop_event(self) -> asyncio.Event: ...
    def stop(self): self._stop.set()
    def last_tick_at(self) -> float: ...
    def pending_updates(self) -> set[asyncio.Task]: ...

    async def run(self):
        # Spawn N worker tasks, await stop, cancel/gather
        ...

    async def _worker_loop(self, worker_id: int):
        while not self._stop.is_set():
            job = await self._store.claim_pending()
            if job is None: await asyncio.wait_for(self._stop.wait(), timeout=poll_interval); continue
            try:
                await self._run_job(job, worker_id=worker_id)
            except CancelledError:
                # shielded mark_interrupted
                ...
            except Exception:
                log.error(...)

    async def _run_job(self, job, *, worker_id):
        # 1. fresh ClaudeBridge instance
        # 2. fresh subagent_chat_id = -(1000000 + job_id)
        # 3. INSERT initial user-message into conversations
        # 4. env_overlay = {"SUBAGENT_PARENT_JOB_ID": str(job_id)}
        # 5. async with timeout: bridge.ask_subagent(...) → collect blocks
        #    - cancel poll between blocks
        #    - persist each block to conversations
        #    - accumulate text for result
        # 6. mark_done/failed/cancelled with cost+session+turns
        # 7. dispatcher.notify_terminal(job_id)
```

New `ClaudeBridge.ask_subagent(...)` (~50 LOC): per-kind system prompt, per-kind allowed_tools (narrowed), env_overlay via SDK ClaudeAgentOptions.env if supported (TBD spike).

---

## 8. `SubagentDispatcher` (notify path)

```python
class SubagentDispatcher:
    async def notify_terminal(self, job_id):
        # Throttle 500ms between notifies
        # Get job from store
        # Format result_text + footer "[job N status in Xs, kind=K, cost=$Y]"
        # adapter.send_text(owner, formatted) — chunked via existing splitter
```

Called directly by pool on terminal transition (no observer-loop, no queue — pool knows transition immediately).

---

## 9. `Daemon.start()` / `stop()` integration

### 9.1. Startup (after scheduler spawn)

```python
subagent_store = SubagentStore(self._conn, lock=conv.lock)
reverted = await subagent_store.recover_orphans()  # running|interrupted → pending
subagent_dispatcher = SubagentDispatcher(...)
self._subagent_pool = SubagentPool(...)
self._spawn_bg(self._subagent_pool.run(), name="subagent_pool")
```

### 9.2. Shutdown (extends phase-5 sequence)

1. Scheduler loop+dispatcher stop signal
2. **NEW: subagent_pool stop signal**
3. Drain `_bg_tasks` (5s timeout → cancel)
4. **NEW: drain `subagent_pool.pending_updates()` (shielded interrupt UPDATEs, 2s timeout)**
5. Drain `scheduler_dispatcher.pending_updates()` (existing)
6. `adapter.stop()`
7. `conn.close()`
8. pid-fd release

Step 4 mirrors phase-5 fix-pack HIGH #5 pattern.

### 9.3. Health check (NOT in phase 6)

`subagent_pool.last_tick_at()` field готов; phase 9 watchdog добавит monitoring.

---

## 10. Bash hook extension

`_validate_task_argv` для `tools/task/main.py` ~80 LOC. Pattern phase-5 `_validate_schedule_argv`:
- Subcommand whitelist
- Per-subcommand flag whitelist
- Dup-flag rejection (lesson B-W2-5)
- Size caps (task ≤ 4096B)
- Range checks (max_turns 1-100, timeout_s 10-3600, wait timeout_s 1-600)
- Tool name validation against `_GLOBAL_TOOLS_BASELINE` (re-export from claude.py)

---

## 11. Built-in subagent types registry

`src/assistant/subagent/types.py` ~80 LOC:

```python
@dataclass(frozen=True)
class SubagentType:
    name: str
    description: str
    allowed_tools: FrozenSet[str]
    system_prompt_template: str
    max_turns: int = 20
    timeout_s: int = 300

SUBAGENT_TYPES: dict[str, SubagentType] = {
    "general": SubagentType(...),
    "worker": SubagentType(...),
    "researcher": SubagentType(...),
}

def allowed_tools_for_kind(kind, override) -> list[str]:
    base = SUBAGENT_TYPES[kind].allowed_tools
    if override is None: return sorted(base)
    return sorted(set(override) & base)  # narrowing only
```

---

## 12. Config — `SubagentSettings`

```python
class SubagentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUBAGENT_", env_file=..., extra="ignore")
    enabled: bool = True
    pool_size: int = 4              # validator: 1-16
    max_pending: int = 64
    max_depth: int = 3
    default_timeout_s: int = 300
    default_max_turns: int = 20
    poll_interval_s: float = 1.0
    notify_throttle_ms: int = 500
```

Settings.subagent: SubagentSettings = Field(default_factory=...)

---

## 13. File tree additions

### 13.1. New files

| Path | LOC | Role |
|---|---|---|
| `src/assistant/state/migrations/0004_subagent.sql` | 40 | DDL v4 |
| `src/assistant/subagent/__init__.py` | 10 | |
| `src/assistant/subagent/store.py` | 220 | CRUD + recovery |
| `src/assistant/subagent/pool.py` | 280 | workers + _run_job + shield |
| `src/assistant/subagent/dispatcher.py` | 110 | notify + format + throttle |
| `src/assistant/subagent/types.py` | 90 | registry + prompts |
| `tools/task/main.py` | 380 | argparse + sqlite |
| `skills/task/SKILL.md` | 90 | |
| `spikes/phase6_s1_parallel_sdk.py` | 120 | |
| `spikes/phase6_s2_sdk_cancel.py` | 80 | |
| Tests (8 files) | 1000 | |

### 13.2. Modified files

| File | Delta | |
|---|---|---|
| `src/assistant/state/db.py` | +25 | _apply_v4 |
| `src/assistant/config.py` | +40 | SubagentSettings |
| `src/assistant/main.py` | +70 | pool lifecycle |
| `src/assistant/bridge/hooks.py` | +80 | _validate_task_argv |
| `src/assistant/bridge/claude.py` | +50 | ask_subagent |
| `src/assistant/bridge/system_prompt.md` | +10 | task skill note |

LOC total: ~3325 (new ~2050, mods ~275, tests ~1000).

### 13.3. Phase-4/5 import shim

Same `sys.path.append(<root>/src)` pattern. `_memlib` consolidation остаётся deferred phase 9.

---

## 14. Testing plan

### 14.1. Unit (~440 LOC, ~25 tests)

- test_subagent_store.py: CRUD + status-precondition + claim FIFO + race + recover_orphans
- test_subagent_types.py: registry + narrowing
- test_subagent_dispatcher.py: format + throttle
- test_task_cli.py: all subcommands + edge cases + cap + depth

### 14.2. Integration (~530 LOC, ~15 tests)

- test_subagent_pool.py: spawn 5 with cap=2, FIFO, fail, timeout, cancel shield, fresh conv_id, bridge factory called
- test_subagent_recovery.py: running → recover; interrupted → recover; flock guard; SIGTERM mid-job
- test_subagent_hook_allowlist.py: 10 allow/deny cases
- test_subagent_e2e.py: spawn → notify; cancel mid-run; 5 parallel pool=2 limit

### 14.3. Failure modes covered

SDK timeout, CancelledError propagate, parent delete cascade, worker exception logged not killing pool, claim_pending DB error retry.

---

## 15. Security concerns

1. Task injection: owner-controlled, single-user — accept.
2. Subagent Bash: same hooks apply.
3. allowed_tools narrowing only.
4. Env leak: just integer.
5. DB exhaustion: max_pending=64 cap.
6. OAuth rate-limit: pool=4 default.

---

## 16. Open questions for orchestrator Q&A

| # | Question | Recommended |
|---|---|---|
| 1 | Pool size | 4 |
| 2 | Conv isolation | Fresh conv_id |
| 3 | Built-in kinds | general+worker+researcher (3) |
| 4 | Notify format | Concise + footer |
| 5 | Cancel API | CLI-only |
| 6 | Recursion | Allowed, depth=3 |
| 7 | OAuth sharing | Shared (verify spike) |
| 8 | Job retention | Forever in phase 6 |
| 9 | task skill | Always available |
| 10 | Auto-retry | NO |
| 11 | Schema version | v4 |
| 12 | Bridge reuse | Fresh per job |

---

## 17. Dependencies

- Phase 2: ClaudeBridge (new ask_subagent method)
- Phase 3: _BASH_PROGRAMS dispatch
- Phase 4: sys.path.append shim
- Phase 5: _spawn_bg, ConversationStore.lock, IncomingMessage, send_text retry, flock, status-precondition SQL, asyncio.shield + _pending_updates, Daemon.stop ordering
- Phase 7+ future: register media-specific kinds

---

## 18. Tech debt deferred

1. _memlib consolidation (phase 9)
2. HISTORY snippet cap (phase 9)
3. Subagent health-check watchdog (phase 9)
4. Admin panel listing (phase 9)
5. Job retention sweeper (phase 9)
6. Skill-declared subagent_kind frontmatter (phase 7)
7. Streaming progress mid-run (phase 9)

---

## 19. Risk register

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | SDK OAuth concurrency unknown | 🔴 | S-6-1 BLOCKER spike |
| 2 | CancelledError leaves zombie subprocess | 🔴 | S-6-2 + finally aclose |
| 3 | Recursive infinite spawn | 🟡 | depth cap=3 |
| 4 | Unbounded pending | 🟡 | MAX_PENDING=64 |
| 5 | Notify burst spam | 🟡 | 500ms throttle |
| 6 | Shutdown mid-job orphan | 🟡 | recover_orphans on boot |
| 7 | OAuth rate-limit at pool 8+ | 🟢 | env override |
| 8 | SDK upgrades change semantics | 🟢 | roll-our-own insulation |
| 9 | Bridge shared _sem bottleneck | 🟡 | ask_subagent bypasses _sem |
| 10 | Exception doesn't reach terminal | 🟡 | every branch marks status + notify |
| 11 | Large result overflows Telegram | 🟢 | existing split_for_telegram |
| 12 | Parent deletion orphans children | 🟢 | FK ON DELETE SET NULL |

---

## 20. Invariants

1. At-most-N concurrent running jobs (N = pool_size)
2. depth ≤ max_depth enforced at spawn
3. Status transitions guarded by SQL precondition
4. Each job has its own conversation_id
5. notify_terminal called exactly once per terminal
6. recover_orphans runs ONCE at boot before pool starts
7. Daemon.stop drains pending_updates before conn.close
8. Subagent's Bash goes through same PreToolUse hook
9. Fresh ClaudeBridge per job
10. cancel_requested observed within ≤ 1 SDK block

---

## 21. Skeptical notes

- Poll-based claim 1s — adds 1s start delay, acceptable
- Shared _sem bypass: ask_subagent owns concurrency, not main session sem
- Job retention forever: 1KB row × 10k jobs = 10MB, acceptable
- Depth via env: requires SDK env propagation; PID-file fallback
- Notify on cancelled: minor chatter, but explicit confirmation
- interrupted recovery = full re-run: jobs idempotent by convention
- ask_subagent duplicates ask: 80% overlap, refactor in phase 9
- Shared aiosqlite: phase-5 proved p99 3.4ms, plenty of headroom

---

## 22. Summary & exit checklist

Phase 6 delivers universal background subagent pool, foundational for phases 7-9.

Exit:
- [ ] All 12 Q&A answered
- [ ] Spikes S-6-1 + S-6-2 PASS
- [ ] Migration v4 applied
- [ ] 30+ new tests passing (~900 total)
- [ ] mypy strict clean
- [ ] Phase-5 invariants preserved
- [ ] skills/task/SKILL.md in manifest
- [ ] E2E spawn → notify verified
- [ ] Daemon.stop mid-job clean

---

### Critical Files for Implementation

- /Users/agent2/Documents/0xone-assistant/src/assistant/main.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/claude.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/hooks.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/state/db.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/config.py
