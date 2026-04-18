# Phase 6 — Implementation (spike-verified, v1, 2026-04-18)

Thin layer над SDK-native primitives `AgentDefinition` +
`SubagentStart/SubagentStop` hooks. Все ключевые допущения плана
прошли через S-6-0 (см. `plan/phase6/spike-findings.md` и
`spikes/phase6_s0_report.json`). Никаких blocker'ов — есть 3
redefined допущения и 1 expected fallback.

## Revision history

- **v1** (2026-04-18, after S-6-0): initial coder-ready spec.

Empirical backing:
- `spikes/phase6_s0_native_subagent.py` — одна orchestrator-script, 8
  subtests + 5 cheap probes.
- `spikes/phase6_s0_report.json` — raw observations.
- `spikes/phase6_s0_findings.md` / `plan/phase6/spike-findings.md` —
  verdicts and per-Q analysis.

Companion docs (coder **must** read before starting):
- `plan/phase6/description.md` — E2E scenarios.
- `plan/phase6/detailed-plan.md` — canonical spec §1–§20.
- `plan/phase5/implementation.md` — style precedent.
- `plan/phase5/summary.md` — phase-5 invariants (shared lock, shield,
  drain order, flock, LRU, status-precondition SQL).

**Auth:** OAuth via `claude` CLI (`~/.claude/`). No `ANTHROPIC_API_KEY`.
Subagents inherit the parent's auth via SDK.

---

## 0. Pitfall box (MUST READ)

Things the coder absolutely MUST NOT do. Each item either comes from
S-6-0 (spike evidence) or phase-5 hard-won lessons (see
`plan/phase5/summary.md` §7).

1. **DO NOT** parse `agent_transcript_path` to extract subagent's reply
   text. S-6-0 Q5 confirmed `SubagentStopHookInput["last_assistant_message"]`
   carries the final assistant text as a plain string. Use the hook field
   directly; transcript-read is FALLBACK ONLY (when the string is empty
   or missing).
2. **DO NOT** add a "depth cap deny" handler in `SubagentStart` that
   returns `additionalContext`. S-6-0 Q4: depth is gated by the child's
   own `tools` list — if `AgentDefinition(tools=[...])` omits `"Task"`,
   recursion is structurally impossible. Just don't include `"Task"` in
   `general`/`worker`/`researcher` definitions for phase 6.
3. **DO NOT** rely on `main_task.cancel()` to kill a subagent. S-6-0 Q7:
   orphan. Use PreToolUse flag-poll: hook reads `cancel_requested=1` from
   `subagent_jobs` keyed on `agent_id` → returns deny → subagent
   stack unwinds on its next tool call. If subagent uses NO tools, flag
   has no effect. Accept and document.
4. **DO NOT** key the ledger on `session_id` from SubagentStart hook.
   S-6-0 Q12: on Start, `session_id` is the parent's session; on Stop,
   `session_id` is the subagent's own session. Asymmetric. Key
   `subagent_jobs.sdk_agent_id` (UNIQUE) — stable across both hooks.
5. **DO NOT** expect main `query()` iterator to finish "in ~3 sec" for
   E2E scenario 1. S-6-0 Q1: main ResultMessage arrived AFTER subagent
   TaskNotification. The Task tool is a synchronous RPC within the
   parent's turn — main `query()` stays open until the child ends. Plan
   description scenarios 1+2 are semantically REAL (child work happens
   in a separate session), but main turn wall ≈ subagent wall. Notify
   pushes through the hook during child's stop, so the OWNER sees result
   "later" regardless.
6. **DO NOT** attach `"Task"` tool to the subagent's own `tools` list
   (= prevents recursion). Keep `"Task"` only in MAIN turn's
   `allowed_tools`.
