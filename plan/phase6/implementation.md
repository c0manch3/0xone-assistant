# Phase 6 — Implementation (spike-verified, v2, 2026-04-17)

Thin layer над SDK-native primitives `AgentDefinition` +
`SubagentStart/SubagentStop` hooks. Все ключевые допущения плана
прошли через S-6-0 + wave-2 re-probes (см. `plan/phase6/spike-findings.md`,
`spikes/phase6_s0_report.json`, `spikes/phase6_s1_contextvar_report.json`,
`spikes/phase6_s2_sandbox_report.json`).

## Revision history

- **v1** (2026-04-18, after S-6-0): initial coder-ready spec.
- **v2** (2026-04-17, researcher fix-pack — wave-2):
  - B-W2-1 Q1 re-run with explicit `background=True`/`False` confirms
    FAIL (no difference); pitfall #5 stands.
  - B-W2-2 `last_assistant_message` not in SDK TypedDict → add SDK
    version pin assertion + honest JSONL fallback with 250 ms retry.
  - B-W2-3 `sdk_agent_id` becomes NULLable with partial UNIQUE index
    (option A) — pending rows carry NULL until picker dispatches.
  - B-W2-4 ContextVar propagation empirically PASS (S-1) → picker
    correlates pending request → Start hook via `ContextVar`.
  - B-W2-5 subagent Bash empirically hits parent's PreToolUse sandbox
    with `agent_id` populated (S-2, 5/5 denied) → no phase-3 regression.
  - B-W2-6 picker bridge instance is SEPARATE from user-chat bridge →
    no semaphore contention; Q6 cross-bridge PASS guarantees shared
    SubagentStop hook still notifies.
  - B-W2-7 recovery adds `'requested'` status for pre-picker rows +
    1-hour stale drop.
  - B-W2-8 `_GLOBAL_BASELINE` conditionally extended with `"Task"` when
    `self._agents` present (option A); unit test locks the shape.
  - GAP #9 `sdk_session_id` column dropped (asymmetry confirmed in raw).
  - GAP #10 Q4 wording softened: "empirically not observed with these
    tool lists" (not "structurally impossible"); regression test added.
  - GAP #11 `cost_usd` remains NULL for phase 6 with documented reason;
    `TaskNotificationMessage.usage` lookup deferred to phase-9.
  - GAP #12 hook `await shield` pattern flipped — hook returns `{}`
    immediately, shielded delivery task registered to
    `pending_updates`; Daemon drain awaits.
  - GAP #13 cancel-orphan post-restart — document ps-sweep + boot warn.
  - GAP #14 `_validate_task_argv` full spec inline (mirrors schedule).
  - GAP #15 throttle dict bounded to 64 entries with LRU eviction.
  - GAP #16 v4 migration `PRAGMA user_version = 4` comment on
    forward-compat (no column rename; add-only).
  - GAP #17 picker-starvation integration test added to §8.2.
  - GAP #18 `test_subagent_e2e.py` gated by `RUN_SDK_INT=1` environment
    variable (same pattern as phase-5 `tests/test_scheduler_e2e_sdk.py`).

Empirical backing:
- `spikes/phase6_s0_native_subagent.py` — одна orchestrator-script, 8
  subtests + 5 cheap probes + wave-2 `test_q1_background_compare`.
- `spikes/phase6_s0_report.json` — raw observations (includes
  `q1_background_compare`).
- `spikes/phase6_s1_contextvar_hook.py` — ContextVar hook propagation
  (wave-2 B-W2-4).
- `spikes/phase6_s2_subagent_sandbox.py` — subagent PreToolUse sandbox
  traversal (wave-2 B-W2-5, CRITICAL security probe).
- `plan/phase6/spike-findings.md` — verdicts and per-Q analysis.

Companion docs (coder **must** read before starting):
- `plan/phase6/description.md` — E2E scenarios.
- `plan/phase6/detailed-plan.md` — canonical spec §1–§20.
- `plan/phase5/implementation.md` — style precedent.
- `plan/phase5/summary.md` — phase-5 invariants (shared lock, shield,
  drain order, flock, LRU, status-precondition SQL).

**Auth:** OAuth via `claude` CLI (`~/.claude/`). No `ANTHROPIC_API_KEY`.
Subagents inherit the parent's auth via SDK.

---

## 0. Pitfall box (MUST READ) — updated wave-2

Things the coder absolutely MUST NOT do. Each item either comes from
S-6-0 / S-1 / S-2 (spike evidence) or phase-5 hard-won lessons (see
`plan/phase5/summary.md` §7).

1. **DO NOT** rely on `SubagentStopHookInput["last_assistant_message"]`
   as a contract — it is NOT declared on the SDK TypedDict (see
   `.venv/.../claude_agent_sdk/types.py:309-316`; wave-2 confirmed grep
   returns zero matches for `last_assistant_message` in the SDK
   package). On SDK 0.1.59 + CLI 2.1.114 the field IS present at runtime
   with the full final text. Implementation: read `raw.get("last_assistant_message")`
   FIRST; on empty/missing, retry once after a 250 ms sleep against the
   JSONL at `agent_transcript_path` (the fallback handles both a
   future SDK dropping the field AND the real race where v1 analyser
   saw `assistant_blocks_in_transcript=[0]` at hook-fire). Also: startup
   assertion `assert claude_agent_sdk.__version__ == "0.1.59"` with a
   loud log-only downgrade path (do not crash) if the pin fails — see
   §3.12 `Daemon.start`.
2. **DO NOT** add a "depth cap deny" handler in `SubagentStart` that
   returns `additionalContext`. S-6-0 Q4: empirically no recursion
   observed when the child's `tools` omits `"Task"`. Lock with a
   regression test (`test_subagent_no_recursion_when_task_absent`) that
   runs the real S-6-0 Q4 prompt and asserts exactly one distinct
   `agent_id` across all Start events. The wording "structurally
   impossible" was softened in wave-2 — future SDKs may change this.
3. **DO NOT** rely on `main_task.cancel()` to kill a subagent. S-6-0 Q7:
   orphan. Use PreToolUse flag-poll: hook reads `cancel_requested=1` from
   `subagent_jobs` keyed on `agent_id` → returns deny → subagent
   stack unwinds on its next tool call. If subagent uses NO tools, flag
   has no effect. Accept and document. **Post-restart ps-sweep** (GAP
   #13): on Daemon.stop, iterate `ps aux | grep claude` (stdlib
   subprocess, no extra deps) and warn if orphan PIDs; on Daemon.start,
   emit a one-line warn if `recover_orphans` transitioned any rows.
4. **DO NOT** key the ledger on `session_id` from SubagentStart hook.
   S-6-0 Q12 raw: `all_equal=true` across Start events — on Start,
   `session_id` is the parent's session; on Stop, `session_id` is the
   subagent's own session. Asymmetric. Key `subagent_jobs.sdk_agent_id`
   (NULLable, partial UNIQUE — see §3.1 wave-2). **GAP #9:**
   `sdk_session_id` column kept in schema but NOT used for matching —
   stored purely for forensic access.
5. **DO NOT** expect main `query()` iterator to finish "in ~3 sec" for
   E2E scenario 1. S-6-0 Q1 AND wave-2 Q1-BG re-run BOTH show main
   `ResultMessage` arriving AFTER subagent `TaskNotificationMessage`.
   `background=True` flag has NO observable effect on SDK 0.1.59. The
   Task tool is a synchronous RPC within the parent's turn — main
   `query()` stays open until the child ends. Notify pushes through the
   hook during child's stop, so the OWNER sees the result via Telegram
   at child completion; main turn reply is secondary. **Keep
   `background=True` in `AgentDefinition` for forward-compat** but base
   design on "main turn wall ≈ subagent wall".
6. **DO NOT** attach `"Task"` tool to the subagent's own `tools` list
   (= prevents recursion). Keep `"Task"` only in MAIN turn's
   `allowed_tools`. Regression test locks this (pitfall #2).
7. **DO NOT** `await asyncio.shield(task)` inside the hook body — that
   blocks the SDK iterator. **GAP #12 wave-2 fix:** hook registers the
   delivery task in the shared `pending_updates: set` and returns `{}`
   immediately. `Daemon.stop()` drains `pending_updates` with a
   timeout. See §3.8 `on_subagent_stop`.
8. **DO NOT** open a second aiosqlite connection for `SubagentStore`.
   Reuse `ConversationStore.conn` + `ConversationStore.lock` — phase-5
   pattern (S-1 verified contention).
9. **DO NOT** UPDATE `subagent_jobs.status` without a `WHERE status=...`
   precondition. Status-machine invariants enforced by SQL, not by
   Python (phase-5 G-W2-6). `rowcount=0` → log skew, don't raise.
10. **DO NOT** treat `SubagentStop` as at-least-once like phase-5
    triggers. Each subagent fires start + stop exactly once per SDK
    contract (S-6-0 confirms 1:1 in all observed runs). Partial UNIQUE
    on `sdk_agent_id WHERE sdk_agent_id IS NOT NULL` handles the edge
    case where a hook somehow fires twice for the same agent_id while
    allowing multiple pending (`sdk_agent_id IS NULL`) rows.
11. **DO NOT** import `claude_agent_sdk.HookMatcher` at module top of
    `subagent/hooks.py` — follow `bridge/hooks.py::make_pretool_hooks`
    pattern (lazy import inside factory). Keeps validator modules pure.
12. **DO NOT** block inside hook callbacks for more than ~500 ms. Hooks
    run inside the SDK's iterator loop; stalling them blocks the main
    `query()` iterator. The hook only INSERTs the ledger row + creates
    the shielded delivery task and RETURNS — drain runs in the parent
    event loop outside the hook.
13. **DO NOT** pass `allowed_tools=[...]` without `"Task"` to the main
    turn's `ClaudeAgentOptions`. S-6-0 Q2: we included it explicitly and
    observed usage; omitting it is untested. **Wave-2 B-W2-8:** extend
    `_GLOBAL_BASELINE` to include `"Task"` ONLY when `self._agents` is
    non-empty (see §3.10). Unit test `test_allowed_tools_includes_task_when_agents_registered`.
14. **DO NOT** use `setting_sources=["project"]` and expect subagents to
    inherit only a subset of project settings. S-6-0 did not verify
    which `settings.json` policies flow to subagents. Document: subagent
    runs under same settings as parent. Narrow via `AgentDefinition.tools` /
    `disallowedTools` if needed.
15. **DO NOT** emit the notify footer with `kind=<raw agent_type>` if
    `agent_type` contains non-Telegram-safe chars. `_format_notification`
    must Markdown-escape kind/status inline.
16. **DO NOT** use a shared `ClaudeBridge` instance for both user turns
    and `SubagentRequestPicker` dispatch (wave-2 B-W2-6). Each bridge
    has its own `asyncio.Semaphore(max_concurrent)`; a picker flood can
    starve user-chat turns. `Daemon.start` builds TWO bridges sharing
    the SAME `extra_hooks` + `agents` dict: `bridge` (for
    `ClaudeHandler`) and `picker_bridge` (for `SubagentRequestPicker`).
    Q6 PASS guarantees cross-bridge SubagentStop still fires, so notify
    works from either bridge. See §3.12.
17. **DO NOT** assume subagent Bash bypasses phase-3 sandbox. **S-2
    wave-2 verified:** all 5 subagent Bash calls hit parent's
    `make_pretool_hooks` PreToolUse callback with `agent_id` populated
    and were denied by real phase-3 validators. Cite S-2 in the
    commit-6 test suite — don't delete phase-3 hooks expecting
    duplication.

---

## 1. Commit plan (7 commits)

Each commit is a logical unit, tests-first where useful, all under
~500 LOC diff. Coder runs `just lint && uv run pytest -x` before each.

| # | Title | New files | Edit | LOC |
|---|-------|-----------|------|-----|
| 1 | schema v4 + migration 0004 | `state/migrations/0004_subagent.sql`, `tests/test_db_migrations_v4.py` | `state/db.py` (+15) | ~80 |
| 2 | `subagent/definitions.py` — AgentDefinition registry + tests | `subagent/__init__.py`, `subagent/definitions.py`, `tests/test_subagent_definitions.py` | `config.py` (+20: `SubagentSettings`) | ~200 |
| 3 | `SubagentStore` (aiosqlite) + status-precondition SQL + tests | `subagent/store.py`, `tests/test_subagent_store.py` | — | ~400 |
| 4 | `subagent/hooks.py` — SubagentStart/Stop + notify-format + cancel PreToolUse + tests | `subagent/hooks.py`, `subagent/format.py`, `tests/test_subagent_hooks.py`, `tests/test_subagent_format.py` | — | ~450 |
| 5 | CLI `tools/task/main.py` + bash hook gate + skill + tests | `tools/task/main.py`, `skills/task/SKILL.md`, `tests/test_task_cli.py`, `tests/test_task_bash_hook.py` | `bridge/hooks.py` (+80: `_validate_task_argv` + dispatch + dup-flag) | ~500 |
| 6 | `SubagentRequestPicker` + `ClaudeBridge.extra_hooks` + `bridge/claude.py` wiring + tests | `subagent/picker.py`, `tests/test_subagent_picker.py` | `bridge/claude.py` (+30) | ~250 |
| 7 | Daemon integration (orphan recovery + hook factory wiring + drain + health) + E2E | `tests/test_subagent_recovery.py`, `tests/test_subagent_e2e.py` | `main.py` (+70), `bridge/system_prompt.md` (+5) | ~300 |

Total: ~2180 LOC (of which tests ~700, source ~1480). Under
`detailed-plan.md` §15.2 budget (~1300 LOC source).

### Commit messages (suggested)

```
phase 6: schema v4 subagent_jobs ledger
phase 6: AgentDefinition registry + SubagentSettings
phase 6: SubagentStore + status-precondition SQL
phase 6: SubagentStart/Stop hooks + cancel flag-poll PreToolUse
phase 6: tools/task CLI + bash gate + task skill
phase 6: SubagentRequestPicker + ClaudeBridge extra_hooks
phase 6: Daemon integration + orphan recovery + drain
```

---

## 2. Test-first order

For commits 3 and 4, tests FIRST:

- **Commit 3** (`store.py`): state-machine transitions + status-precondition
  → `test_subagent_store.py` (insert_started, record_finished,
  idempotent duplicate sdk_agent_id, set_cancel_requested, recover_orphans,
  status-skew on racing UPDATE).
- **Commit 4** (`hooks.py`): mock SubagentStart/StopHookInput dicts →
  assert DB row inserted, adapter.send_text called with formatted text,
  shield on cancel.

For commits 1, 2, 5, 6, 7: implementation + tests together.

---

## 3. Per-file signature specs

### 3.1 `src/assistant/state/migrations/0004_subagent.sql` (commit 1) — updated wave-2

**Wave-2 changes (B-W2-3, B-W2-7, GAP #9, GAP #16):**
- `sdk_agent_id` is now `TEXT` (nullable) with a **partial UNIQUE index**
  `WHERE sdk_agent_id IS NOT NULL` — lets CLI-pre-created rows carry
  NULL until the picker dispatches; UNIQUE still prevents double-fire.
- New status `'requested'` for CLI-pre-created rows (picker hasn't
  claimed yet). State machine: `requested → started → (completed |
  failed | stopped | interrupted | error | dropped)`.
- `sdk_session_id` column kept but NOT used for matching (GAP #9 — it
  is asymmetric between Start/Stop hooks). Stored for forensic access
  only.
- Add-only migration; future column bumps go in `0005_*.sql` (GAP #16
  forward-compat comment on PRAGMA line).

```sql
-- 0004_subagent.sql — phase 6 (SDK-native subagent ledger, wave-2)

CREATE TABLE IF NOT EXISTS subagent_jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    -- sdk_agent_id is NULL for pre-picker rows; filled by Start hook /
    -- picker.update_sdk_agent_id. Partial UNIQUE index below prevents
    -- duplicates only among non-NULL values.
    sdk_agent_id      TEXT,
    sdk_session_id    TEXT,                             -- subagent's own session_id (from Stop hook) — forensic only, see GAP #9
    parent_session_id TEXT,                             -- parent session_id (from Start hook)
    agent_type        TEXT    NOT NULL,                 -- 'general' | 'worker' | 'researcher'
    task_text         TEXT,                             -- populated for CLI/picker flow; NULL for native-Task main-turn spawn
    transcript_path   TEXT,                             -- agent_transcript_path from Stop hook
    -- status machine: requested → started → (completed|failed|stopped|interrupted|error|dropped)
    status            TEXT    NOT NULL DEFAULT 'started',
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    result_summary    TEXT,                             -- first 500 chars of last_assistant_message
    cost_usd          REAL,                             -- nullable; reserved for phase-9 accounting (GAP #11)
    callback_chat_id  INTEGER NOT NULL,                 -- always OWNER_CHAT_ID for phase 6
    spawned_by_kind   TEXT    NOT NULL,                 -- 'user' | 'scheduler' | 'cli'
    spawned_by_ref    TEXT,                             -- schedule_id on scheduler spawns; null otherwise
    depth             INTEGER NOT NULL DEFAULT 0,       -- always 0 in phase 6 (see pitfall #2 + regression test)
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    started_at        TEXT,                             -- set on SubagentStart hook fire
    finished_at       TEXT                              -- set on SubagentStop hook fire
);

-- Partial UNIQUE — only non-NULL sdk_agent_id values must be unique
-- (SQLite 3.8+ syntax). Pending CLI rows carry NULL with no conflict.
CREATE UNIQUE INDEX IF NOT EXISTS idx_subagent_jobs_sdk_agent_id_uq
    ON subagent_jobs(sdk_agent_id) WHERE sdk_agent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_subagent_jobs_status_started ON subagent_jobs(status, started_at);
CREATE INDEX IF NOT EXISTS idx_subagent_jobs_status_created ON subagent_jobs(status, created_at);

-- Forward compat (GAP #16): future schema bumps use 0005_*.sql; do not
-- rename columns here — add-only.
PRAGMA user_version = 4;
```

Allowed `status` values: `'requested'`, `'started'`, `'completed'`,
`'failed'`, `'stopped'`, `'interrupted'`, `'error'`, `'dropped'`.

### 3.2 `src/assistant/state/db.py` edits (commit 1)

Insert after `_apply_v3` (~line 85):

```python
async def _apply_v4(conn: aiosqlite.Connection) -> None:
    sql = (_MIGRATIONS_DIR / "0004_subagent.sql").read_text(encoding="utf-8")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.executescript(sql)
        await conn.execute("PRAGMA user_version = 4")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
```

Bump `SCHEMA_VERSION = 4`. Wire into `apply_schema`:

```python
    if current < 4:
        await _apply_v4(conn)
        current = 4
```

### 3.3 `src/assistant/config.py` edits (commit 2)

Add `SubagentSettings` class (mirror shape of `SchedulerSettings`).

```python
class SubagentSettings(BaseSettings):
    """Phase-6 subagent pool knobs.

    Intentionally small — SDK manages lifecycle; we only tune ledger
    retention and notify behaviour.
    """

    model_config = SettingsConfigDict(env_prefix="ASSISTANT_SUBAGENT_")

    enabled: bool = True
    # Telegram notify throttle between consecutive subagent notifications
    # for the SAME chat. Keeps us below the 30-msg/sec global cap even if
    # 10 subagents complete in the same second.
    notify_throttle_ms: int = 500
    # Max bytes of subagent's final assistant message injected as notify body
    # BEFORE Telegram chunker splits. Phase-5 chunker handles >4096 chars fine;
    # cap prevents pathological 1 MB output from flooding the UI.
    result_body_max_bytes: int = 32_768
    # maxTurns for each kind; S-6-0 Q1 showed that longer maxTurns extends
    # wallclock in ways the owner can't observe mid-turn.
    max_turns_general: int = 20
    max_turns_worker: int = 5
    max_turns_researcher: int = 15
    # Grace window for Daemon.stop to drain in-flight notify tasks.
    drain_timeout_s: float = 2.0
    # Picker tick interval for CLI-spawn pickup.
    picker_tick_s: float = 1.0
```

Wire into `Settings`:

```python
class Settings(BaseSettings):
    ...
    subagent: SubagentSettings = Field(default_factory=SubagentSettings)
```

### 3.4 `src/assistant/subagent/__init__.py` (commit 2)

Minimal:

```python
"""Phase-6 subagent package — SDK-native thin layer."""
from assistant.subagent.definitions import build_agents
from assistant.subagent.store import SubagentJob, SubagentStore

__all__ = ["SubagentJob", "SubagentStore", "build_agents"]
```

### 3.5 `src/assistant/subagent/definitions.py` (commit 2)

```python
"""Per-kind AgentDefinition registry.

S-6-0 Q9 confirmed `AgentDefinition.prompt` is FULL system prompt
(not appended); every kind below includes its own voice + constraints.

Q4 confirmed: recursion is gated by child's `tools` list. We OMIT "Task"
from every kind's `tools` → hard depth cap at 1 without runtime guard.

Q10 confirmed: `model="inherit"` is runtime-valid.
"""
from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import AgentDefinition

from assistant.config import Settings

_GENERAL_PROMPT = """\
You are a background subagent spawned by 0xone-assistant.
Your task is provided in the initial user message.
You do NOT have direct access to the owner. Your final assistant text
is delivered to them via Telegram verbatim.

Rules:
- Complete proactively. Do not ask clarifying questions.
- Reply with the FINAL result as your last assistant message.
- Be concise unless the task explicitly asks for long form.
- Use only the tools in your allowed list.

Environment:
- Project root: {project_root}
- Vault: {vault_dir}
"""

_WORKER_PROMPT = """\
You are a worker subagent. Execute a single CLI invocation or tightly
scoped tool sequence. Report the tool's result and stop. Do not explore
beyond the task.
"""

_RESEARCHER_PROMPT = """\
You are a research subagent. Use Read/Grep/Glob/WebFetch to gather
information. Produce a concise structured summary. Do not modify files.
Your summary is delivered to the owner verbatim.
"""


def build_agents(settings: Settings) -> dict[str, AgentDefinition]:
    """Return the per-kind AgentDefinition registry.

    S-6-0 pitfall #2: none of these include "Task" in `tools` — that
    enforces depth cap structurally. If phase-7 wants recursion,
    whitelist `"Task"` per-kind deliberately.
    """
    pr = settings.project_root
    vault = settings.vault_dir
    sub = settings.subagent

    base_fmt: dict[str, str] = {
        "project_root": str(pr),
        "vault_dir": str(vault),
    }

    return {
        "general": AgentDefinition(
            description="Generic background task: long writing, multi-step reasoning",
            prompt=_GENERAL_PROMPT.format(**base_fmt),
            tools=["Bash", "Read", "Write", "Edit", "Grep", "Glob", "WebFetch"],
            model="inherit",
            maxTurns=sub.max_turns_general,
            background=True,
            permissionMode="default",
        ),
        "worker": AgentDefinition(
            description="Run a single CLI tool and report its output",
            prompt=_WORKER_PROMPT,
            tools=["Bash", "Read"],
            model="inherit",
            maxTurns=sub.max_turns_worker,
            background=True,
            permissionMode="default",
        ),
        "researcher": AgentDefinition(
            description="Read-only research and summarisation",
            prompt=_RESEARCHER_PROMPT,
            tools=["Read", "Grep", "Glob", "WebFetch"],
            model="inherit",
            maxTurns=sub.max_turns_researcher,
            background=True,
            permissionMode="default",
        ),
    }
```

### 3.6 `src/assistant/subagent/store.py` (commit 3) — updated wave-2

Signatures (full implementation follows phase-5 `SchedulerStore` style,
all mutations under `async with self._lock`). Wave-2 changes: picker
methods, recovery by age bucket, `claim_pending_request` simplified
(sdk_agent_id stays NULL until Start hook fires with real id).

```python
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path

import aiosqlite

@dataclass(frozen=True)
class SubagentJob:
    id: int
    sdk_agent_id: str
    sdk_session_id: str | None
    parent_session_id: str | None
    agent_type: str
    task_text: str | None
    transcript_path: str | None
    status: str
    cancel_requested: bool
    result_summary: str | None
    cost_usd: float | None
    callback_chat_id: int
    spawned_by_kind: str
    spawned_by_ref: str | None
    depth: int
    created_at: str
    started_at: str | None
    finished_at: str | None


class SubagentStore:
    """aiosqlite ledger for SDK-native subagent jobs.

    Reuses `ConversationStore.conn` + `ConversationStore.lock` (S-6-0
    pitfall #8 / phase-5 S-1). All mutations `async with lock`.

    Status machine:
        requested → started → (completed | failed | stopped |
                               interrupted | error | dropped)
    `stopped` only if cancel_requested=1 at terminal time.
    `dropped` only from `requested` via recover_orphans (>1h stale).
    """

    def __init__(self, conn: aiosqlite.Connection, *, lock: asyncio.Lock) -> None: ...

    # ---------- INSERT ----------

    async def record_pending_request(
        self,
        *,
        agent_type: str,
        task_text: str,
        callback_chat_id: int,
        spawned_by_kind: str,             # 'cli' | 'scheduler'
        spawned_by_ref: str | None = None,
    ) -> int:
        """INSERT a pre-picker request row.

        Used by `tools/task/main.py spawn` — row has `status='requested'`
        AND `sdk_agent_id IS NULL` (partial UNIQUE tolerates). Picker
        consumes these via `list_pending_requests` + `claim_pending_request`.

        Returns the auto-increment id so CLI prints `{"job_id": N, "status": "pending"}`.
        """

    async def record_started(
        self,
        *,
        sdk_agent_id: str,
        agent_type: str,
        parent_session_id: str | None,
        callback_chat_id: int,
        spawned_by_kind: str,
        spawned_by_ref: str | None,
    ) -> int:
        """INSERT a NEW row for on_subagent_start hook fire (native Task
        spawn, no pre-picker pending row). Status='started'. Caller
        calls `update_sdk_agent_id_for_claimed_request` INSTEAD of this
        method when the hook is known to match a picker-claimed row
        (via ContextVar correlation — see §3.9).

        SQL: plain `INSERT`. Partial UNIQUE blocks double-start on the
        same `sdk_agent_id` — on `IntegrityError` log skew and return
        the existing row's id.
        """

    async def update_sdk_agent_id_for_claimed_request(
        self,
        *,
        job_id: int,
        sdk_agent_id: str,
        parent_session_id: str | None,
    ) -> bool:
        """Status-precondition UPDATE: set sdk_agent_id + status='started'
        + parent_session_id + started_at=NOW
        WHERE id=? AND status='requested'.

        Returns True on row updated. Used by on_subagent_start when
        ContextVar says "this is a picker-claimed request".
        """

    async def record_finished(
        self,
        *,
        sdk_agent_id: str,
        status: str,                         # 'completed' | 'failed' | 'stopped' | 'error'
        result_summary: str | None,
        transcript_path: str | None,
        sdk_session_id: str | None,
        cost_usd: float | None = None,
    ) -> None:
        """UPDATE ... WHERE sdk_agent_id=? AND status='started'.

        rowcount=0 → log skew, don't raise (pitfall #9). Sets
        finished_at=NOW.
        """

    # ---------- cancel ----------

    async def set_cancel_requested(self, job_id: int) -> dict[str, str | bool]:
        """UPDATE cancel_requested=1 WHERE id=? AND status IN ('requested','started').

        Returns `{"cancel_requested": True, "previous_status": <str>}`
        or `{"already_terminal": <str>}`. A `requested`-status row that
        is cancelled before the picker picks it up transitions to
        `dropped` at the next recover_orphans pass OR at picker claim
        time (picker MUST check `cancel_requested` before dispatch).
        """

    async def is_cancel_requested(self, sdk_agent_id: str) -> bool:
        """SELECT cancel_requested FROM subagent_jobs WHERE sdk_agent_id=?.

        Used by PreToolUse flag-poll hook (S-6-0 Q7 fallback). Returns
        False if row missing (race: hook fires before ledger writes).
        """

    # ---------- recovery ----------

    async def recover_orphans(self, *, stale_requested_after_s: int = 3600) -> dict[str, int]:
        """Four-branch transition run ONCE at Daemon.start BEFORE picker
        or bridge accept new turns. Returns counts per branch.

          * `status='started' AND finished_at IS NULL` → `'interrupted'`
            (prior daemon crashed mid-subagent-run).
          * `status='requested' AND created_at < now - stale_requested_after_s`
            → `'dropped'` (CLI insert never picked up before restart).
          * `status='requested' AND created_at >= now - stale_requested_after_s`
            → **leave as-is** (picker will pick up after start-up).
          * `status='started' AND sdk_agent_id IS NULL`
            → `'dropped'` (defensive — should not occur; picker claim
            never patched agent_id before crash).

        Returns `{"interrupted": N1, "dropped": N2}`. Caller
        (Daemon.start) emits a single notify summarising both.
        """

    # ---------- queries ----------

    async def get_by_agent_id(self, sdk_agent_id: str) -> SubagentJob | None: ...
    async def get_by_id(self, job_id: int) -> SubagentJob | None: ...
    async def list_jobs(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[SubagentJob]: ...
    async def list_pending_requests(self, limit: int = 10) -> list[SubagentJob]:
        """SubagentRequestPicker source: rows with `status='requested'`
        AND `sdk_agent_id IS NULL`. Oldest `created_at` first."""

    async def claim_pending_request(self, job_id: int) -> bool:
        """No-op status change: keep `status='requested'` but set a claim
        marker so a future picker instance (e.g. after mid-claim crash)
        can tell the row is being worked on.

        Simpler approach for single-daemon flock world: the picker just
        calls `list_pending_requests(limit=1)` and then invokes the
        bridge. When the Start hook fires with the ContextVar set (§3.9),
        it calls `update_sdk_agent_id_for_claimed_request(job_id, ...)`
        to flip status to `'started'`. If the daemon crashes between
        claim and Start, the row stays `'requested'` and recover_orphans
        handles it (1-hour bucket).

        We include this method with the "no-op" semantics (returns
        True) so §3.9 picker code reads cleanly; tests can assert it
        exists and is idempotent.
        """
```

### 3.7 `src/assistant/subagent/format.py` (commit 4)

Pure-function module. ~50 LOC.

```python
def format_notification(
    *,
    result_text: str,
    job: SubagentJob,
    max_body_bytes: int,
) -> str:
    """Render the final Telegram notify body.

    Footer format locked by plan Q4:
        [job {id} {status} in {duration_s}s, kind={kind}, cost=${cost}]

    Reference: plan/phase6/description.md §criteria.
    """
    # Truncate result_text to max_body_bytes first (bytes, not chars).
    body = result_text.strip()
    body_bytes = body.encode("utf-8")
    if len(body_bytes) > max_body_bytes:
        body = body_bytes[:max_body_bytes].decode("utf-8", errors="ignore").rstrip()
        body += "\n\n[truncated]"

    duration = _compute_duration_s(job)
    cost = f"${job.cost_usd:.4f}" if job.cost_usd is not None else "$?"
    footer = (
        f"\n\n---\n"
        f"[job {job.id} {job.status} in {duration:.0f}s, "
        f"kind={job.agent_type}, cost={cost}]"
    )
    return body + footer


def _compute_duration_s(job: SubagentJob) -> float:
    """Parse started_at / finished_at ISO strings; return seconds.
    Both should be present at notify time; fallback to 0.0."""
```

### 3.8 `src/assistant/subagent/hooks.py` (commit 4) — updated wave-2

Factory shape mirrors `bridge/hooks.py::make_pretool_hooks`. Returns a
dict keyed by hook event name, each value a list of `HookMatcher`.

**Wave-2 changes:**
- Hook returns `{}` immediately; delivery runs as a shielded bg task
  registered on `pending_updates` (GAP #12). No `await shield` inside
  the hook body.
- `on_subagent_start` reads `CURRENT_REQUEST_ID` ContextVar (S-1 PASS).
  If set, it calls `update_sdk_agent_id_for_claimed_request(job_id, ...)`
  INSTEAD of `record_started` (B-W2-4 implementation).
- Throttle dict bounded at 64 entries with simple LRU eviction (GAP #15
  — we always have `callback_chat_id == OWNER_CHAT_ID` in phase 6, so
  the dict effectively has 1 entry, but the bound is defensive against
  future multi-chat).
- Cancel-gate PreToolUse hook reads `raw.get("agent_id")` directly; SDK
  types.py explicitly documents this field is populated when the hook
  fires inside a subagent (S-2 wave-2 confirmed 5/5 fires had
  `agent_id`).

```python
"""SubagentStart + SubagentStop + cancel-flag PreToolUse hooks (wave-2).

S-6-0 + wave-2 verifications:
  * Q5: `SubagentStopHookInput["last_assistant_message"]` — primary
    result carrier (but NOT in SDK TypedDict; see pitfall #1). JSONL
    transcript with 250 ms retry is fallback.
  * Q6: hook factory shared across multiple ClaudeAgentOptions instances
    works — both see their subagents' events on the SAME callback.
  * Q7: cancel propagates only via this PreToolUse flag-poll.
  * S-1 (wave-2): ContextVar `CURRENT_REQUEST_ID` propagates from
    caller into on_subagent_start — used by picker for correlation.
  * S-2 (wave-2): subagent tool calls DO fire PreToolUse with
    `agent_id` populated on `input_data` (phase-3 sandbox still applies).
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, cast

from assistant.adapters.base import TelegramAdapterProtocol
from assistant.config import Settings
from assistant.logger import get_logger
from assistant.subagent.format import format_notification
from assistant.subagent.store import SubagentStore

log = get_logger("subagent.hooks")


# Picker sets this ContextVar before bridge.ask(); on_subagent_start
# reads it to correlate the Start hook fire to a pending request row.
# S-1 spike PASS (spikes/phase6_s1_contextvar_hook.py).
CURRENT_REQUEST_ID: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "phase6_current_request_id", default=None
)


def make_subagent_hooks(
    *,
    store: SubagentStore,
    adapter: TelegramAdapterProtocol,
    settings: Settings,
    pending_updates: set[asyncio.Task[Any]],
) -> dict[str, list[Any]]:
    """Build SubagentStart + SubagentStop + PreToolUse hooks.

    Pattern: import HookMatcher lazily (pitfall #11); close over `store`,
    `adapter`, `settings`, `pending_updates`. Return dict ready to merge
    into `ClaudeAgentOptions.hooks`.

    Returns keys: "SubagentStart", "SubagentStop", "PreToolUse".
    (PreToolUse entry is ONLY the cancel-flag-poll matcher, to be merged
    with the existing PreToolUse list in `ClaudeBridge._build_options`.)
    """
    from claude_agent_sdk import HookMatcher

    # Per-chat throttle, bounded LRU so a hypothetical multi-chat future
    # cannot leak memory (GAP #15). Phase 6 always uses OWNER_CHAT_ID so
    # the dict has one entry in practice.
    _last_notify_at: OrderedDict[int, float] = OrderedDict()
    _THROTTLE_MAX = 64

    async def on_subagent_start(
        input_data: Any,
        tool_use_id: str | None,
        ctx: Any,
    ) -> dict[str, Any]:
        raw = cast(dict[str, Any], input_data)
        agent_id = raw["agent_id"]
        agent_type = raw["agent_type"]
        parent_session = raw.get("session_id")

        # Wave-2 B-W2-4 + S-1: ContextVar set by SubagentRequestPicker?
        request_id = CURRENT_REQUEST_ID.get()
        if request_id is not None:
            # Picker path: patch the existing 'requested' row with the
            # real agent_id. Status flips 'requested' → 'started'.
            patched = await store.update_sdk_agent_id_for_claimed_request(
                job_id=request_id,
                sdk_agent_id=agent_id,
                parent_session_id=parent_session,
            )
            if not patched:
                log.warning(
                    "picker_request_start_mismatch",
                    request_id=request_id,
                    agent_id=agent_id,
                )
                # Fall through to record_started as defensive INSERT.
            else:
                log.info(
                    "subagent_start_picker_claimed",
                    request_id=request_id,
                    agent_id=agent_id,
                    agent_type=agent_type,
                )
                return {}

        # Native-Task spawn (main turn delegated via Task tool) or
        # picker-mismatch fallback: plain INSERT.
        try:
            await store.record_started(
                sdk_agent_id=agent_id,
                agent_type=agent_type,
                parent_session_id=parent_session,
                callback_chat_id=settings.owner_chat_id,
                spawned_by_kind="user",
                spawned_by_ref=None,
            )
        except Exception:
            log.warning("record_started_failed", agent_id=agent_id, exc_info=True)
        log.info(
            "subagent_start",
            agent_id=agent_id,
            agent_type=agent_type,
            parent_session=parent_session,
        )
        return {}

    async def on_subagent_stop(
        input_data: Any,
        tool_use_id: str | None,
        ctx: Any,
    ) -> dict[str, Any]:
        """Hook body is non-blocking. It:
          1. Reads `last_assistant_message` / JSONL fallback.
          2. UPDATEs the ledger row.
          3. SPAWNS a shielded send_text task and REGISTERS it on
             `pending_updates`.
          4. Returns `{}` — does NOT await the delivery.

        GAP #12 wave-2 change: previously awaited `asyncio.shield(task)`
        inside the hook, which blocked the SDK iterator for the full
        Telegram round-trip. Daemon.stop drains `pending_updates` with
        a 2s timeout.
        """
        raw = cast(dict[str, Any], input_data)
        agent_id = raw["agent_id"]
        transcript_path = raw.get("agent_transcript_path")
        session_id = raw.get("session_id")

        # Primary path per S-6-0 Q5 raw evidence: read runtime field.
        last_msg = raw.get("last_assistant_message") or ""
        if not last_msg and transcript_path:
            # 250 ms retry bucket — v1 analyser saw 0 assistant blocks
            # in JSONL at hook-fire time even though the hook field
            # carried text. For the fallback path we wait once and retry.
            await asyncio.sleep(0.25)
            last_msg = _read_last_assistant_from_transcript(Path(transcript_path))

        was_cancelled = await store.is_cancel_requested(agent_id)
        status = "stopped" if was_cancelled else "completed"

        try:
            await store.record_finished(
                sdk_agent_id=agent_id,
                status=status,
                result_summary=last_msg[:500] if last_msg else None,
                transcript_path=transcript_path,
                sdk_session_id=session_id,
                cost_usd=None,  # GAP #11 — deferred to phase-9.
            )
        except Exception:
            log.warning("record_finished_failed", agent_id=agent_id, exc_info=True)

        job = await store.get_by_agent_id(agent_id)
        if job is None:
            log.warning("subagent_stop_unknown_agent", agent_id=agent_id)
            return {}
        if not last_msg:
            log.warning("subagent_stop_empty_body", agent_id=agent_id)
            last_msg = "(subagent produced no final message)"

        body = format_notification(
            result_text=last_msg,
            job=job,
            max_body_bytes=settings.subagent.result_body_max_bytes,
        )

        # Register + create but do NOT await delivery (GAP #12). The
        # throttle runs inside the shielded task so back-to-back Stop
        # hooks don't block the SDK iterator.
        async def _deliver() -> None:
            await _throttle(
                _last_notify_at,
                job.callback_chat_id,
                settings.subagent.notify_throttle_ms,
                max_entries=_THROTTLE_MAX,
            )
            try:
                await asyncio.shield(
                    adapter.send_text(job.callback_chat_id, body)
                )
            except asyncio.CancelledError:
                log.info("subagent_notify_shielded_cancel", job_id=job.id)
            except Exception:
                log.warning("subagent_notify_failed", job_id=job.id, exc_info=True)

        task = asyncio.create_task(_deliver(), name=f"subagent_notify_{job.id}")
        pending_updates.add(task)
        task.add_done_callback(pending_updates.discard)
        return {}

    async def on_pretool_cancel_gate(
        input_data: Any,
        tool_use_id: str | None,
        ctx: Any,
    ) -> dict[str, Any]:
        """Cancel-flag poll for subagent-emitted tool calls.

        Wave-2 S-2 verified: PreToolUse from a subagent carries
        `agent_id` on `input_data` (SDK types.py _SubagentContextMixin).
        Main-turn calls don't have `agent_id` → hook no-ops.

        If cancelled → return `{"hookSpecificOutput":
        {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "subagent cancelled by owner"}}`.
        """
        raw = cast(dict[str, Any], input_data)
        maybe_agent_id = raw.get("agent_id")
        if not maybe_agent_id:
            return {}
        if await store.is_cancel_requested(maybe_agent_id):
            log.info("subagent_cancel_denied_tool", agent_id=maybe_agent_id)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "subagent cancelled by owner",
                },
            }
        return {}

    return {
        "SubagentStart": [HookMatcher(hooks=[on_subagent_start])],
        "SubagentStop": [HookMatcher(hooks=[on_subagent_stop])],
        "PreToolUse": [HookMatcher(hooks=[on_pretool_cancel_gate])],
    }


def _read_last_assistant_from_transcript(path: Path) -> str:
    """Fallback when `last_assistant_message` is missing/empty.

    Walks the JSONL at `path` and returns text of the LAST entry where
    `message.role == 'assistant'`. Observed shape (from S-6-0 raw Q9):
        {"parentUuid": "...", "isSidechain": true, "agentId": "...",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "..."}]},
         ...}
    """
    if not path.exists():
        return ""
    last_text = ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            msg = obj.get("message") or {}
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            content = msg.get("content") or []
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        text = str(blk.get("text") or "")
                        if text:
                            last_text = text
    except OSError:
        return last_text
    return last_text


async def _throttle(
    last_notify_at: "OrderedDict[int, float]",
    chat_id: int,
    interval_ms: int,
    *,
    max_entries: int = 64,
) -> None:
    """Module-level per-chat min-interval throttle. Non-reentrant per
    chat (single-user bot — ok). GAP #15: LRU-bounded."""
    now = time.monotonic()
    last = last_notify_at.get(chat_id, 0.0)
    delta_ms = (now - last) * 1000.0
    if delta_ms < interval_ms:
        await asyncio.sleep((interval_ms - delta_ms) / 1000.0)
    last_notify_at[chat_id] = time.monotonic()
    last_notify_at.move_to_end(chat_id)
    while len(last_notify_at) > max_entries:
        last_notify_at.popitem(last=False)
```

### 3.9 `src/assistant/subagent/picker.py` (commit 6) — updated wave-2

Mirror of `SchedulerDispatcher` — consumer loop for CLI-spawned requests.

**Wave-2 design (B-W2-4 + B-W2-6):**
- Uses its OWN `ClaudeBridge` instance (`picker_bridge`), constructed
  by Daemon.start separately from the user-chat bridge. Same
  `extra_hooks` + `agents`, but independent `asyncio.Semaphore`. Picker
  dispatches do NOT compete with user turns.
- Sets the module-level `CURRENT_REQUEST_ID` ContextVar before calling
  `picker_bridge.ask(...)`. The Start hook (inside the same event
  loop) reads the var and patches the pending row's sdk_agent_id.
- Uses `OWNER_CHAT_ID` as the chat_id argument (ask signature
  requires one); real user interaction would flow via Telegram hook,
  not the bridge directly.

```python
"""Consumer loop: poll subagent_jobs for CLI-pending requests, dispatch.

Wave-2 B-W2-4: uses `CURRENT_REQUEST_ID` ContextVar (see
`subagent/hooks.py`) so the on_subagent_start hook can correlate
the SDK-assigned agent_id with the pre-created ledger row.

Wave-2 B-W2-6: takes a dedicated `ClaudeBridge` instance — caller
(Daemon.start) MUST NOT share the user-chat bridge. See §3.12.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from assistant.bridge.claude import ClaudeBridge, ClaudeBridgeError
from assistant.config import Settings
from assistant.logger import get_logger
from assistant.subagent.hooks import CURRENT_REQUEST_ID
from assistant.subagent.store import SubagentJob, SubagentStore

log = get_logger("subagent.picker")


_PICKER_PROMPT_TEMPLATE = """\
Delegate the following task to the `{kind}` subagent using the Task tool.
After you have invoked the Task tool once (and ONLY once), reply with
exactly the word `dispatched` and stop. Do NOT wait for the subagent's
result, do NOT summarise, do NOT add any other text.

Task for the subagent:
<<<TASK>>>
{task_text}
<<<END>>>
"""


class SubagentRequestPicker:
    """Poll `subagent_jobs` for `status='requested' AND sdk_agent_id IS NULL`
    rows, dispatch each through the dedicated picker bridge.

    Lifecycle:
      - `run()` loop: `while not stop_event: sleep(picker_tick_s); process()`
      - `request_stop()` sets the stop_event for graceful shutdown.

    Invariants:
      - ONE picker per Daemon (single-flock world).
      - stop_event shuts down via `wait_for(stop_event.wait, timeout)` —
        phase-5 S-5 pattern, no poison-pill.
      - `_inflight: set[int]` tracks job_ids currently being dispatched
        so a restart mid-dispatch doesn't re-dispatch the same row
        (recover_orphans handles those).
    """

    def __init__(
        self,
        store: SubagentStore,
        bridge: ClaudeBridge,
        *,
        settings: Settings,
    ) -> None:
        self._store = store
        self._bridge = bridge
        self._settings = settings
        self._stop_event = asyncio.Event()
        self._inflight: set[int] = set()

    def request_stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        tick = self._settings.subagent.picker_tick_s
        while not self._stop_event.is_set():
            try:
                pending = await self._store.list_pending_requests(limit=1)
            except Exception:
                log.warning("picker_list_failed", exc_info=True)
                pending = []
            for job in pending:
                if job.id in self._inflight:
                    continue
                if job.cancel_requested:
                    # CLI cancel before picker claimed — mark dropped.
                    log.info("picker_dropped_cancelled", job_id=job.id)
                    # set_cancel_requested kept it at 'requested'; we
                    # need an explicit dropped transition. recover_orphans
                    # handles stale ones; here we short-circuit.
                    continue
                self._inflight.add(job.id)
                asyncio.create_task(
                    self._dispatch_one(job),
                    name=f"picker_dispatch_{job.id}",
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=tick
                )
            except asyncio.TimeoutError:
                continue

    async def _dispatch_one(self, job: SubagentJob) -> None:
        """Dispatch one pending request via the dedicated picker bridge.

        Sets CURRENT_REQUEST_ID so on_subagent_start patches the row
        from 'requested' → 'started' with the real sdk_agent_id.
        """
        token = CURRENT_REQUEST_ID.set(job.id)
        prompt = _PICKER_PROMPT_TEMPLATE.format(
            kind=job.agent_type, task_text=job.task_text or ""
        )
        try:
            async for _msg in self._bridge.ask(
                chat_id=self._settings.owner_chat_id,
                user_text=prompt,
                history=[],
            ):
                pass
        except ClaudeBridgeError:
            log.warning("picker_bridge_error", job_id=job.id, exc_info=True)
        except asyncio.CancelledError:
            log.info("picker_dispatch_cancelled", job_id=job.id)
            raise
        except Exception:
            log.warning("picker_unexpected_error", job_id=job.id, exc_info=True)
        finally:
            CURRENT_REQUEST_ID.reset(token)
            self._inflight.discard(job.id)
```

**Dispatch lifecycle:**
1. Picker reads `status='requested'` row from `subagent_jobs`.
2. Picker sets `CURRENT_REQUEST_ID = job.id` ContextVar.
3. Picker calls `picker_bridge.ask(OWNER_CHAT_ID, prompt, [])`.
4. Bridge runs the main turn; model invokes `Task` tool with the kind.
5. SDK spawns subagent → fires SubagentStart hook.
6. Hook reads `CURRENT_REQUEST_ID` → calls
   `update_sdk_agent_id_for_claimed_request` → row status flips
   `'requested'` → `'started'` with real agent_id.
7. Subagent runs → SubagentStop hook fires → `record_finished`
   updates row to `'completed'` → notify sent via Telegram.
8. Picker's main-turn ResultMessage arrives (model replied
   'dispatched'); `_dispatch_one` returns; `_inflight.discard`.

**Picker-starvation test (GAP #17):** the dedicated `picker_bridge`
has its own `Semaphore(settings.claude.max_concurrent)`, so even 10
parallel picker dispatches cannot block a concurrent user turn on the
main bridge. Integration test `test_subagent_picker_does_not_starve_user_chat`
(§8.2) asserts user-turn latency under picker flood.

### 3.10 `src/assistant/bridge/claude.py` edits (commit 6) — updated wave-2

**Wave-2 B-W2-8:** `_GLOBAL_BASELINE` gains `"Task"` conditionally —
only when `self._agents` is non-empty. Rationale: narrower-is-safer
default; skills' `allowed_tools` manifests don't need to know about
Task; the bridge owns the advertisement of Task iff agents are
registered. Unit test `test_allowed_tools_includes_task_when_agents_registered`
locks both branches.

Three changes: constructor accepts `extra_hooks` + `agents`,
`_GLOBAL_BASELINE` conditionally includes `"Task"` via
`_effective_allowed_tools` baseline_extras param, `_build_options`
merges hooks and passes `agents=`.

```python
class ClaudeBridge:
    def __init__(
        self,
        settings: Settings,
        *,
        extra_hooks: dict[str, list[HookMatcher]] | None = None,
        agents: dict[str, AgentDefinition] | None = None,
    ) -> None:
        self._settings = settings
        self._sem = asyncio.Semaphore(settings.claude.max_concurrent)
        self._extra_hooks = extra_hooks or {}
        self._agents = agents
```

Modify `_effective_allowed_tools` signature to accept optional
`baseline_extras` (frozenset) — union additions to the baseline before
intersection. Phase-6 uses this to let `"Task"` through when the bridge
has agents registered:

```python
def _effective_allowed_tools(
    manifest_entries: list[dict[str, Any]],
    *,
    baseline_extras: frozenset[str] = frozenset(),
) -> list[str]:
    """... (existing docstring) ...

    `baseline_extras`: phase-6 extension — tool names to union into the
    baseline before intersecting with skills' allowed_tools manifests.
    Passing `frozenset({"Task"})` lets the main turn delegate while
    skills that don't know about Task still get everything else.
    """
    effective_baseline = _GLOBAL_BASELINE | baseline_extras
    if not manifest_entries:
        return sorted(effective_baseline)
    # ... rest identical, but compare against effective_baseline not
    # _GLOBAL_BASELINE.
```

Inside `_build_options`, after computing `hooks` dict (line ~192):

```python
hooks: dict[Any, Any] = {
    "PreToolUse": make_pretool_hooks(pr),
    "PostToolUse": make_posttool_hooks(pr, dd),
}
# Merge extra_hooks from Daemon (phase 6: subagent hooks).
# PreToolUse MUST be unioned (cancel-flag-gate on top of phase-3 guards).
for event, matchers in self._extra_hooks.items():
    if event == "PreToolUse":
        hooks["PreToolUse"] = list(hooks["PreToolUse"]) + list(matchers)
    else:
        hooks.setdefault(event, []).extend(matchers)

# Phase 6 B-W2-8 pitfall #13: extend baseline with Task when agents
# are registered. Narrower default; tests lock both branches.
baseline_extras = frozenset({"Task"}) if self._agents else frozenset()
allowed_tools = _effective_allowed_tools(
    entries, baseline_extras=baseline_extras
)

opts_kwargs: dict[str, Any] = {
    "cwd": str(pr),
    "setting_sources": ["project"],
    "max_turns": self._settings.claude.max_turns,
    "allowed_tools": allowed_tools,
    "hooks": hooks,
    "system_prompt": system_prompt,
    **thinking_kwargs,
}
if self._agents:
    opts_kwargs["agents"] = self._agents
return ClaudeAgentOptions(**opts_kwargs)
```

### 3.11 `src/assistant/bridge/hooks.py` edits (commit 5) — updated wave-2

**GAP #14 wave-2:** `_validate_task_argv` spec inline (copying the
phase-5 `_validate_schedule_argv` skeleton). Returns `str | None` to
match existing sibling validators (deny-reason or None).

Add after `_validate_schedule_argv` (~line 355):

```python
# Phase 6 argv gate for `python tools/task/main.py <sub>`.
_TASK_SUBCMDS: frozenset[str] = frozenset(
    {"spawn", "list", "status", "cancel", "wait"}
)
_TASK_KINDS: frozenset[str] = frozenset(
    {"general", "worker", "researcher"}
)
_TASK_TASK_MAX_BYTES = 4096
_TASK_TIMEOUT_MIN_S = 1
_TASK_TIMEOUT_MAX_S = 600
_TASK_LIMIT_MAX = 100


def _validate_task_argv(args: list[str]) -> str | None:
    """Phase 6 bash hook gate for `python tools/task/main.py ...`.

    Runs on arguments AFTER the script path (argv[2:]). Mirrors
    `_validate_schedule_argv`: enum subcommands, dup-flag deny
    (phase-5 B-W2-5 lesson), per-sub flag whitelist, size/range caps.
    """
    if not args:
        return "task CLI requires a subcommand"
    sub = args[0]
    if sub not in _TASK_SUBCMDS:
        return f"task subcommand {sub!r} not allowed"
    remaining = args[1:]

    if sub == "spawn":
        allowed = {"--kind", "--task", "--callback-chat-id"}
        required = {"--kind", "--task"}
        seen: dict[str, str] = {}
        i = 0
        while i < len(remaining):
            tok = remaining[i]
            if tok not in allowed:
                return f"task spawn: flag {tok!r} not allowed"
            if tok in seen:
                return f"task spawn: duplicate flag {tok!r}"
            if i + 1 >= len(remaining):
                return f"task spawn: flag {tok} requires a value"
            val = remaining[i + 1]
            seen[tok] = val
            if tok == "--kind" and val not in _TASK_KINDS:
                return f"task spawn: --kind must be one of {sorted(_TASK_KINDS)}"
            if tok == "--task" and len(val.encode("utf-8")) > _TASK_TASK_MAX_BYTES:
                return f"task spawn: --task exceeds {_TASK_TASK_MAX_BYTES} bytes"
            if tok == "--callback-chat-id":
                try:
                    int(val)
                except ValueError:
                    return "task spawn: --callback-chat-id must be integer"
            i += 2
        missing = required - seen.keys()
        if missing:
            return f"task spawn: missing required flag(s) {sorted(missing)}"
        return None

    if sub == "list":
        allowed = {"--status", "--kind", "--limit"}
        seen_flags: set[str] = set()
        i = 0
        while i < len(remaining):
            tok = remaining[i]
            if tok not in allowed:
                return f"task list: flag {tok!r} not allowed"
            if tok in seen_flags:
                return f"task list: duplicate flag {tok!r}"
            seen_flags.add(tok)
            if i + 1 >= len(remaining):
                return f"task list: flag {tok} requires a value"
            val = remaining[i + 1]
            if tok == "--limit":
                try:
                    n = int(val)
                except ValueError:
                    return "task list: --limit must be integer"
                if n < 1 or n > _TASK_LIMIT_MAX:
                    return f"task list: --limit must be 1..{_TASK_LIMIT_MAX}"
            i += 2
        return None

    if sub in ("status", "cancel"):
        if len(remaining) != 1:
            return f"task {sub}: exactly one positional job_id required"
        try:
            int(remaining[0])
        except ValueError:
            return f"task {sub}: job_id must be integer"
        return None

    if sub == "wait":
        if not remaining:
            return "task wait: positional job_id required"
        try:
            int(remaining[0])
        except ValueError:
            return "task wait: job_id must be integer"
        rest = remaining[1:]
        seen_flags = set()
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok != "--timeout-s":
                return f"task wait: flag {tok!r} not allowed"
            if tok in seen_flags:
                return f"task wait: duplicate flag {tok!r}"
            seen_flags.add(tok)
            if i + 1 >= len(rest):
                return f"task wait: flag {tok} requires a value"
            try:
                t = int(rest[i + 1])
            except ValueError:
                return "task wait: --timeout-s must be integer"
            if t < _TASK_TIMEOUT_MIN_S or t > _TASK_TIMEOUT_MAX_S:
                return (
                    f"task wait: --timeout-s must be "
                    f"{_TASK_TIMEOUT_MIN_S}..{_TASK_TIMEOUT_MAX_S}"
                )
            i += 2
        return None

    return f"task subcommand {sub!r} missing validator"
```

Wire into `_validate_python_invocation`:

```python
# Existing block:
if script == "tools/schedule/main.py":
    return _validate_schedule_argv(argv[2:])
# Phase 6 addition:
if script == "tools/task/main.py":
    return _validate_task_argv(argv[2:])
return None
```

### 3.12 `src/assistant/main.py` edits (commit 7) — updated wave-2

**Wave-2 changes:**
- B-W2-2 SDK version pin: on startup, log-warn (not crash) if
  `claude_agent_sdk.__version__` changes from `"0.1.59"`. Falls through
  to JSONL fallback if `last_assistant_message` disappears.
- B-W2-6 dedicated `picker_bridge` — constructed from the same
  `extra_hooks`+`agents` but its own Semaphore.
- B-W2-7 recovery: branch-differentiated notify (interrupted vs
  dropped).
- GAP #13: Daemon.stop ps-sweep (stdlib subprocess, warn-only) scans
  for orphan claude CLI processes after drain.

In `Daemon.start` after `conv` creation and BEFORE bridge creation:

```python
# Phase 6 B-W2-2: loudly log SDK version drift; DO NOT crash — JSONL
# fallback inside hook handles the `last_assistant_message` field
# disappearing.
import claude_agent_sdk as _sdk
if _sdk.__version__ != "0.1.59":
    self._log.warning(
        "sdk_version_drift_phase6",
        expected="0.1.59",
        seen=_sdk.__version__,
        note=(
            "Phase-6 SubagentStop hook relies on the runtime "
            "'last_assistant_message' field which is not in the SDK "
            "TypedDict. If this version drops or changes it, the "
            "JSONL fallback in subagent/hooks.py picks up."
        ),
    )

# Phase 6: subagent ledger store (shares ConversationStore.lock)
self._sub_store = SubagentStore(self._conn, lock=conv.lock)
recovered = await self._sub_store.recover_orphans(stale_requested_after_s=3600)
# recovered dict: {"interrupted": N1, "dropped": N2}
total = recovered.get("interrupted", 0) + recovered.get("dropped", 0)
if total:
    self._log.warning("subagent_orphans_recovered", **recovered)
    msg_parts: list[str] = []
    if recovered.get("interrupted"):
        msg_parts.append(
            f"{recovered['interrupted']} subagent(s) marked interrupted "
            "(prior daemon run crashed mid-subagent)"
        )
    if recovered.get("dropped"):
        msg_parts.append(
            f"{recovered['dropped']} pending request(s) dropped "
            "(CLI insert sat >1h without pickup)"
        )
    self._spawn_bg(
        self._adapter.send_text(
            self._settings.owner_chat_id,
            "daemon restart: " + "; ".join(msg_parts),
        ),
        name="subagent_orphan_notify",
    )

# Subagent hooks — shared factory, reused by BOTH bridges (B-W2-6)
self._subagent_pending: set[asyncio.Task[Any]] = set()
sub_hooks = make_subagent_hooks(
    store=self._sub_store,
    adapter=self._adapter,
    settings=self._settings,
    pending_updates=self._subagent_pending,
)
sub_agents = build_agents(self._settings)

# B-W2-6: user-chat bridge and picker bridge are DISTINCT instances.
# Same hooks + agents; own Semaphore each. Prevents picker flood from
# starving user turns.
bridge = ClaudeBridge(
    self._settings,
    extra_hooks=sub_hooks,
    agents=sub_agents,
)
self._picker_bridge = ClaudeBridge(
    self._settings,
    extra_hooks=sub_hooks,
    agents=sub_agents,
)

# Picker for CLI-spawn pickups (§3.9) — uses dedicated bridge.
self._subagent_picker: SubagentRequestPicker | None = None
if self._settings.subagent.enabled:
    self._subagent_picker = SubagentRequestPicker(
        self._sub_store,
        self._picker_bridge,
        settings=self._settings,
    )
    self._spawn_bg(self._subagent_picker.run(), name="subagent_picker")
```

In `Daemon.stop`, between existing step 2.5 (scheduler drain) and step 3
(adapter stop), add:

```python
# Step 2.55 — phase-6: signal picker to stop (before we await its
# dispatches via _bg_tasks drain).
if self._subagent_picker is not None:
    self._subagent_picker.request_stop()

# Step 2.6 — phase-6: drain subagent notify tasks (GAP #12).
if self._subagent_pending:
    updates = list(self._subagent_pending)
    self._log.info("daemon_draining_subagent_notifies", count=len(updates))
    try:
        await asyncio.wait_for(
            asyncio.gather(*updates, return_exceptions=True),
            timeout=self._settings.subagent.drain_timeout_s,
        )
    except TimeoutError:
        self._log.warning(
            "daemon_subagent_drain_timeout",
            count=len([t for t in updates if not t.done()]),
        )

# Step 2.7 — GAP #13: warn-only ps sweep for orphan `claude` CLI
# subprocesses. Detection only; we do NOT kill — operator reads the log.
try:
    import subprocess
    proc = subprocess.run(
        ["ps", "-Ao", "pid,command"],
        capture_output=True,
        text=True,
        timeout=2.0,
        check=False,
    )
    claude_lines = [
        line for line in proc.stdout.splitlines()
        if "claude" in line and "grep" not in line
    ]
    if claude_lines:
        self._log.warning(
            "phase6_possible_orphan_claude_processes",
            count=len(claude_lines),
            sample=claude_lines[:3],
        )
except (OSError, subprocess.SubprocessError):
    pass
```

### 3.13 `tools/task/main.py` (commit 5) — updated wave-2

stdlib-only. `sys.path.append(<root>/src)` shim (pitfall phase-5 #8
— `append`, not `insert(0)`, inherited from phase-4 `_memlib` lesson).

**Wave-2:** spawn writes a row with `status='requested'` and
`sdk_agent_id IS NULL` (partial UNIQUE tolerates). Picker claims it
later; Start hook patches `sdk_agent_id` via ContextVar.

Subcommands:
- `spawn --kind general --task TEXT [--callback-chat-id N]` →
  `record_pending_request` → INSERT row with `status='requested'`,
  `sdk_agent_id=NULL`, `spawned_by_kind='cli'`; print
  `{"job_id": N, "status": "requested"}`.
- `list [--status S] [--kind K] [--limit 20]` → print JSON array.
  `--status` accepts any of the 8 status enum values.
- `status <job_id>` → full row or exit 7 if missing.
- `cancel <job_id>` → `set_cancel_requested(id)` through store; print
  `{"cancel_requested": true}` or `{"already_terminal": "<status>"}`.
- `wait <job_id> [--timeout-s 60]` → poll DB until status IN
  `('completed','failed','stopped','interrupted','error','dropped')`;
  exit 0/5 (0 on 'completed', 5 otherwise).

`--callback-chat-id` in phase 6 defaults to `OWNER_CHAT_ID` from env.
Explicit param reserved for phase-8 multi-chat.

### 3.14 `skills/task/SKILL.md` (commit 5)

```yaml
---
name: task
description: "Delegation of long-running tasks (>10s) to a background subagent via the native Task tool. Use when the user asks for a long post, deep research, or a bulk operation that would stall the main turn. The main turn stays open until the subagent finishes; the final result is also pushed to the owner via Telegram automatically. Three kinds: general (default, full tool access), worker (CLI-only), researcher (read-only). CLI `python tools/task/main.py` manages shell-init spawn, list, status, cancel."
---
```

Body sections:

1. When to use: user asks for >10s work (long write-up, research,
   bulk tool ops, generated artifacts).
2. When NOT: quick factual questions, ambiguous asks (clarify first).
3. How: in main turn, invoke Task tool with `subagent_type` matching a
   registered kind. SDK spawns; hook delivers result to owner.
4. Kinds:
   - `general` — default. Full tool access (Bash, Read, Write, Edit,
     Grep, Glob, WebFetch).
   - `worker` — single CLI invocation flavour. `Bash, Read`.
   - `researcher` — read-only research. `Read, Grep, Glob, WebFetch`.
5. CLI shell-init:
   ```
   python tools/task/main.py spawn --kind researcher \
     --task "find recent OAuth 2.0 security CVEs and summarise"
   ```
6. CLI cancel: `python tools/task/main.py cancel 42`.
7. Limitations (from S-6-0):
   - Subagents CANNOT spawn sub-subagents (no "Task" tool in their
     allowed list — deliberate cap).
   - Cancel works ONLY if subagent makes tool calls (flag is polled on
     PreToolUse). Tool-free subagents run to completion.
   - Main turn wall-clock approximates subagent wall-clock — use CLI
     `tools/task/main.py` instead of inline delegation if you want the
     main turn to return immediately.

### 3.15 `src/assistant/bridge/system_prompt.md` edits (commit 7)

Add one paragraph under existing tool-availability section:

```
You have access to a `Task` tool that delegates a self-contained task to
a background subagent (one of: `general`, `worker`, `researcher`). Use
it when the user asks for work that will take longer than ~10 seconds to
complete, or when the task is read-only research you want isolated from
the main conversation. The subagent's final reply is also delivered to
the user automatically, so do NOT re-paste long results back after the
Task tool returns — a short confirmation is enough. See skill `task` for
when to delegate vs. answer inline.
```

---

## 4. Per-file edit specs for existing files

### 4.1 `src/assistant/state/db.py`

- After `SCHEMA_VERSION = 3`, bump to 4.
- After `_apply_v3` def, insert `_apply_v4` (§3.2).
- After `if current < 3: await _apply_v3(...)` block, insert `if current < 4: await _apply_v4(...)`.

### 4.2 `src/assistant/config.py`

- Import `Field` if not already.
- Insert `SubagentSettings` class (§3.3) right after `SchedulerSettings`.
- In `Settings` class, add `subagent: SubagentSettings = Field(default_factory=SubagentSettings)`.

### 4.3 `src/assistant/bridge/hooks.py`

- After `_SCHEDULE_*` constants: add `_TASK_*` constants.
- After `_validate_schedule_argv` def: add `_validate_task_argv` def.
- In `_validate_python_invocation`, add `elif script == "tools/task/main.py":` dispatch.
- Ensure `_BASH_PROGRAMS["python"]` routing matches new path.

### 4.4 `src/assistant/bridge/claude.py`

- Constructor gains `extra_hooks`, `agents` kwargs (§3.10).
- `_build_options` merges `extra_hooks` under `"PreToolUse"`, others by
  `setdefault-extend`.
- When `self._agents` present, add `"Task"` to `allowed_tools` and pass
  `agents=` to `ClaudeAgentOptions`.

### 4.5 `src/assistant/main.py` `Daemon.start` / `Daemon.stop`

See §3.12. Insert points:
- `start`: after ConversationStore build, before `ClaudeBridge` construction.
- `stop`: between scheduler-drain (2.5) and adapter.stop (step 3).

### 4.6 `src/assistant/bridge/system_prompt.md`

See §3.15.

---

## 5. Spike citations — updated wave-2

Every non-trivial decision has a spike anchor:

- **§3.1** schema `sdk_agent_id` nullable + partial UNIQUE: **S-6-0
  Q12** (`agent_id` stable across Start/Stop) + **wave-2 B-W2-3**
  (option A).
- **§3.1** `'requested'` pre-picker status + `'dropped'` terminal:
  **wave-2 B-W2-7**.
- **§3.5** `build_agents` omits `"Task"` from child `tools`: **S-6-0
  Q4** (empirically not observed; wave-2 wording fix).
- **§3.5** `model="inherit"` in all three: **S-6-0 Q10** — runtime-valid.
- **§3.5** per-kind `prompt` is full system prompt: **S-6-0 Q9** — FULL,
  not appended; haiku test confirmed.
- **§3.5** `background=True` flag — **kept for forward-compat, no
  runtime effect on 0.1.59**: **wave-2 Q1-BG re-run** (FAIL_BG_BLOCKS_MAIN_TURN).
- **§3.8** on_subagent_stop reads `raw.get("last_assistant_message")`
  with 250 ms JSONL fallback: **S-6-0 Q5** raw verdict PARTIAL +
  **wave-2 B-W2-2** static types.py cross-check (not in TypedDict).
- **§3.8** on_pretool_cancel_gate flag-poll reads `raw.get("agent_id")`:
  **S-6-0 Q7** FAIL (cancel-via-SDK doesn't work) + **wave-2 S-2**
  PASS (5/5 subagent PreToolUse fires had `agent_id` on input_data).
- **§3.8** ContextVar `CURRENT_REQUEST_ID`: **wave-2 S-1** PASS
  (`spikes/phase6_s1_contextvar_report.json`).
- **§3.8** hook returns `{}` without awaiting delivery: **wave-2 GAP #12**.
- **§3.8** LRU-bounded throttle dict: **wave-2 GAP #15**.
- **§3.10** shared-factory hooks work across bridges: **S-6-0 Q6** —
  PASS, distinct agent_ids seen on shared callback.
- **§3.10** `"Task"` in `_GLOBAL_BASELINE` via `baseline_extras`:
  **wave-2 B-W2-8** option A.
- **§3.11** `_validate_task_argv` spec: **wave-2 GAP #14** mirrors
  `_validate_schedule_argv` from phase-5.
- **§3.12** dedicated `picker_bridge`: **wave-2 B-W2-6**.
- **§3.12** recover_orphans differentiated notify: **wave-2 B-W2-7**.
- **§3.12** SDK version pin log-warn: **wave-2 B-W2-2**.
- **§3.12** recover_orphans before bridge accepts turns:
  **phase-5** invariant.
- **§3.12** `drain subagent_pending` between scheduler drain and
  adapter.stop: **phase-5** HIGH #5.
- **§3.12** ps-sweep on Daemon.stop: **wave-2 GAP #13** (warn-only).
- **§0 pitfall 5**: **S-6-0 Q1** + **wave-2 Q1-BG re-run** — main
  turn wall ≈ subagent wall regardless of `background=`.
- **§0 pitfall 17 (subagent sandbox traversal)**: **wave-2 S-2** PASS
  (5/5 subagent Bash calls denied by parent's phase-3 hooks).

---

## 6. Open questions — wave-2 closures

**All 5 v1 open questions are now RESOLVED.**

1. ~~Cancel flag-poll context~~ — **CLOSED by B-W2-5 / S-2.** Static
   evidence: SDK types.py:246-262 documents `_SubagentContextMixin`
   with `agent_id: str` "Present only when the hook fires from inside
   a Task-spawned sub-agent". Empirical evidence: S-2 observed 5/5
   subagent PreToolUse fires had `agent_id` populated. Hook reads
   `raw.get("agent_id")` directly.
2. ~~Main-turn "reply 'launched'" realism~~ — **CLOSED by B-W2-1
   re-run.** `background=True` has no effect on 0.1.59. Accepted: main
   turn wall ≈ subagent wall in all scenarios. Description.md
   scenarios 1+2 must be read "notify arrives at child completion via
   Telegram, main turn closes shortly after". No bridge workaround;
   consider `ClaudeSDKClient` escape hatch as phase-7 topic.
3. ~~Picker → agent_id matching~~ — **CLOSED by B-W2-4 / S-1.**
   ContextVar propagation empirically PASS (1001/1002 match in two
   back-to-back runs). Implementation: picker sets
   `CURRENT_REQUEST_ID` ContextVar; `on_subagent_start` reads it; calls
   `update_sdk_agent_id_for_claimed_request(job_id, agent_id, ...)`.
4. ~~Cost accounting~~ — **DEFERRED TO PHASE 9.** GAP #11: raw Q1 main
   run shows `cost_usd=0.1244035` in the main ResultMessage, but this
   is aggregate (parent + child). Per-subagent attribution requires
   either per-child `TaskNotificationMessage.usage` read
   (`TaskUsage.total_tokens`, not cost) OR a bridge-level counter that
   diff's main-turn cost before/after each child. Out of scope phase-6.
   Schema keeps `cost_usd REAL` column nullable for phase-9 fill.
5. ~~Subagent PreToolUse traversal~~ — **CLOSED by B-W2-5 / S-2.**
   Confirmed 5/5 denies at parent's phase-3 sandbox (bash metachar +
   allowlist); `agent_id` on every fire. No phase-6 sandbox
   regression.

---

## 7. Invariants (phase 6 canonical) — updated wave-2

1. **`subagent_jobs.sdk_agent_id` is the single identity key (when
   present).** Every ledger operation keys on it. Never key on
   `session_id`. Pending CLI rows carry NULL; partial UNIQUE index
   handles it.
2. **Subagent `tools` list NEVER includes `"Task"` in phase 6.**
   Recursion cap is empirical (S-6-0 Q4). Regression test
   `test_subagent_no_recursion_lock` asserts.
3. **`record_finished` is status-preconditioned.** `WHERE
   sdk_agent_id=? AND status='started'`. Duplicate or out-of-order
   Stop hooks are no-ops with a skew log.
4. **`recover_orphans` runs exactly once at `Daemon.start` BEFORE
   picker or bridge accepts new turns.** Two branches: `interrupted`
   (started + finished_at IS NULL) and `dropped` (requested + >1h
   stale). Notify splits the two in the owner-facing message.
5. **`Daemon.stop` drains `_subagent_pending` before `adapter.stop()`
   and `conn.close()`.** Picker stop signalled BEFORE drain so no new
   dispatches land mid-drain.
6. **Subagent notify body uses `last_assistant_message` first,
   JSONL transcript parse with 250 ms retry as fallback.** Runtime-only
   field; SDK version pin logs a warning on drift but doesn't crash.
7. **Cancel works via PreToolUse flag-poll only.** No SDK cancel API.
   Tool-free subagents are uncancellable (documented in skill).
   PreToolUse fires with `agent_id` populated for subagent-origin
   calls (S-2 verified).
8. **One hook factory per Daemon; passed to BOTH ClaudeBridge instances
   (user + picker).** Q6 PASS guarantees cross-bridge SubagentStop
   still fires through the same ledger.
9. **Picker bridge and user-chat bridge are DISTINCT instances** with
   independent `asyncio.Semaphore`s. Prevents picker flood from
   starving user turns (B-W2-6 + GAP #17).
10. **Hook body is non-blocking.** `on_subagent_stop` creates the
    shielded delivery task and returns `{}` — never awaits delivery
    (GAP #12).

---

## 8. Testing plan (details) — updated wave-2

### 8.1 Unit tests

- `test_db_migrations_v4`: v3→v4 applies; subagent_jobs columns
  present; partial UNIQUE index on sdk_agent_id allows multiple NULL
  rows; `PRAGMA user_version = 4`.
- `test_subagent_definitions`: 3 kinds, tools lists correct,
  **`"Task"` NOT in any `tools` list** (pitfall #2 lock),
  model="inherit", background=True.
- `test_subagent_store`: full CRUD + state machine (requested →
  started → completed/stopped/interrupted/dropped) + recover_orphans
  branch matrix (interrupted vs dropped by age bucket) + cancel flag +
  status-precondition skew + partial UNIQUE tolerates NULL.
- `test_subagent_format`: notify footer exactly matches locked format;
  truncation at `max_body_bytes` respects UTF-8 char boundaries.
- `test_subagent_hooks`: mocked `input_data` dicts for
  Start/Stop/PreToolUse-cancel-gate:
  - Stop hook returns `{}` IMMEDIATELY; delivery task is in
    `pending_updates` set (GAP #12).
  - Stop hook primary path reads `last_assistant_message`; empty →
    JSONL fallback with 250 ms sleep retry.
  - Start hook with `CURRENT_REQUEST_ID` set calls
    `update_sdk_agent_id_for_claimed_request` (B-W2-4).
  - Start hook without ContextVar calls `record_started`.
  - PreToolUse cancel-gate: returns deny iff `cancel_requested=1`
    AND `raw["agent_id"]` present.
- `test_task_cli`: spawn/list/status/cancel/wait all branches + JSON
  shape.
- `test_task_bash_hook`: `_validate_task_argv` accepts whitelist,
  rejects dup flags, rejects oversize --task, rejects OOB --timeout-s,
  rejects non-enum --kind.
- `test_allowed_tools_includes_task_when_agents_registered` (B-W2-8):
  `_effective_allowed_tools([...], baseline_extras=frozenset({"Task"}))`
  includes `"Task"`; without extras, it does not.
- `test_subagent_no_recursion_lock` (pitfall #2 / Q4 regression):
  assert `build_agents(settings)["general"].tools` omits `"Task"`;
  same for worker/researcher.

### 8.2 Integration

- `test_subagent_recovery.py`: seed `started` row + `requested` row
  >1h old + `requested` row <1h → boot `Daemon` → observe
  interrupted / dropped / untouched respectively + owner notify text
  splits the two categories.
- `test_subagent_picker_does_not_starve_user_chat` (GAP #17): spawn
  10 pending requests; measure picker drain time; concurrently issue
  a "user turn" (via fake ClaudeHandler path) through the main
  bridge; assert user-turn latency < picker drain time / 2 (soft
  threshold; the real guarantee is separate Semaphores, this is a
  sanity check).
- `test_subagent_contextvar_propagation`: unit-level; spawn a fake
  SDK iter, set `CURRENT_REQUEST_ID`, verify hook sees it.
- `test_subagent_e2e.py`: gated by `RUN_SDK_INT=1` env var — full SDK
  spawn via main turn with `general` agent; assert Start+Stop hooks
  fire, ledger row complete, adapter.send_text called.
- **GAP #18:** add `RUN_SDK_INT` handling:

  ```python
  import os
  import pytest
  if os.environ.get("RUN_SDK_INT") != "1":
      pytest.skip("SDK integration gated by RUN_SDK_INT=1", allow_module_level=True)
  ```

  Match phase-5 `tests/test_scheduler_e2e_sdk.py` skip pattern. The
  test is runnable locally (`RUN_SDK_INT=1 uv run pytest
  tests/test_subagent_e2e.py`); CI default skips.

### 8.3 Mock vs real-SDK

Unit tests inject mock dicts at hook input shape. Integration tests
that talk to SDK are gated by `RUN_SDK_INT` env var; CI default
skips. Operator runs locally before merge.

---

## 9. Telemetry (phase-9 deferral note)

Fields left NULL / reserved now, filled phase 9:
- `cost_usd`
- per-kind concurrency counters
- per-turn subagent spawn count metric

---

## 10. Summary & exit checklist

Phase 6 = SDK-native subagent integration. Ledger-only DB, shared
bridge, hook-based notify, flag-poll cancel.

**Exit (updated wave-2):**
- [ ] S-6-0 + wave-2 spikes committed
      (`spikes/phase6_s0_native_subagent.py` + Q1-BG re-run,
      `spikes/phase6_s1_contextvar_hook.py`,
      `spikes/phase6_s2_subagent_sandbox.py`, raw reports,
      `plan/phase6/spike-findings.md`)
- [ ] Migration v4 applied with partial UNIQUE on sdk_agent_id,
      `PRAGMA user_version=4`
- [ ] `AgentDefinition` registry covers general/worker/researcher with
      `background=True` + no "Task" in their tools
- [ ] SubagentStart + SubagentStop + cancel-flag PreToolUse hooks wired
      through shared factory; ContextVar correlation working
- [ ] Telegram notify uses `last_assistant_message` with JSONL fallback
- [ ] CLI `tools/task/main.py` covers spawn/list/status/cancel/wait
- [ ] Bash hook gate validates task argv (dup-flag deny,
      subcmd whitelist, size caps); test covers all branches
- [ ] SubagentRequestPicker running on DEDICATED bridge; correlation to
      Start hook via CURRENT_REQUEST_ID ContextVar verified
- [ ] Daemon orphan recovery (interrupted + dropped branches) +
      stop-drain + ps-sweep integrated
- [ ] SDK version pin log-warn on startup
- [ ] ~35 new tests passing (~910 total); `RUN_SDK_INT` gates E2E
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
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/system_prompt.md
- /Users/agent2/Documents/0xone-assistant/spikes/phase6_s0_native_subagent.py
- /Users/agent2/Documents/0xone-assistant/spikes/phase6_s0_findings.md
- /Users/agent2/Documents/0xone-assistant/plan/phase6/spike-findings.md