7. **DO NOT** forget `asyncio.shield` on `adapter.send_text` inside
   `on_subagent_stop` hook — phase-5 lesson (B-W2-3, HIGH #5): if
   `Daemon.stop()` cancels the dispatching task mid-send, the DB UPDATE
   / adapter call get cancelled and the ledger desyncs. Use the same
   `pending_updates: set[asyncio.Task]` + `shield` pattern as
   `SchedulerDispatcher`.
8. **DO NOT** open a second aiosqlite connection for `SubagentStore`.
   Reuse `ConversationStore.conn` + `ConversationStore.lock` — phase-5
   pattern (S-1 verified contention).
9. **DO NOT** UPDATE `subagent_jobs.status` without a `WHERE status=...`
   precondition. Status-machine invariants enforced by SQL, not by
   Python (phase-5 G-W2-6). `rowcount=0` → log skew, don't raise.
10. **DO NOT** treat `SubagentStop` as at-least-once like phase-5
    triggers. Each subagent fires start + stop exactly once per SDK
    contract (S-6-0 confirms 1:1 in all observed runs). UNIQUE
    constraint on `sdk_agent_id` handles the edge case where a hook
    somehow fires twice for the same agent_id.
11. **DO NOT** import `claude_agent_sdk.HookMatcher` at module top of
    `subagent/hooks.py` — follow `bridge/hooks.py::make_pretool_hooks`
    pattern (lazy import inside factory). Keeps validator modules pure.
12. **DO NOT** block inside hook callbacks for more than ~500 ms. Hooks
    run inside the SDK's iterator loop; stalling them blocks the main
    `query()` iterator. Use `_spawn_bg(adapter.send_text(...), name=...)`
    from a reference held at hook factory construction; the hook itself
    only INSERTs the ledger row + creates the shielded delivery task.
13. **DO NOT** pass `allowed_tools=[...]` without `"Task"` to the main
    turn's `ClaudeAgentOptions`. S-6-0 Q2: we included it explicitly and
    observed usage; omitting it is untested (plan detailed §2.1 Q2
    fallback remains documented).
14. **DO NOT** use `setting_sources=["project"]` and expect subagents to
    inherit only a subset of project settings. S-6-0 did not verify
    which `settings.json` policies flow to subagents. Document: subagent
    runs under same settings as parent. Narrow via `AgentDefinition.tools` /
    `disallowedTools` if needed.
15. **DO NOT** emit the notify footer with `kind=<raw agent_type>` if
    `agent_type` contains non-Telegram-safe chars. `_format_notification`
    must Markdown-escape kind/status inline.

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

### 3.1 `src/assistant/state/migrations/0004_subagent.sql` (commit 1)

Verbatim SQL. S-6-0 confirms `sdk_agent_id` as stable primary key across
Start+Stop (Q12). `depth INTEGER DEFAULT 0` is documentation only — we
don't enforce depth in hook (see pitfall #2); kept for audit.

```sql
-- 0004_subagent.sql — phase 6 (SDK-native subagent ledger)

CREATE TABLE IF NOT EXISTS subagent_jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sdk_agent_id      TEXT    NOT NULL UNIQUE,
    sdk_session_id    TEXT,                             -- subagent's own session_id (from Stop hook)
    parent_session_id TEXT,                             -- parent session_id (from Start hook)
    agent_type        TEXT    NOT NULL,                 -- 'general' | 'worker' | 'researcher'
    task_text         TEXT,                             -- nullable for native-Task spawn (we don't see user prompt)
    transcript_path   TEXT,                             -- agent_transcript_path from Stop hook
    status            TEXT    NOT NULL DEFAULT 'started',
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    result_summary    TEXT,                             -- first 500 chars of last_assistant_message
    cost_usd          REAL,                             -- nullable; reserved for phase-9 accounting
    callback_chat_id  INTEGER NOT NULL,                 -- always OWNER_CHAT_ID for phase 6
    spawned_by_kind   TEXT    NOT NULL,                 -- 'user' | 'scheduler' | 'cli'
    spawned_by_ref    TEXT,                             -- schedule_id on scheduler spawns; null otherwise
    depth             INTEGER NOT NULL DEFAULT 0,       -- always 0 in phase 6 (structural cap)
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    started_at        TEXT,                             -- set on SubagentStart hook fire
    finished_at       TEXT                              -- set on SubagentStop hook fire
);
CREATE INDEX IF NOT EXISTS idx_subagent_jobs_status_started ON subagent_jobs(status, started_at);
CREATE INDEX IF NOT EXISTS idx_subagent_jobs_agent_id      ON subagent_jobs(sdk_agent_id);

PRAGMA user_version = 4;
```

Allowed `status` values: `'started'`, `'completed'`, `'failed'`,
`'stopped'`, `'interrupted'`, `'error'`.

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

### 3.6 `src/assistant/subagent/store.py` (commit 3)

Signatures (full implementation follows phase-5 `SchedulerStore` style,
all mutations under `async with self._lock`):

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
        started → (completed | failed | stopped | interrupted | error)
    `stopped` only if cancel_requested=1 at terminal time.
    """

    def __init__(self, conn: aiosqlite.Connection, *, lock: asyncio.Lock) -> None: ...

    # ---------- INSERT ----------

    async def record_pending_request(
        self,
        *,
        agent_type: str,
        task_text: str,
        callback_chat_id: int,
        spawned_by_kind: str,
        spawned_by_ref: str | None = None,
    ) -> int:
        """INSERT a pending request for SubagentRequestPicker pickup.

        Used by `tools/task/main.py spawn` — row has status='started' but
        sdk_agent_id is NULL (filled in by on_subagent_start hook when
        picker dispatches it through ClaudeBridge).

        **Phase-6 simplification:** CLI spawn pre-creates a row with
        `sdk_agent_id=NULL`; picker dispatches; Start hook matches by
        spawned_by_ref=request_id and patches the agent_id. See §4.3.
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
        """INSERT (or patch pending) — idempotent on `sdk_agent_id`.

        SQL: `INSERT ... ON CONFLICT(sdk_agent_id) DO UPDATE SET
        started_at=excluded.started_at, status='started'`. Returns
        auto-increment id. Status-precondition N/A on insert.
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
        """UPDATE cancel_requested=1 WHERE id=? AND status='started'.

        Returns `{"cancel_requested": True, "previous_status": <str>}`
        or `{"already_terminal": <str>}`.
        """

    async def is_cancel_requested(self, sdk_agent_id: str) -> bool:
        """SELECT cancel_requested FROM subagent_jobs WHERE sdk_agent_id=?.

        Used by PreToolUse flag-poll hook (S-6-0 Q7 fallback). Returns
        False if row missing (race: hook fires before ledger writes).
        """

    # ---------- recovery ----------

    async def recover_orphans(self) -> int:
        """UPDATE status='interrupted', finished_at=NOW
        WHERE status='started' AND finished_at IS NULL.

        Run ONCE at Daemon.start() BEFORE bridge accepts new turns
        (invariant §17 detailed-plan). Returns rowcount.
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
        """SubagentRequestPicker source: rows with `sdk_agent_id IS NULL
        AND status='started'`. Oldest first."""

    async def claim_pending_request(self, job_id: int) -> bool:
        """Status-precondition UPDATE: set status='started' (no-op),
        sdk_agent_id='__claimed__<job_id>__' as a claim sentinel. Returns
        True if claimed, False if already claimed by another picker
        (which should be impossible in single-daemon flock world, but
        defensive).
        """

    # ---------- misc ----------

    async def update_sdk_agent_id(self, job_id: int, sdk_agent_id: str) -> None:
        """Patch the claim-sentinel with the real agent_id once Start
        hook fires. Matches by (job_id, status='started',
        sdk_agent_id LIKE '__claimed__%')."""
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

### 3.8 `src/assistant/subagent/hooks.py` (commit 4)

Factory shape mirrors `bridge/hooks.py::make_pretool_hooks`. Returns a
dict keyed by hook event name, each value a list of `HookMatcher`.

```python
"""SubagentStart + SubagentStop + cancel-flag PreToolUse hooks.

S-6-0 verifications:
  * Q5: `SubagentStopHookInput["last_assistant_message"]` — primary
    result carrier. JSONL transcript is fallback.
  * Q6: hook factory shared across multiple ClaudeAgentOptions instances
    works — both see their subagents' events on the SAME callback.
  * Q7: cancel propagates only via this PreToolUse flag-poll.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from assistant.adapters.base import TelegramAdapterProtocol
from assistant.config import Settings
from assistant.logger import get_logger
from assistant.subagent.format import format_notification
from assistant.subagent.store import SubagentStore

log = get_logger("subagent.hooks")


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

    # Per-chat throttle.
    _last_notify_at: dict[int, float] = {}

    async def on_subagent_start(
        input_data: Any,
        tool_use_id: str | None,
        ctx: Any,
    ) -> dict[str, Any]:
        raw = cast(dict[str, Any], input_data)
        agent_id = raw["agent_id"]
        agent_type = raw["agent_type"]
        parent_session = raw.get("session_id")

        # spawn_attribution heuristic — if parent_session matches a
        # pending-request's picker session we know it's CLI/scheduler.
        # For phase 6 we default to 'user' and let the picker overwrite
        # on claim. See §4.3.
        await store.record_started(
            sdk_agent_id=agent_id,
            agent_type=agent_type,
            parent_session_id=parent_session,
            callback_chat_id=settings.owner_chat_id,
            spawned_by_kind="user",       # picker-claimed rows are patched earlier
            spawned_by_ref=None,
        )
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
        raw = cast(dict[str, Any], input_data)
        agent_id = raw["agent_id"]
        transcript_path = raw.get("agent_transcript_path")
        session_id = raw.get("session_id")

        # S-6-0 Q5 primary path:
        last_msg = raw.get("last_assistant_message") or ""
        if not last_msg and transcript_path:
            last_msg = _read_last_assistant_from_transcript(Path(transcript_path))

        was_cancelled = await store.is_cancel_requested(agent_id)
        status = "stopped" if was_cancelled else "completed"
        # (If we ever detect failure upstream, pass via additionalContext;
        # phase-6 treats non-cancel terminal as 'completed'.)

        try:
            await store.record_finished(
                sdk_agent_id=agent_id,
                status=status,
                result_summary=last_msg[:500] if last_msg else None,
                transcript_path=transcript_path,
                sdk_session_id=session_id,
                cost_usd=None,  # phase-9 accounting
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

        await _throttle(_last_notify_at, job.callback_chat_id,
                        settings.subagent.notify_throttle_ms)

        task = asyncio.create_task(
            adapter.send_text(job.callback_chat_id, body),
            name=f"subagent_notify_{job.id}",
        )
        pending_updates.add(task)
        task.add_done_callback(pending_updates.discard)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            log.info("subagent_notify_shielded_cancel", job_id=job.id)
        return {}

    async def on_pretool_cancel_gate(
        input_data: Any,
        tool_use_id: str | None,
        ctx: Any,
    ) -> dict[str, Any]:
        """Cancel-flag poll for subagent-emitted tool calls (S-6-0 Q7).

        Matcher: "*" (all tools) — runs on every tool call. For
        main-turn calls there is no `agent_id` in context so we no-op.
        For subagent calls, `ctx` carries the agent_id (form TBD — SDK
        doesn't document; fallback: read `raw["agent_id"]` if present,
        else `raw.get("session_id")` + `SELECT FROM subagent_jobs WHERE
        sdk_session_id=?`).

        If cancelled → return `{"hookSpecificOutput":
        {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "subagent cancelled by owner"}}`.
        """
        raw = cast(dict[str, Any], input_data)
        maybe_agent_id = raw.get("agent_id")
        if not maybe_agent_id:
            # Main-turn call; no subagent to cancel.
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
    `message.role == 'assistant'`. S-6-0 Q5 showed subagent transcripts
    at `.../subagents/agent-<id>.jsonl` — newline-delimited JSON, one
    entry per event.
    """
    ...


async def _throttle(
    last_notify_at: dict[int, float], chat_id: int, interval_ms: int
) -> None:
    """Module-level per-chat min-interval throttle. Non-reentrant per
    chat (single-user bot — ok)."""
    import time
    now = time.monotonic()
    last = last_notify_at.get(chat_id, 0.0)
    delta_ms = (now - last) * 1000.0
    if delta_ms < interval_ms:
        await asyncio.sleep((interval_ms - delta_ms) / 1000.0)
    last_notify_at[chat_id] = time.monotonic()
```

### 3.9 `src/assistant/subagent/picker.py` (commit 6)

Mirror of `SchedulerDispatcher` — consumer loop for CLI-spawned requests.

```python
class SubagentRequestPicker:
    """Poll `subagent_jobs` for rows with sdk_agent_id IS NULL, dispatch
    through ClaudeBridge as a single-prompt turn.

    Use-case: `tools/task/main.py spawn --kind researcher --task TEXT`
    inserts a pending row; picker ticks every `picker_tick_s` seconds,
    claims the row via `claim_pending_request`, invokes a fresh
    ClaudeBridge turn with a crafted prompt that uses native Task tool
    to delegate to the kind.

    Invariants:
    - ONE picker per Daemon (single-flock world).
    - stop_event shuts down via `wait_for(sleep, timeout)` — phase-5 S-5
      pattern, no poison-pill.
    - Shielded dispatch: CancelledError during bridge.ask still lets us
      UPDATE ledger so row doesn't stay 'started' forever.
    """
    def __init__(self, store: SubagentStore, bridge: ClaudeBridge, *,
                 settings: Settings) -> None: ...
    async def run(self) -> None: ...
    def request_stop(self) -> None: ...
```

Dispatching a request means the picker sends a prompt to the MAIN turn
asking the model to "use the `<kind>` agent with the following task".
That triggers Task tool → SDK spawns subagent → SubagentStart hook
captures real agent_id → picker patches the pending row.

### 3.10 `src/assistant/bridge/claude.py` edits (commit 6)

Two changes: constructor accepts `extra_hooks`, `_build_options`
registers `agents` and merges hooks.

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

# Phase 6 Q2 pitfall #13: include "Task" in allowed_tools only when
# agents are registered (we won't advertise Task otherwise).
if self._agents:
    if "Task" not in allowed_tools:
        allowed_tools = list(allowed_tools) + ["Task"]

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

### 3.11 `src/assistant/bridge/hooks.py` edits (commit 5)

Add `_validate_task_argv` after `_validate_schedule_argv` (~line 300).

```python
# Phase 6 argv gate for `python tools/task/main.py <sub>`.
_TASK_SUBCMDS: frozenset[str] = frozenset({"spawn", "list", "status", "cancel", "wait"})

_TASK_SPAWN_REQUIRED: frozenset[str] = frozenset({"--kind", "--task"})
_TASK_SPAWN_OPTIONAL: frozenset[str] = frozenset({"--callback-chat-id"})
_TASK_KINDS: frozenset[str] = frozenset({"general", "worker", "researcher"})
_TASK_TASK_MAX_BYTES = 4096
_TASK_TIMEOUT_MIN_S = 1
_TASK_TIMEOUT_MAX_S = 600


def _validate_task_argv(argv_after_script: list[str]) -> tuple[bool, str]:
    """Return (ok, reason). Called from `_validate_python_invocation` when
    script matches tools/task/main.py. Applies:
    - subcommand whitelist
    - dup-flag rejection (phase-5 B-W2-5 lesson)
    - per-sub flag whitelist
    - size/range caps
    """
    ...
```

Wire into `_validate_python_invocation`:

```python
if script == "tools/task/main.py":
    ok, reason = _validate_task_argv(argv[2:])
    if not ok:
        return _deny(f"task argv rejected: {reason}")
    return None
```

### 3.12 `src/assistant/main.py` edits (commit 7)

In `Daemon.start` after `conv` creation and BEFORE bridge creation:

```python
# Phase 6: subagent ledger store (shares ConversationStore.lock)
self._sub_store = SubagentStore(self._conn, lock=conv.lock)
recovered = await self._sub_store.recover_orphans()
if recovered:
    self._log.warning("subagent_orphans_recovered", count=recovered)
    self._spawn_bg(
        self._adapter.send_text(
            self._settings.owner_chat_id,
            f"daemon restart: {recovered} subagent(s) marked interrupted",
        ),
        name="subagent_orphan_notify",
    )

# Subagent hooks — shared factory, reused by scheduler-turn bridge too
self._subagent_pending: set[asyncio.Task[Any]] = set()
sub_hooks = make_subagent_hooks(
    store=self._sub_store,
    adapter=self._adapter,
    settings=self._settings,
    pending_updates=self._subagent_pending,
)
sub_agents = build_agents(self._settings)

# Replace existing `bridge = ClaudeBridge(self._settings)` with:
bridge = ClaudeBridge(
    self._settings,
    extra_hooks=sub_hooks,
    agents=sub_agents,
)

# Picker for CLI-spawn pickups (§3.9)
if self._settings.subagent.enabled:
    picker = SubagentRequestPicker(self._sub_store, bridge, settings=self._settings)
    self._subagent_picker = picker
    self._spawn_bg(picker.run(), name="subagent_picker")
```

In `Daemon.stop`, between existing step 2.5 (scheduler drain) and step 3
(adapter stop), add:

```python
# Step 2.6 — phase-6: drain subagent notify tasks
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

if getattr(self, "_subagent_picker", None):
    self._subagent_picker.request_stop()
```

### 3.13 `tools/task/main.py` (commit 5)

stdlib-only. `sys.path.append(<root>/src)` shim (pitfall phase-5 #8
— `append`, not `insert(0)`, inherited from phase-4 `_memlib` lesson).

Subcommands:
- `spawn --kind general --task TEXT [--callback-chat-id N]` → INSERT pending row; print `{"job_id": N, "status": "pending"}`.
- `list [--status S] [--kind K] [--limit 20]` → print JSON array.
- `status <job_id>` → full row or exit 7.
- `cancel <job_id>` → `set_cancel_requested(id)` through store; print
  `{"cancel_requested": true}` or `{"already_terminal": "<status>"}`.
- `wait <job_id> [--timeout-s 60]` → poll DB until terminal; exit 0/5.

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

## 5. Spike citations

Every non-trivial decision has an S-6-0 anchor:

- **§3.1** schema `sdk_agent_id UNIQUE`: **S-6-0 Q12** — `agent_id`
  stable across Start/Stop; `session_id` asymmetric.
- **§3.5** `build_agents` omits `"Task"` from child `tools`: **S-6-0 Q4**
  — recursion gated by child tools; structural cap.
- **§3.5** `model="inherit"` in all three: **S-6-0 Q10** — runtime-valid.
- **§3.5** per-kind `prompt` is full system prompt: **S-6-0 Q9** — FULL,
  not appended; haiku test confirmed.
- **§3.8** on_subagent_stop reads `raw["last_assistant_message"]`:
  **S-6-0 Q5** — direct hook field; no transcript parse needed.
- **§3.8** on_pretool_cancel_gate flag-poll: **S-6-0 Q7** FAIL →
  fallback required.
- **§3.8** `set[asyncio.Task]` + shield: **phase-5** B-W2-3 / HIGH #5.
- **§3.8** throttle 500ms: plan §4.5 Q4-locked.
- **§3.10** shared-factory hooks work across bridges: **S-6-0 Q6** —
  PASS, distinct agent_ids seen on shared callback.
- **§3.10** `"Task"` must be in `allowed_tools` explicitly: **S-6-0 Q2**
  — we tested with explicit; untested without.
- **§3.12** recover_orphans before bridge accepts turns:
  **phase-5** invariant (CleanState for scheduler).
- **§3.12** `drain subagent_pending` between scheduler drain and
  adapter.stop: **phase-5** HIGH #5.
- **§0 pitfall 5**: **S-6-0 Q1** — main turn wall ≈ subagent wall.

---

## 6. Open questions for devil wave-2

1. **Cancel flag-poll context** (§3.8, `on_pretool_cancel_gate`): I
   speculated that `HookContext` or `input_data` on PreToolUse from a
   SUBAGENT will contain `agent_id` — but S-6-0 did NOT verify this.
   **If PreToolUse from subagent has no `agent_id` in scope**, the
   cancel gate must fall back to `session_id → SELECT agent_id` lookup
   via store — slower and racy. Coder's first task in commit 4 should
   log a PreToolUse invocation from a subagent tool call (a spike-inside-
   implementation) and confirm which field carries the subagent identity.
2. **Main-turn "reply 'launched' and stop" realism** (§0 pitfall 5). If
   the model doesn't comply and always summarises child output, the main
   turn blocks for minutes. Does phase-6 accept this UX, or does it need
   a bridge-level workaround (e.g. `ClaudeSDKClient` with `cancel_current`
   after TaskStartedMessage)? Devil should weigh whether to tighten E2E
   scenarios 1/2 in `description.md` or add a bridge escape hatch.
3. **SubagentRequestPicker pickup → agent_id matching** (§3.9). Picker
   claims a pending row and launches a main turn; that turn spawns a
   Task → SubagentStart fires. How does the hook know the just-started
   agent_id corresponds to the picker-claimed request id? Options: (a)
   picker injects a task-text marker like `TASK_REQUEST_ID=42`, Start
   hook parses the initial user message to pick it out (fragile); (b)
   picker uses a scratch text file and matches on session_id (also
   fragile — S-6-0 Q12 says Start.session_id = parent session, not
   subagent); (c) picker lays down a flag in-memory and correlates by
   wallclock (racy). **Need a concrete answer before commit 6.** Default
   fallback: drop picker's `spawned_by_kind='cli'` attribution — just
   track the kind='cli' in the Start hook via a shared registry keyed
   on parent_session_id from picker's own bridge turn.
4. **Cost accounting** (§3.1 `cost_usd`). S-6-0 showed
   `ResultMessage.total_cost_usd` on the MAIN turn; subagent cost is
   not surfaced in `SubagentStopHookInput`. `TaskUsage.total_tokens`
   exists on `TaskNotificationMessage` but not in hook. Open: should we
   read `ResultMessage` on main turn after subagent ends to get child
   cost? That means tracking which child contributed what — out of
   scope phase-6. Devil: is `cost_usd=NULL` acceptable for phase 6, or
   must we compute it?
5. **Subagent sees the parent's PreToolUse hooks?** Plan invariant §17
   claims "Subagent's Bash/file/web tools go through SAME PreToolUse
   hooks — Q15 cheap". S-6-0 did not explicitly probe this. It's
   implicit in the cancel-gate hook design (we assume subagent tool
   calls trigger PreToolUse). Devil should ask: do we need a spike-6.5
   that confirms, or is it safe to assume from SDK docs?

---

## 7. Invariants (phase 6 canonical)

1. **`subagent_jobs.sdk_agent_id` is the single identity key.** Every
   ledger operation keys on it. Never key on `session_id`.
2. **Subagent `tools` list NEVER includes `"Task"` in phase 6.**
   Recursion cap is structural. Tests assert.
3. **`record_finished` is status-preconditioned.** `WHERE
   sdk_agent_id=? AND status='started'`. Duplicate or out-of-order
   Stop hooks are no-ops with a skew log.
4. **`recover_orphans` runs exactly once at `Daemon.start` BEFORE
   picker or bridge accepts new turns.**
5. **`Daemon.stop` drains `_subagent_pending` before `adapter.stop()`
   and `conn.close()`.**
6. **Subagent notify body uses `last_assistant_message` first,
   transcript parse only as fallback.**
7. **Cancel works via PreToolUse flag-poll only.** No SDK cancel API.
   Tool-free subagents are uncancellable (documented in skill).
8. **One hook factory per Daemon; passed to every ClaudeBridge instance.**
   Ensures cross-bridge subagent events flow through the same ledger.

---

## 8. Testing plan (details)

### 8.1 Unit tests

- `test_db_migrations_v4`: v3→v4 applies; subagent_jobs columns present.
- `test_subagent_definitions`: 3 kinds, tools lists correct, NO "Task"
  in any `tools`, model="inherit", background=True.
- `test_subagent_store`: full CRUD + state machine + orphan recovery +
  cancel flag + status-precondition skew (UPDATE on terminal row returns
  rowcount=0).
- `test_subagent_format`: notify footer exactly matches locked format;
  truncation at `max_body_bytes` respects UTF-8 char boundaries.
- `test_subagent_hooks`: mocked `input_data` dicts for Start/Stop/
  PreToolUse-cancel-gate; adapter.send_text called with formatted text;
  shield-on-cancel protects DB write.
- `test_task_cli`: spawn/list/status/cancel/wait all branches + JSON
  shape.
- `test_task_bash_hook`: `_validate_task_argv` accepts whitelist,
  rejects dup flags, rejects oversize --task, rejects OOB timeout.

### 8.2 Integration

- `test_subagent_recovery.py`: seed a `started` row → boot `Daemon` →
  observe row transitioned to `interrupted` + owner notify fired.
- `test_subagent_e2e.py`: only if `RUN_SDK_INT=1` env var — full SDK
  spawn via main turn with `general` agent; assert Start+Stop hooks
  fire, ledger row complete, adapter.send_text called.

### 8.3 Mock vs real-SDK

Unit tests inject mock dicts at hook input shape. The ONE integration
test that talks to SDK is gated by env var; CI runs it on-demand.

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

**Exit:**
- [ ] S-6-0 spike committed (`spikes/phase6_s0_native_subagent.py`,
      `spikes/phase6_s0_findings.md`, `plan/phase6/spike-findings.md`)
- [ ] Migration v4 applied, `PRAGMA user_version=4`
- [ ] `AgentDefinition` registry covers general/worker/researcher — no
      "Task" in their tools
- [ ] SubagentStart + SubagentStop + cancel-flag PreToolUse hooks wired
      through shared factory
- [ ] Telegram notify uses `last_assistant_message` directly
- [ ] CLI `tools/task/main.py` covers spawn/list/status/cancel/wait
- [ ] Bash hook gate validates task argv (dup-flag deny,
      subcmd whitelist, size caps)
- [ ] SubagentRequestPicker running as bg task; correlation to Start
      hook resolved (see §6 Q3)
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
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/system_prompt.md
- /Users/agent2/Documents/0xone-assistant/spikes/phase6_s0_native_subagent.py
- /Users/agent2/Documents/0xone-assistant/spikes/phase6_s0_findings.md
- /Users/agent2/Documents/0xone-assistant/plan/phase6/spike-findings.md
