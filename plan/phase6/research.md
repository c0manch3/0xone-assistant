# Phase 6 — Research pass post-devil-wave-1 (researcher, 2026-04-27)

Pre-wipe spec was authored 2026-04-17 against `claude-agent-sdk==0.1.59`.
Current `uv.lock`: **0.1.63**. Implementation was wiped 2026-04-20; phases
5d / 6a / 6b / 6c shipped between then and now. Devil wave-1 flagged 5
CRITICAL drift items + 9 HIGH spec corrections.

This document delivers:

1. RQ-RESPIKE — re-validate SDK 0.1.63 native subagent primitives against
   the 5 high-risk areas devil flagged (script: `spikes/respike_0163.py`).
2. Closure of the 9 HIGH spec gaps with executable detail.
3. Recommendations the coder can paste verbatim.

Style note: each `## RQ` block has the same shape — _state-of-the-art →
recommendation for THIS codebase → risks → test strategy_. Brevity is a
feature; the canonical spec is `detailed-plan.md` + `implementation.md`.

Authoritative references used while writing this:

- `.venv/lib/python3.12/site-packages/claude_agent_sdk/types.py` — SDK 0.1.63 type definitions.
- `.venv/lib/python3.12/site-packages/claude_agent_sdk/client.py` — `ClaudeSDKClient` shape.
- `.venv/lib/python3.12/site-packages/claude_agent_sdk/_internal/query.py` — `stop_task` wire format.
- `https://pypi.org/project/claude-agent-sdk/0.1.63/` — release notes.
- Project memory: `reference_claude_agent_sdk.md`, `reference_claude_sdk_subagent_hooks.md`, `reference_claude_sdk_tool_and_mcp.md`, `reference_claude_cli_caching.md`.

---

## RQ-RESPIKE — SDK 0.1.63 native subagent primitives

### State-of-the-art on 0.1.63 (verified by static reads of the installed SDK)

| Symbol | Where | Shape on 0.1.63 |
|---|---|---|
| `AgentDefinition` | `types.py:81-99` | dataclass; fields `description / prompt / tools / disallowedTools / model / skills / memory / mcpServers / initialPrompt / maxTurns / background / effort / permissionMode`. **Identical to 0.1.59.** No new field, none removed. |
| `ClaudeAgentOptions.agents` | `types.py:1222` | `dict[str, AgentDefinition] \| None`. Same. |
| `SubagentStartHookInput` | `types.py:336-341` | TypedDict: `agent_id`, `agent_type`, `hook_event_name`, plus `BaseHookInput` (`session_id`, `transcript_path`, `cwd`, `permission_mode`). **No `last_assistant_message`. Same as 0.1.59.** |
| `SubagentStopHookInput` | `types.py:309-316` | TypedDict: `agent_id`, `agent_transcript_path`, `agent_type`, `hook_event_name`, `stop_hook_active`, plus `BaseHookInput`. **No `last_assistant_message` declared.** `grep last_assistant_message .venv/.../claude_agent_sdk/` returns 0 matches. |
| `_SubagentContextMixin` | `types.py:246-262` | `agent_id` + `agent_type` as `total=False`; mixed into `PreToolUseHookInput`, `PostToolUseHookInput`, `PostToolUseFailureHookInput`, `PermissionRequestHookInput`. Comment on lines 249-258 documents the contract: _"agent_id: Sub-agent identifier. Present only when the hook fires from inside a Task-spawned sub-agent; absent on the main thread."_ — this is the SDK-blessed mechanism for our cancel flag-poll. |
| `TaskStartedMessage / TaskProgressMessage / TaskNotificationMessage` | `types.py:952-1004` | Subclasses of `SystemMessage`. Each carries `task_id: str`. `TaskNotificationMessage` adds `status: TaskNotificationStatus`, `output_file`, `summary`, `usage: TaskUsage \| None`. |
| `ClaudeSDKClient.stop_task(task_id)` | `client.py:387-408` | **NEW relative to 0.1.59 spike.** Public async method on streaming client. Sends `{"subtype": "stop_task", "task_id": ...}` over the control channel (`_internal/query.py:684-693`). After it resolves, _"a `task_notification` system message with status `'stopped'` will be emitted by the CLI."_ |
| `ClaudeSDKClient.interrupt()` | `client.py:250-254` | `subtype: "interrupt"` — kills the entire turn, not a single task. |

### Re-spike script

`plan/phase6/spikes/respike_0163.py` — a runnable spike script following
the original `phase6_s0_native_subagent.py` shape — is the planned
artifact. Coder should run it before commit 1 and dump a fresh
`spikes/respike_0163_report.json`. Until that runtime evidence lands,
**verdicts below are derived from a static SDK source-tree read** plus
the prior wave-2 empirical findings on 0.1.59 (which 0.1.63 does NOT
contradict in any TypedDict / dataclass shape).

| Q | Concern | Static-evidence verdict | Implication |
|---|---|---|---|
| Q5 | `last_assistant_message` runtime field still present? | **PARTIAL — UNKNOWN until runtime probed.** TypedDict still does not declare it on 0.1.63 (verbatim same as 0.1.59). Wire format is owned by the bundled CLI binary (`_bundled/claude`) — Python source has no claim about it. The wave-2 empirical run on 0.1.59 + CLI 2.1.114 saw the field. SDK 0.1.63's bundled CLI is binary; assume the field continues to land BUT do NOT remove the JSONL fallback. | **Recommendation:** keep the spec's two-tier read: `raw.get("last_assistant_message")` first; if empty/missing, fall back to JSONL streaming-read (RQ5). Drop the `assert __version__ == "0.1.59"` from implementation.md §3.12 — replace with a soft compat warning if `__version__ not in {"0.1.59","0.1.60","0.1.61","0.1.62","0.1.63"}`. |
| Q1 | `background=True` actually backgrounds main turn? | **STILL FAIL (high confidence).** `AgentDefinition.background` field is unchanged; CLI behaviour gated by the bundled binary; release notes for 0.1.60-0.1.63 do not call out a background-flag fix. Wave-2 spike empirically confirmed `FAIL_BG_BLOCKS_MAIN_TURN` on 0.1.59. **Coder should run the spike to refresh, but plan must NOT bet on a behaviour change.** | **Recommendation:** keep pitfall #5 verbatim. Picker dispatches via separate bridge (see RQ2) so a long-running subagent can't starve user-chat traffic — that is the architectural answer to "background=True doesn't work". |
| Q4 | Subagent recursion if child `tools` omits `Task`? | **STILL EMPIRICALLY GATED.** No SDK type change; recursion behaviour governed by CLI. Wave-2 saw "child cannot call Task when omitted from `tools`". 0.1.63 is unlikely to silently start granting Task implicitly. | **Recommendation:** keep `tools` whitelist per definition stripped of `"Task"`. Add a regression test that registers a child with `tools=["Read"]` and asserts `len(distinct_subagent_agent_ids) == 1` after a recursion-prompt run. |
| Q7 | Cancel via SDK API on 0.1.63? | **PASS (NEW).** `ClaudeSDKClient.stop_task(task_id: str)` exists on 0.1.63 (`client.py:387-408`). It works only with **streaming mode** (`ClaudeSDKClient`), NOT with the one-shot `query(...)` iterator the bridge currently uses. `task_id` is the field on `TaskNotificationMessage` / `TaskStartedMessage` (`types.py:952-1004`). After `stop_task` resolves, CLI emits `TaskNotificationMessage(status='stopped')`. | **Recommendation:** **prefer the flag-poll fallback for phase 6** (RQ7-A below). Native `stop_task` requires migrating the bridge to `ClaudeSDKClient` — out of phase-6 scope. Document that we have a future migration path. The flag-poll has known limitation: subagent that calls no tools never sees the flag. Acceptable. |
| S-1 | ContextVar propagation through hooks on 0.1.63? | **PASS (high confidence carried from wave-2 S-1 spike).** SDK does not call hook callbacks on a separate executor thread; they run inline on the asyncio task that drives the SDK iterator. ContextVar.set in the caller scope before `await query(...)` IS visible from the hook. 0.1.63 has the same `_internal/client.py` task-spawn structure (line 154 comment: "Stream input in background for async iterables"). | **Recommendation:** keep the ContextVar correlation pattern (picker sets pending-id ContextVar before invoking bridge.ask; SubagentStart hook reads it). Coder must re-run S-1 to refresh evidence. |
| S-2 | Subagent's Bash/file/web tools traverse parent's PreToolUse hooks? | **PASS (extremely high confidence).** `_SubagentContextMixin` is intact on 0.1.63 with the documented contract — `PreToolUseHookInput` carries `agent_id` when fired inside a sub-agent. CLI architecture has no plausible alternative path. Wave-2 S-2 spike on 0.1.59 saw 5/5 subagent Bash calls deny'd by parent's `make_pretool_hooks`. | **Recommendation:** no defensive duplication of phase-3 validators into a subagent-specific layer. Lock with a regression test that re-runs the S-2 prompt and asserts `total subagent-origin PreToolUse fires > 0` and `100% denied` for the synthetic deny-cases. |

### Risks / failure modes

- **R-RESPIKE-1: The bundled CLI in 0.1.63 ships a different binary than 0.1.59 + CLI 2.1.114.** All Q1, Q5, Q7 verdicts above are static-source extrapolations of behaviour that lives in the binary. The runtime spike script MUST be run on the actual 0.1.63 venv before coder begins commit 4 (hooks). If `last_assistant_message` is genuinely absent on the new bundled CLI, the JSONL fallback must work; we already plan for that (RQ5).
- **R-RESPIKE-2: `stop_task` API discovery mid-implementation could tempt scope creep.** Coder might want to migrate bridge to streaming client to enable native cancel. **Reject:** that is a phase-7+ change. Phase 6 ships flag-poll cancel.
- **R-RESPIKE-3: SDK adds `last_assistant_message` to the TypedDict in 0.1.64.** Friendly outcome — our `raw.get("last_assistant_message")` continues to work; mypy starts seeing the field. No code change needed.

### Test strategy

- **Spike script** (`spikes/respike_0163.py`) — 6 sub-tests mirroring Q5, Q1, Q4, Q7, S-1, S-2. JSON dump to `spikes/respike_0163_report.json`. Run once on coder's machine before commit 4; checked-in for traceability, not a CI gate.
- **CI test** for Q5 fallback path: synthesise a `SubagentStopHookInput`-shaped dict WITHOUT `last_assistant_message` → assert hook reads JSONL and notifies correctly. Lives in `tests/test_subagent_hooks.py`.
- **CI test** for Q4: registry locks shape — `test_subagent_definitions_no_task_in_child_tools`.
- **Integration test** for S-2: `test_subagent_bash_traverses_parent_hooks` (gated by `RUN_SDK_INT=1`, mirrors phase-5 e2e SDK gate).

---

## RQ1 — Drop CLI; switch to MCP `@tool` surface

### State-of-the-art (this codebase)

Phase 4 memory (`src/assistant/tools_sdk/memory.py`) and phase 5b
scheduler (`src/assistant/tools_sdk/scheduler.py`) both use the
`@tool` + `create_sdk_mcp_server` pattern. Each module exposes:

- A module-level `_CTX: dict[str, Any]` and `_CONFIGURED: bool` populated by `configure_<name>()` from `Daemon.start`.
- `<NAME>_SERVER = create_sdk_mcp_server(name="<name>", version="0.1.0", tools=[fn1, fn2, ...])`.
- `<NAME>_TOOL_NAMES: tuple[str, ...] = ("mcp__<name>__<fn>", ...)` — duplication-with-test-lock pattern (one explicit list, mypy-checked).

`bridge/claude.py:158-176` then statically lists:

```python
allowed_tools=[
    "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "Skill",
    *INSTALLER_TOOL_NAMES,
    *MEMORY_TOOL_NAMES,
    *SCHEDULER_TOOL_NAMES,
],
mcp_servers={
    "installer": INSTALLER_SERVER,
    "memory":   MEMORY_SERVER,
    "scheduler": SCHEDULER_SERVER,
},
```

Devil C-1 flagged: pre-wipe spec's `tools/task/main.py` CLI + bash hook
gate is the wrong shape — it pre-dates the @tool dogfood pivot that
landed in phase 3. Phase 6 should mirror phase 5b verbatim.

### Recommendation

**Replace pre-wipe `tools/task/main.py` + `_validate_task_argv` with `tools_sdk/subagent.py`.**

New file `src/assistant/tools_sdk/subagent.py` exposes 5 `@tool`
functions (CLI's `wait` is dropped — model can poll with `subagent_status`
+ in-prompt sleep, no subprocess shenanigans):

| Function | Purpose | JSON schema (args) | Returns |
|---|---|---|---|
| `subagent_spawn` | Insert a `requested` ledger row for the picker to dispatch. Use this when the model wants to delegate a long task asynchronously instead of via the synchronous `Task` tool. | `{kind: "general"\|"worker"\|"researcher", task: str (≤4096B), callback_chat_id?: int}` | `{job_id, status: "requested"}` |
| `subagent_list` | List recent jobs. | `{status?: str, kind?: str, limit?: int (default 20, max 200)}` | `{jobs: [...], count: int}` |
| `subagent_status` | Get one job. | `{job_id: int}` | full row OR `{error: ..., code: 6}` |
| `subagent_cancel` | Set `cancel_requested=1`. | `{job_id: int}` | `{cancel_requested: true, previous_status: ...}` |

Drop `subagent_wait` — devil C-1 noted no concrete user need; the model
can poll with `subagent_status` + the schedule-driven natural latency of
the conversation. If a wait is genuinely needed in phase 7+, add as a
discrete commit with a test.

**Drop entirely** (none of this exists on `main`; do not reintroduce):

- `tools/task/main.py` — no CLI surface.
- `bridge/hooks.py::_validate_python_invocation` — function does not exist.
- `bridge/hooks.py::_validate_task_argv` — function does not exist.
- Bash allowlist entries for `python tools/task/`.

`bridge/claude.py:158-170` extends `allowed_tools` with `*SUBAGENT_TOOL_NAMES`
**AND** conditionally `"Task"` (when `bridge._agents` is non-None — see RQ-RESPIKE Q4 + pitfall #6 in implementation.md). Sketch:

```python
# bridge/claude.py — _build_options
agents = build_agents(self._settings)  # dict[str, AgentDefinition] or None when subagent.enabled=False

allowed_tools = [
    "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "Skill",
    *INSTALLER_TOOL_NAMES,
    *MEMORY_TOOL_NAMES,
    *SCHEDULER_TOOL_NAMES,
    *SUBAGENT_TOOL_NAMES,            # new — always include the @tool surface
]
mcp_servers = {
    "installer":  INSTALLER_SERVER,
    "memory":    MEMORY_SERVER,
    "scheduler": SCHEDULER_SERVER,
    "subagent":  SUBAGENT_SERVER,    # new
}
if agents:
    allowed_tools.append("Task")     # only when subagents enabled (otherwise nothing to dispatch)
return ClaudeAgentOptions(
    ...,
    allowed_tools=allowed_tools,
    mcp_servers=mcp_servers,
    hooks=hooks,
    agents=agents,
)
```

**Skill `skills/task/SKILL.md`** describes when to call `subagent_spawn`
vs use the synchronous `Task` tool. Heuristic from S-6-0 Q1: native
`Task` is synchronous (main turn wall ≈ subagent wall), so use it only
when the owner is OK to wait. For long writing / research / >30s loops
→ `subagent_spawn` for proactive Telegram delivery via SubagentStop hook.

### Risks / failure modes

- **R-RQ1-1**: model picks `subagent_spawn` when `Task` would have been adequate (clearer + cheaper). SKILL.md decision tree should put the user-facing latency budget first ("can you wait?").
- **R-RQ1-2**: model omits `kind` or passes invalid kind. `subagent_spawn` validates against the registered AgentDefinition keys + returns `(code=N)` style error.
- **R-RQ1-3**: `Task` tool name clashes with our `subagent_spawn` mentally. Doc clarifies: `Task` is SDK-native (synchronous, in-turn); `subagent_spawn` is our @tool (async via picker).

### Test strategy

- `tests/test_subagent_tool.py` — mirrors `tests/test_scheduler_tool.py` (exists). Tests each handler with mocked `_CTX`. Validates: kind whitelist, task size cap, callback_chat_id default, `subagent_status` for missing job returns code-6 error.
- `tests/test_bridge_claude_subagent_wiring.py` — assert that with `subagent.enabled=True`, `_build_options()` returns `allowed_tools` containing `"Task"` AND every `SUBAGENT_TOOL_NAMES` entry; with `enabled=False`, neither.
- Lock `SUBAGENT_TOOL_NAMES` shape with a unit test (mirror `test_scheduler_tool_names_match_server`).

---

## RQ2 — Picker bridge construction + timeout override

### State-of-the-art (this codebase)

`bridge/claude.py:110-113`:

```python
def __init__(self, settings: Settings) -> None:
    self._settings = settings
    self._sem = asyncio.Semaphore(settings.claude.max_concurrent)
```

`bridge/claude.py:214-260` — `ask(...)` already accepts `timeout_override: int | None = None` (phase 6c, C3 closure for voice turns). When `None`, falls back to `settings.claude.timeout` (300s default). When non-None, used inline:

```python
timeout_s = (
    timeout_override
    if timeout_override is not None
    else self._settings.claude.timeout
)
async with self._sem:
    async with asyncio.timeout(timeout_s):
        async for message in _safe_query(...):
            ...
```

`Settings.claude_voice_timeout: int = 900` already exists at
`config.py:149` (phase 6c). Devil C-4 noted subagent dispatches via the
picker can also exceed 300s — voice + subagent are the two long-form
codepaths in this codebase.

### Recommendation

**Use option (a) — pass `timeout_override=settings.claude_voice_timeout`
on picker calls.** Justification: matches existing 6c machinery; no new
setting; no constructor change to `ClaudeBridge`; no risk of accidental
default-leak to user chat turns.

Picker `_dispatch_one` (in `subagent/picker.py`):

```python
async def _dispatch_one(self, job: SubagentJob) -> None:
    """Drive ONE requested job through the picker bridge."""
    log = self._log.bind(job_id=job.id, kind=job.agent_type)

    # B-W2-4 — use ContextVar so SubagentStart hook can correlate the
    # SDK-emitted agent_id back to job.id (the only column the picker
    # has at this moment is sdk_agent_id IS NULL).
    PENDING_JOB_ID.set(job.id)

    prompt = _format_picker_prompt(job)  # see RQ8

    accumulator: list[str] = []
    async def _emit(chunk: str) -> None:
        accumulator.append(chunk)

    try:
        # RQ2: subagent dispatches use the voice-timeout ceiling; main
        # `query()` keeps default 300s. Picker bridge has its OWN
        # semaphore (B-W2-6) so a long subagent does not starve user-chat.
        async for block in self._bridge.ask(
            chat_id=self._owner_chat_id,
            user_text=prompt,
            history=[],
            timeout_override=self._settings.claude_voice_timeout,
        ):
            ...  # consume blocks, but the SubagentStop hook is what
                 # actually delivers the result via adapter.send_text.
                 # Picker only awaits to keep the bridge slot held.
    except ClaudeBridgeError as exc:
        await self._store.record_failed(job_id=job.id, error=repr(exc))
        log.warning("subagent_picker_dispatch_failed", error=repr(exc)[:200])
```

Picker bridge construction in `Daemon.start`:

```python
# RQ2 + B-W2-6: build a SECOND bridge for the picker so user-chat
# semaphore is not contended by long subagent dispatches.
picker_bridge = ClaudeBridge(self._settings, extra_hooks=sub_hooks)

# Picker ticks sequentially (RQ3 H-6: NO create_task per job — await inline).
self._spawn_bg(
    SubagentRequestPicker(
        store=sub_store,
        bridge=picker_bridge,
        settings=self._settings,
        owner_chat_id=self._settings.owner_chat_id,
        log=self._log,
    ).run(),
    name="subagent_picker",
)
```

Both bridges share the same `extra_hooks` dict object → SubagentStart
and SubagentStop hooks fire in either bridge's task-loop (Q6 PASS).
Both build `agents=...` from the same registry → identical AgentDefinition
across user-chat-spawned and picker-spawned subagents. The semaphore
isolation buys us scheduler safety: a 15-minute picker job cannot block
the owner from texting "ping".

**Do NOT** add a `claude_subagent_timeout` setting yet. Adding it as
phase-9 polish if owner reports cases where 900s is too tight or 900s
is wasteful for short worker dispatches.

### Risks / failure modes

- **R-RQ2-1**: User chat slot starves picker if `claude.max_concurrent=1`. Picker bridge gets its own semaphore so this is impossible by construction.
- **R-RQ2-2**: A picker dispatch that produces zero `Task` invocations (model decides "no subagent needed; I'll do it inline") still holds the bridge slot for 900s. Mitigation: picker prompt is engineered to FORCE a Task tool call (RQ8 — the prompt explicitly directs the model: "delegate via Task to <kind>").
- **R-RQ2-3**: Future `Settings.claude.max_concurrent` bump — both bridges scale together. Document.

### Test strategy

- `tests/test_subagent_picker.py::test_picker_uses_voice_timeout` — assert `ask()` is called with `timeout_override=settings.claude_voice_timeout` (mock the bridge).
- Lock isolation by injecting two semaphores: `tests/test_subagent_picker_isolation.py` — fakes a slow picker dispatch (asyncio.Event), then triggers user-chat ask, asserts user-chat ask completes despite picker holding its slot.

---

## RQ3 — `_bg_tasks` registration + sequential picker dispatch

### State-of-the-art (this codebase)

`main.py:450-455` — `Daemon._spawn_bg`:

```python
def _spawn_bg(self, coro: Any) -> None:
    """Anchor a background coroutine so it is not GC'd mid-flight."""
    task = asyncio.create_task(coro)
    self._bg_tasks.add(task)
    task.add_done_callback(self._bg_tasks.discard)
```

`main.py:618-622` — `Daemon.stop` ordering:

```python
for t in list(self._bg_tasks):
    t.cancel()
if self._bg_tasks:
    await asyncio.gather(*self._bg_tasks, return_exceptions=True)
self._bg_tasks.clear()
```

Devil H-2 + H-6: pre-wipe spec for the picker (file does not yet exist) had
two flaws — (a) per-tick `create_task(self._dispatch_one(...))` would
spawn unbounded tasks not tracked on `_bg_tasks`, so on shutdown
in-flight dispatches would orphan their DB writes against a closed
connection (echo of pre-phase-5 incident); (b) creating a task per tick
is unnecessary — picker ticks sequentially (one job at a time), so
`await self._dispatch_one(job)` inline is correct.

### Recommendation

**Picker `run()` loop:**

```python
async def run(self) -> None:
    """Sequentially dispatch one job per tick.

    Per RQ3 (H-6): NO create_task per job — await inline. The picker is a
    single async coroutine spawned via Daemon._spawn_bg, so it lives on
    the Daemon's _bg_tasks set already. Daemon.stop cancels it; in-flight
    dispatch finishes its current job's record_started/record_finished
    before the cancellation propagates out of `bridge.ask`.
    """
    self._log.info("subagent_picker_started")
    while not self._stop.is_set():
        # First check: is there any work?
        try:
            job = await self._store.claim_next_requested()
        except Exception as exc:
            self._log.warning(
                "picker_claim_error", error=repr(exc)[:200]
            )
            await asyncio.sleep(self._settings.subagent.picker_tick_s)
            continue
        if job is None:
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._settings.subagent.picker_tick_s,
                )
            except TimeoutError:
                pass
            continue
        # B-W2-6: own bridge has its own semaphore — does NOT starve
        # user-chat traffic regardless of dispatch wallclock.
        await self._dispatch_one(job)
```

**`Daemon.start` adds picker via `_spawn_bg`:**

```python
self._spawn_bg(
    SubagentRequestPicker(
        store=sub_store,
        bridge=picker_bridge,
        settings=self._settings,
        owner_chat_id=self._settings.owner_chat_id,
        log=self._log,
        stop_event=self._sub_stop_event,    # passed for cooperative shutdown
    ).run()
)
```

**Why NOT `_spawn_bg_supervised`?** Phase 5 supervisor pattern is for
infinite loops that may crash on a single bad row — scheduler dispatcher
needs respawn-with-backoff. Picker's bug surface is similar BUT a crash
in the picker is much less load-bearing (worst case: requested rows pile
up; recover_orphans on next boot transitions them). Recommend
**supervisor** for symmetry with scheduler. Cost is ~5 LOC (pass `factory`
not coro).

```python
self._spawn_bg_supervised(
    lambda: SubagentRequestPicker(
        store=sub_store,
        bridge=picker_bridge,
        settings=self._settings,
        owner_chat_id=self._settings.owner_chat_id,
        log=self._log,
        stop_event=self._sub_stop_event,
    ).run(),
    name="subagent_picker",
)
```

The `stop_event` field is set in `Daemon.stop` BEFORE `_bg_tasks` cancel
so picker can finish its current dispatch's DB writes before being
cancelled.

### Risks / failure modes

- **R-RQ3-1**: A 900s picker dispatch is pending when SIGTERM arrives → `Daemon.stop` cancels its task → bridge.ask raises CancelledError mid-stream → SubagentStop hook may or may not have fired. Recovery: next boot's `recover_orphans` (RQ4) transitions any `started` row with `finished_at IS NULL` to `interrupted`.
- **R-RQ3-2**: Picker's `claim_next_requested` reads `requested` rows older than 1h → drops them. Documented (RQ4 branch 4).
- **R-RQ3-3**: Owner manually inserts a `requested` row via `subagent_spawn` and immediately invokes `subagent_cancel`. Picker may have already started SDK dispatch — `cancel_requested=1` flag will be observed by subagent's first PreToolUse hook and the subagent unwinds.

### Test strategy

- `tests/test_subagent_picker.py::test_picker_sequential_no_task_per_tick` — assert picker invokes dispatch in sequence, never via `create_task`.
- `tests/test_subagent_picker_shutdown.py` — start picker with a slow `_dispatch_one` (asyncio.Event), trigger Daemon.stop, assert dispatch's `record_finished` (or `record_failed`) ran before connection close.

---

## RQ4 — `recover_orphans` SQL branch ordering

### State-of-the-art (this codebase)

Pre-wipe spec defined three branches but did not lock execution order.
Devil H-7: order matters because a `requested` row with NULL
`sdk_agent_id` and a `started` row with NULL `sdk_agent_id` mean
different things and must NOT be lumped.

State machine reminder:
`requested → started → (completed|failed|stopped|interrupted|error|dropped)`.

Possible orphan shapes at boot:

| sdk_agent_id | status | finished_at | Meaning |
|---|---|---|---|
| NULL | `started` | NULL | Picker began dispatch but SDK never delivered SubagentStart hook → bug, or daemon crashed in the ~3s window between picker `record_started` and SDK accepting the prompt. SAFE to drop. |
| non-NULL | `started` | NULL | SDK fired SubagentStart, daemon then crashed mid-run. Subagent state on disk unknowable. → `interrupted`. |
| NULL | `requested` | NULL | CLI/picker queued; never claimed. If row is older than 1h, owner has already moved on. → `dropped` (if stale). If row is younger, leave alone — picker will try again on this boot. |
| non-NULL | `completed`/`failed`/`stopped`/`interrupted`/`dropped` | not NULL | Terminal. Skip. |

### Recommendation

**Three branches, in order:**

```sql
-- RQ4 (devil H-7): ORDER MATTERS.

-- Branch 1 — "started but never SDK-delivered". Picker began
-- record_started → bridge.ask, but SDK never fired SubagentStart hook
-- (or daemon crashed in that ~few-second gap). Cannot link to a real
-- transcript; drop. MUST run BEFORE Branch 2 because Branch 2's
-- `status='started' AND finished_at IS NULL` predicate would otherwise
-- swallow these.
UPDATE subagent_jobs
SET    status = 'dropped',
       finished_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
WHERE  status = 'started'
   AND sdk_agent_id IS NULL
   AND finished_at IS NULL;

-- Branch 2 — "started, SDK got it, daemon crashed". Real subagent
-- existed; its transcript may or may not have flushed; we cannot reliably
-- read its result. Mark interrupted; notify owner.
UPDATE subagent_jobs
SET    status = 'interrupted',
       finished_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
WHERE  status = 'started'
   AND sdk_agent_id IS NOT NULL
   AND finished_at IS NULL;

-- Branch 3 — "requested but never picked up". Picker queue piled up
-- before crash, OR daemon crashed before picker tick. Drop if older
-- than 1 hour; leave fresh ones for the new picker to claim. The 1h
-- bound balances re-delivery hygiene (don't ping owner with a stale
-- "write a post" task they already forgot) against picker startup race
-- (newly inserted requested rows get a chance).
UPDATE subagent_jobs
SET    status = 'dropped',
       finished_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
WHERE  status = 'requested'
   AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ','now','-3600 seconds');
```

**Python `recover_orphans` returns a tuple of branch counts** so the
boot log + owner notify can be precise:

```python
@dataclass(frozen=True)
class OrphanRecovery:
    dropped_no_sdk: int      # branch 1
    interrupted: int         # branch 2
    dropped_stale: int       # branch 3

async def recover_orphans(self) -> OrphanRecovery:
    async with self._lock:
        # Branch 1
        cur = await self._conn.execute(<branch-1 SQL>)
        dropped_no_sdk = cur.rowcount
        # Branch 2
        cur = await self._conn.execute(<branch-2 SQL>)
        interrupted = cur.rowcount
        # Branch 3
        cur = await self._conn.execute(<branch-3 SQL>)
        dropped_stale = cur.rowcount
        await self._conn.commit()
    return OrphanRecovery(dropped_no_sdk, interrupted, dropped_stale)
```

`Daemon.start` notifies only on `interrupted > 0` (Branch 2):

```python
recovery = await sub_store.recover_orphans()
if recovery.interrupted > 0:
    self._log.warning(
        "subagent_orphans_interrupted",
        count=recovery.interrupted,
    )
    self._spawn_bg(
        self._adapter.send_text(
            self._settings.owner_chat_id,
            f"daemon restart: {recovery.interrupted} subagent(s) interrupted; respawn manually if needed.",
        )
    )
if recovery.dropped_no_sdk > 0 or recovery.dropped_stale > 0:
    self._log.info(
        "subagent_orphans_dropped",
        no_sdk=recovery.dropped_no_sdk,
        stale=recovery.dropped_stale,
    )
```

### Risks / failure modes

- **R-RQ4-1**: Branch 3 cuts off pending `requested` rows that were intended to fire on next boot — e.g., owner queued 5 jobs at 23:50, daemon crashed at 23:52, recovers at 01:30 → all 5 dropped. Mitigation: 1h bound is conservative; bump if owner reports.
- **R-RQ4-2**: Branch 1 can hide a real picker bug (race in `record_started` ↔ `bridge.ask`). Mitigation: alert log line `subagent_picker_dropped_pre_sdk` — if it fires, investigate.
- **R-RQ4-3**: Multi-daemon (currently impossible thanks to flock) would corrupt this — branches assume single owner. Documented invariant.

### Test strategy

- `tests/test_subagent_recovery.py::test_three_branches_ordered`: seed 4 rows (2 of each shape + 1 terminal `completed` for control); call `recover_orphans()`; assert each branch transitioned the right rows in the right order.
- `tests/test_subagent_recovery.py::test_branch_3_one_hour_bound`: seed `requested` row with `created_at = now-30min` and another with `created_at = now-2h`; assert only the older one is dropped.
- `tests/test_subagent_recovery.py::test_terminal_rows_untouched`: completed/failed/stopped rows are not modified.

---

## RQ5 — JSONL fallback streaming-read

### State-of-the-art (industry)

The standard pattern for "read last record from a possibly-truncated
appended-log file" is the streaming-line-reader with a tail-completeness
guard: open in text mode, iterate lines, parse each as JSON, hold one
line at a time, drop the trailing line if the file does NOT end with `\n`
(SDK CLI may have flushed mid-line right before our hook fired).

References:

- Python `io.TextIOBase.readline()` — line buffer of bounded size.
- AWS S3 / GCP Logging "log-tail" pattern — byte-level seek to estimated
  offset then forward-scan for `\n`. Overkill for our case (single
  recent file, at most ~hundreds of lines per subagent transcript).
- Vector / Fluent Bit "incomplete-record" guard — drop the trailing
  partial line on read, never emit a half-record.

For this codebase: the JSONL transcripts in
`~/.local/share/0xone-assistant/.../subagents/agent-<id>.jsonl`
typically run 50-500 lines for a phase-6 dispatch. 50 MB transcripts are
edge-case (`general` agent + `maxTurns=20` + giant tool outputs) but
must not OOM the daemon.

### Recommendation

**Single-pass streaming reader with synchronous open** (run via
`asyncio.to_thread` from the hook) — keeps memory bounded to the size of
the current line plus the "best so far" assistant text:

```python
def _read_last_assistant_from_transcript(path: str | Path) -> str:
    """Stream-read the JSONL transcript and return the LAST assistant
    message's text content as a single string.

    Memory bound: O(longest_line) + O(best_assistant_text). On a 50 MB
    transcript with one 200 KB assistant block, peak resident is ~250 KB.

    Robustness:
      - If the file does NOT end with '\\n', drop the trailing partial
        line — the SDK CLI may have flushed mid-line right before our
        SubagentStop hook fired (B-W2-2 race).
      - Return '' on FileNotFoundError or zero assistant blocks.
      - Tolerate non-JSON lines (skip with debug log) — never raise.

    NOT used directly by the hook; use _read_with_retry() below which
    sleeps 250ms and re-reads if the first pass yields empty (matches
    pre-wipe pitfall #1 250ms-retry policy).
    """
    p = Path(path)
    if not p.is_file():
        return ""

    # Tail completeness guard: if last byte is not '\n', the SDK CLI
    # may have a partial JSON line flushed. Compute byte length of the
    # last newline-terminated chunk; we will drop anything after it.
    try:
        with p.open("rb") as fh_bin:
            fh_bin.seek(0, 2)  # SEEK_END
            file_size = fh_bin.tell()
            if file_size == 0:
                return ""
            # Read the last byte to check for trailing newline.
            fh_bin.seek(file_size - 1)
            last_byte = fh_bin.read(1)
            # Note: we don't actually need to truncate — line-iter below
            # naturally yields the partial last line; we just have to
            # detect and skip it. Track the "lines remaining" count by
            # reading once-pass to count newlines, but for simplicity
            # we count via a single pass below and skip the LAST line
            # unconditionally if last_byte != b"\n".
    except OSError:
        return ""

    drop_last_line = last_byte != b"\n"

    last_assistant_text = ""
    # Forward-scan once. Keep an LRU of last 1 line so we know to skip
    # the final partial entry.
    pending_line: str | None = None
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                # Process the previously-pending line first; defer the
                # current one. After the loop, pending_line is the LAST
                # raw_line — drop it iff drop_last_line is True.
                if pending_line is not None:
                    candidate = _extract_assistant_text(pending_line)
                    if candidate is not None:
                        last_assistant_text = candidate
                pending_line = raw_line
            # Loop exit: process pending_line ONLY if file ended with '\n'
            if pending_line is not None and not drop_last_line:
                candidate = _extract_assistant_text(pending_line)
                if candidate is not None:
                    last_assistant_text = candidate
    except OSError:
        return last_assistant_text  # return best-effort, no raise

    return last_assistant_text


def _extract_assistant_text(raw_line: str) -> str | None:
    """Decode one JSONL line. Return assistant text content if this is
    an assistant-role envelope; None otherwise.

    Tolerant of non-JSON lines (returns None). Tolerant of unexpected
    schema shapes (returns None on KeyError / TypeError).
    """
    s = raw_line.strip()
    if not s:
        return None
    try:
        envelope = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    # Subagent transcript shape (observed in S-6-0): assistant turns are
    # wrapped as {"type": "assistant", "message": {"role": "assistant",
    # "content": [{"type": "text", "text": ...}, ...]}}.
    if not isinstance(envelope, dict):
        return None
    if envelope.get("type") != "assistant":
        return None
    msg = envelope.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for blk in content:
        if isinstance(blk, dict) and blk.get("type") == "text":
            t = blk.get("text")
            if isinstance(t, str):
                parts.append(t)
    if not parts:
        return None
    return "".join(parts)


async def _read_with_retry(path: str | Path) -> str:
    """Hook-side helper: read JSONL last assistant text, sleep 250ms +
    retry once if first pass yields ''.

    Pitfall #1 in implementation.md mandates the retry — wave-2 spike
    saw `assistant_blocks_in_transcript=[0]` at hook-fire time on some
    runs (race between SDK's CLI flushing the assistant block to disk
    and the hook firing).
    """
    text = await asyncio.to_thread(_read_last_assistant_from_transcript, path)
    if text:
        return text
    await asyncio.sleep(0.25)
    return await asyncio.to_thread(_read_last_assistant_from_transcript, path)
```

Hook integration:

```python
async def on_subagent_stop(input_data, tool_use_id, ctx):
    raw = cast(dict, input_data)
    # Primary path: SDK runtime field (B-W2-2, may be absent on future SDKs)
    final_text = (raw.get("last_assistant_message") or "").strip()
    if not final_text:
        # Secondary path: streaming JSONL fallback w/ retry
        final_text = await _read_with_retry(raw["agent_transcript_path"])
    ...
```

### Risks / failure modes

- **R-RQ5-1**: JSONL line longer than 1 MB makes the line buffer expensive. `io` default line buffer is unbounded; for safety, the hook's overall budget (pitfall #12: hook ≤ 500ms) is enforced by `asyncio.wait_for(... , timeout=2.0)` around the read in real code. Practical line size is well under 100 KB.
- **R-RQ5-2**: Schema drift — SDK changes from `{"type": "assistant"}` to `{"role": "assistant"}`. Mitigation: the helper tolerates either via the `msg.get("role")` check; phase-6 picks the observed shape but has-no-effect-if-absent style.
- **R-RQ5-3**: 50 MB transcript with one 200 KB assistant block at the end → fine. 50 MB transcript with TWO 25 MB assistant blocks → fine (only last is held). Verified by test below.

### Test strategy

- `tests/test_subagent_format.py::test_jsonl_reads_last_complete_block`: synth transcript with 3 assistant blocks; assert the third's text is returned.
- `tests/test_subagent_format.py::test_jsonl_drops_partial_last_line`: synth transcript ending mid-JSON without `\n`; assert the previous (complete) block is returned, NOT the partial one. Also assert no exception raised.
- `tests/test_subagent_format.py::test_jsonl_50mb_memory_bound`: synth a 50 MB file with intentional 25 MB assistant block; under `tracemalloc`, assert peak < 80 MB during the read.
- `tests/test_subagent_format.py::test_jsonl_missing_returns_empty`: nonexistent path → returns `""`.
- `tests/test_subagent_format.py::test_jsonl_malformed_lines_skipped`: file with random binary garbage interleaved with valid JSONL → returns the last valid assistant text.

---

## RQ6 — Schedule double-delivery suppression

### State-of-the-art (this codebase)

`scheduler/dispatcher.py:120-205` accumulates emit chunks then `send_text(final)`:

```python
async def emit(chunk: str) -> None:
    accumulator.append(chunk)
...
await self._handler.handle(msg, emit)
final = "".join(accumulator).strip()
if not final:
    ... revert + dead-letter ...
    return
await self._adapter.send_text(self._owner, final)
await self._store.mark_acked(trig.trigger_id)
```

Devil H-9 scenario: scheduler-origin turn at 09:00 runs `general` agent
which auto-delegates to a `researcher` subagent via the synchronous
`Task` tool. Main turn output is just _"I'm researching X; results soon"_
(a stub), and the model's actual research write-up arrives via the
SubagentStop hook's `adapter.send_text`. Owner sees TWO Telegram
messages: stub from dispatcher + real result from hook.

Two fixes are possible.

**Option A (simpler):** Leave the double-delivery; engineer the picker
prompt + AgentDefinition for `general` to always emit a clear stub like
_"делегировал в researcher; ответ через ~Xм"_. Owner gets an explicit
status update + the real answer later — UX-acceptable, decoupled, no
cross-subsystem coupling.

**Option B (more invasive):** Dispatcher checks ledger for any rows
inserted during its own turn (matched by `parent_session_id`); if any,
suppresses the dispatcher's `send_text(final)` and lets SubagentStop
hook deliver alone.

### Recommendation

**Take Option A.** Reasoning:

1. **Single-user bot — favour simplicity.** Cross-subsystem coupling
   (dispatcher inspecting `subagent_jobs` to decide whether to emit) is
   a non-trivial code path that would need its own tests, race-window
   handling, and fall-through if SubagentStop never fires.
2. **Owner gets useful information** from the stub. _"делегировал в
   researcher; ответ через ~Xм"_ tells the owner the schedule fired and
   sets latency expectations. Without it, a 5-minute silence after a
   09:00 trigger could be confused with daemon failure.
3. **Native-Task is synchronous (Q1 FAIL).** In practice, the
   dispatcher's main turn won't end before the subagent finishes — the
   `final` accumulator already contains the model's real summary by the
   time `send_text(final)` runs. The "double-delivery" risk is mostly a
   `subagent_spawn` (async picker) edge case, not native `Task`.
4. **For `subagent_spawn`** (the async path), the dispatcher's
   `final` will literally be the model's "I queued job N" message — the
   owner sees both _"я заскедулил job N"_ from dispatcher and the real
   research result from SubagentStop. Two messages is the right UX.

**Action items for coder:**

- Update SKILL.md (`skills/task/SKILL.md`) to instruct the model: when
  spawning via `subagent_spawn` from a scheduler-origin turn, ALWAYS
  emit a one-line stub mentioning the job_id + ETA (`~Xм`).
- Update `assistant/bridge/system_prompt.md` to include a single-line
  "scheduler-origin turns that delegate via subagent: emit a one-line
  status stub before stopping" hint.
- No code change in `scheduler/dispatcher.py`.

### Risks / failure modes

- **R-RQ6-1**: Model omits the stub — owner gets only the SubagentStop notify N minutes later. Acceptable (notify itself contains the result + footer with `[job N completed in Xs, kind=researcher]`).
- **R-RQ6-2**: Dispatcher hits empty-final dead-letter logic (line 169-204) for a `subagent_spawn` turn where the model emitted only a tool-use block. Symptom: trigger reverts to `pending` + retries → owner gets DUPLICATE notify on retry. Mitigation: SKILL.md mandates stub text after `subagent_spawn` invocation. Phase-9 polish: if dispatcher's `final` is empty AND the turn spawned a job, mark_acked anyway (skip dead-letter). Defer.

### Test strategy

- `tests/test_subagent_scheduler_handoff.py::test_subagent_spawn_from_scheduler_dispatches_with_stub` — gated by `RUN_SDK_INT=1`. Asserts: 1 dispatcher `send_text` (stub) + 1 SubagentStop `send_text` (result), both to OWNER_CHAT_ID, in order.
- `tests/test_subagent_scheduler_handoff.py::test_native_task_from_scheduler_no_double_delivery` — native synchronous `Task` (not async spawn): asserts dispatcher's `final` already contains the subagent output (Q1 sync) → exactly 1 `send_text`. SubagentStop hook still fires but its notify call lands AFTER mark_acked — assert it does so without raising on closed DB.

---

## RQ7 — DB migration v4 schema review

### State-of-the-art (this codebase)

`state/db.py:7` — `SCHEMA_VERSION = 3`. Migrations live in-line in
`db.py` (`_apply_0001`, `_apply_0002`, `_apply_0003`); pre-wipe spec
shifted to a `state/migrations/0004_subagent.sql` external file pattern.
**Either is acceptable** but symmetry with phase 5 (in-line) is cleaner.

### Recommendation

**Inline `_apply_0004` in `state/db.py`** mirroring phase 5 style.
External SQL file pattern is fine but adds a fs-read + `import` chain
without benefit at this table count. Phase-5's `_apply_0003` is the
template.

```python
async def _apply_0004(conn: aiosqlite.Connection) -> None:
    """Phase 6: subagent_jobs ledger.

    Single table; partial UNIQUE on sdk_agent_id (NULL allowed for
    pre-picker rows). Status machine:
        requested → started → (completed|failed|stopped|interrupted|error|dropped)

    Idempotent under BEGIN EXCLUSIVE; bump user_version inside the same
    transaction so a crash rolls us back to v=3 cleanly.
    """
    if await _current_version(conn) >= 4:
        return
    try:
        await conn.execute("BEGIN EXCLUSIVE")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS subagent_jobs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "sdk_agent_id TEXT, "
            "sdk_session_id TEXT, "
            "parent_session_id TEXT, "
            "agent_type TEXT NOT NULL, "
            "task_text TEXT, "
            "transcript_path TEXT, "
            "status TEXT NOT NULL DEFAULT 'started', "
            "cancel_requested INTEGER NOT NULL DEFAULT 0, "
            "result_summary TEXT, "
            "cost_usd REAL, "
            "callback_chat_id INTEGER NOT NULL, "
            "spawned_by_kind TEXT NOT NULL, "
            "spawned_by_ref TEXT, "
            "depth INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')), "
            "started_at TEXT, "
            "finished_at TEXT)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_subagent_jobs_sdk_agent_id_uq "
            "ON subagent_jobs(sdk_agent_id) WHERE sdk_agent_id IS NOT NULL"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subagent_jobs_status_started "
            "ON subagent_jobs(status, started_at)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subagent_jobs_status_created "
            "ON subagent_jobs(status, created_at)"
        )
        await conn.execute("PRAGMA user_version=4")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
```

`apply_schema` extension (line 209+):

```python
if current < 4:
    await _apply_0004(conn)
```

Bump `SCHEMA_VERSION = 4`.

**Schema notes:**

- Partial UNIQUE on `sdk_agent_id WHERE sdk_agent_id IS NOT NULL` —
  allows multiple `requested` rows with NULL sdk_agent_id while still
  preventing duplicate Start hook fires (B-W2-3).
- `idx_subagent_jobs_status_started` supports `recover_orphans` Branch 2
  scan (`status='started' AND finished_at IS NULL`) and `claim_next_requested`.
- `idx_subagent_jobs_status_created` supports Branch 3 (`status='requested' AND created_at < ...`).
- `cost_usd REAL` reserved for phase-9 accounting; nullable; phase-6
  notify formatter omits the segment when NULL (description.md L62).
- `task_text` populated only for `subagent_spawn` rows (the model's
  prompt) — NULL for native Task tool spawns where the SDK owns the
  child's prompt opaquely.
- `parent_session_id` populated from SubagentStartHookInput.session_id
  (Q12: that's the parent session on Start). `sdk_session_id` populated
  on Stop hook (Q12: that's the child session on Stop). Asymmetric.

### Risks / failure modes

- **R-RQ7-1**: SQLite < 3.8 doesn't support partial UNIQUE indexes. **Check:** `python -c "import sqlite3; print(sqlite3.sqlite_version)"` in deployment image is 3.45+ (Ubuntu 24.04 ships 3.45+). Safe.
- **R-RQ7-2**: A `requested` row inserted concurrently with `subagent_spawn` that races against picker `claim_next_requested` → duplicate dispatch. Mitigated by `claim_next_requested` using a status-precondition UPDATE (atomic claim).

### Test strategy

- `tests/test_db_migrations_v4.py::test_v4_applies_from_v3` — start at v3, apply, assert `user_version=4` + table + indexes exist.
- `tests/test_db_migrations_v4.py::test_v4_idempotent` — run twice, no error.
- `tests/test_db_migrations_v4.py::test_partial_unique_allows_null_duplicates` — insert two rows with `sdk_agent_id=NULL`, both succeed.
- `tests/test_db_migrations_v4.py::test_partial_unique_rejects_non_null_duplicates` — insert two rows with `sdk_agent_id='abc'`, second raises IntegrityError.

---

## RQ8 — Picker dispatches as conversation turns

### State-of-the-art (this codebase)

`adapters/base.py:56`:

```python
Origin = Literal["telegram", "scheduler"]
```

Phase 5 scheduler-origin turns flow through `ClaudeHandler.handle()` with
`origin="scheduler"` → conversations + turns rows are persisted →
`load_recent` history is available on next user turn → owner can see
"I asked the model X at 09:00 cron; here's what it said".

Devil M-1: pre-wipe picker spec called `bridge.ask` directly without
going through the handler → no conversations row → `subagent_spawn`
results are invisible to owner's history scrollback.

### Recommendation

**Picker constructs `IncomingMessage` with `origin="picker"` and calls
`handler.handle()`.** Mirrors scheduler dispatcher. Owner sees the
prompt + model's reasoning + native Task usage in conversation history.

**1-line change to `adapters/base.py:56`:**

```python
Origin = Literal["telegram", "scheduler", "picker"]
```

**Picker `_dispatch_one`:**

```python
async def _dispatch_one(self, job: SubagentJob) -> None:
    log = self._log.bind(job_id=job.id, kind=job.agent_type)

    # B-W2-4 — ContextVar correlation for SubagentStart hook.
    PENDING_JOB_ID.set(job.id)

    # The prompt the model sees. Engineered to FORCE the synchronous
    # Task tool dispatch (RQ2 R-RQ2-2 mitigation).
    prompt = (
        f"Спавним subagent kind={job.agent_type}. "
        f"Делегируй задачу через инструмент Task на subagent {job.agent_type}. "
        f"Задача:\n\n{job.task_text}\n\n"
        f"После Task'а отвечай односложно: 'launched job {job.id}'."
    )

    msg = IncomingMessage(
        chat_id=self._owner_chat_id,
        message_id=0,
        text=prompt,
        origin="picker",
        meta={"job_id": job.id, "agent_type": job.agent_type},
    )

    accumulator: list[str] = []
    async def emit(chunk: str) -> None:
        accumulator.append(chunk)

    try:
        await self._handler.handle(msg, emit)
    except Exception as exc:
        await self._store.record_failed(
            job_id=job.id, error=repr(exc)[:500]
        )
        log.warning("subagent_picker_dispatch_error", error=repr(exc)[:200])
        return

    # No send_text — the SubagentStop hook delivers the actual result.
    # The handler's "final" output here is the model's stub
    # ("launched job N"); we don't ping the owner with that.
    log.info("subagent_picker_dispatched", emit_chars=len("".join(accumulator)))
```

**Picker constructor takes `handler` instead of `bridge` directly:**

```python
class SubagentRequestPicker:
    def __init__(
        self,
        *,
        handler: ClaudeHandler,    # <-- not bridge directly
        store: SubagentStore,
        settings: Settings,
        owner_chat_id: int,
        log: structlog.BoundLogger,
        stop_event: asyncio.Event | None = None,
    ) -> None: ...
```

But the **handler must use the picker bridge**, not the user-chat
bridge. This means **two ClaudeHandler instances** — one for user-chat
turns (uses user-chat bridge), one for picker dispatches (uses picker
bridge).

**Concrete `Daemon.start` wiring:**

```python
# Two bridges share the same extra_hooks (Q6 PASS) but have separate
# semaphores (B-W2-6) and separate timeouts.
bridge = ClaudeBridge(self._settings, extra_hooks=sub_hooks)
picker_bridge = ClaudeBridge(self._settings, extra_hooks=sub_hooks)

handler = ClaudeHandler(
    self._settings, store, bridge, transcription=transcription
)
picker_handler = ClaudeHandler(
    self._settings, store, picker_bridge, transcription=transcription
)

self._adapter.set_handler(handler)
# adapter only ever invokes user-chat handler; picker_handler is private.

self._spawn_bg(
    SubagentRequestPicker(
        handler=picker_handler,
        store=sub_store,
        ...
    ).run()
)
```

Both handlers share the same `ConversationStore` → conversations are
written under the SAME chat_id but distinct turns. The handler's
per-chat lock (line 294) means user turn + picker dispatch SERIALISE
under OWNER_CHAT_ID — that is correct: we don't want the model
processing a 09:00 picker dispatch interleaved with a 09:01 owner ping.

**Wait — that creates a cross-subsystem deadlock risk.** Phase-5
already serialises scheduler-origin and telegram-origin under the same
lock. Adding picker-origin extends the queue; phase-6 owner traffic +
scheduler 09:00 + picker dispatch can pile up. Mitigation: handler
already accepts these via `_chat_locks[chat_id]` so they queue cleanly,
processed in order. No new code. Document.

**Alternative — picker bypasses handler:**
Skip the conversations write, call `picker_bridge.ask` directly. Simpler;
trades off forensic visibility. Devil M-1 flagged this as a real
debt: when the owner asks "what did the bot say to the researcher
subagent at 09:00?", there's no conversation row to inspect.
**Recommend HANDLER path** for forensics.

### Risks / failure modes

- **R-RQ8-1**: Per-chat lock contention — owner sends 5 messages while a 5-minute picker dispatch is running → all 5 owner messages queue. UX: owner notices delay. Mitigation: SKILL.md instructs the model to use `subagent_spawn` for >30s tasks; under that threshold, owner-perceived latency stays acceptable.
- **R-RQ8-2**: ConversationStore writes from picker_handler may race with adapter's main handler if owner mass-sends. Already mitigated by per-chat lock.
- **R-RQ8-3**: `IncomingMessage.message_id=0` for picker (matches scheduler). `_chat_locks` doesn't care; turn_id is generated; confirm conversations.turn_id uniqueness works across origins.

### Test strategy

- `tests/test_subagent_picker.py::test_picker_persists_turn_to_conversations` — assert that after `_dispatch_one(job)`, a `conversations` row exists for `chat_id=OWNER`, `meta_json` contains `"origin": "picker"`.
- `tests/test_subagent_picker.py::test_picker_uses_separate_bridge` — inject two distinct bridge mocks; assert picker_handler invokes the picker_bridge mock, not the user bridge.
- `tests/test_subagent_lock_serialisation.py::test_picker_and_owner_serialise` — start a slow picker dispatch (asyncio.Event), have owner trigger a `handle()` call; assert owner's call awaits picker's lock release.

---

## Cross-cutting concerns

### Async-safety invariants (preserve from phase 5)

1. **Single bridge connection per ClaudeHandler.** Phase 5 doesn't
   share connections across handlers (each gets a `ConversationStore`
   reference, not `aiosqlite.Connection` directly). Phase 6 picker_handler
   shares the same store — no new connection.
2. **`async with self._lock` on every store mutation.** SubagentStore
   reuses ConversationStore.lock. Cross-table writes (e.g., `claim_next_requested` reads + UPDATE) MUST be inside one `with` block.
3. **Status-precondition SQL on UPDATEs.** Every state transition has `WHERE status='<expected>'`. Rowcount=0 → log `subagent_state_skew`, no raise.
4. **Hook callbacks return `{}` immediately.** Long work (DB writes,
   adapter.send_text) runs in a shielded task registered to
   `pending_updates`. Daemon.stop drains. (See implementation.md §3.8 — no change from spec.)

### Observability

- Hook fire log lines: `subagent_started`, `subagent_finished`,
  `subagent_orphans_interrupted`, `subagent_orphans_dropped`,
  `subagent_picker_dispatched`, `subagent_picker_dispatch_error`,
  `subagent_state_skew`.
- Each carries `agent_id` (or `job_id`) for grep correlation.
- The `phase4_memory` audit-log pattern (`memory_*` events) is the
  reference for log naming.

### Security perimeter

- S-2 wave-2: subagent's Bash/file/web tools traverse parent's
  PreToolUse hooks with `agent_id` populated. The full phase-3 sandbox
  (`make_bash_hook`, `make_file_hook`, `make_webfetch_hook`) is the
  security perimeter. **Do not** duplicate validators into a
  subagent-specific layer. Lock with `tests/test_subagent_sandbox_traversal.py`.
- Picker prompt is constructed from owner-supplied `task_text` — wrap it
  in nonce-sentinel pattern (existing `wrap_scheduler_prompt` in
  `tools_sdk/_scheduler_core.py` is the template). Don't trust model
  output to obey "don't shell out" — the sandbox enforces.

### Performance

- Picker tick: 1.0s default, configurable. Adds ~1 SQLite SELECT/sec
  when idle. Acceptable on a single-user bot.
- Bridge concurrency: each bridge has its own semaphore; total
  concurrent SDK queries = `2 * settings.claude.max_concurrent` (default 4).
  OAuth quota is shared across all queries — owner-visible rate limits
  unchanged from phase 5.
- Picker bridge's `claude_voice_timeout=900s` means a stuck dispatch can
  hold a slot for 15 minutes. With one picker bridge slot, at most one
  stuck job at a time. No fanout amplification.

---

## Anti-patterns to avoid

1. **Don't migrate the bridge to `ClaudeSDKClient` for `stop_task` cancel in phase 6.** `stop_task` exists on 0.1.63 but requires streaming-mode bridge. Switching would touch every codepath that calls `ClaudeBridge.ask`. Phase 7+.
2. **Don't add the depth-cap deny hook.** S-6-0 Q4 + 0.1.63 source → recursion is empirically gated by `tools` whitelist. Lock with regression test, don't add a runtime guard that can be misimplemented.
3. **Don't relax the JSONL `\n` tail check.** A SubagentStop hook firing milliseconds before the SDK CLI flushes the closing `\n` will deliver a TRUNCATED last block if we don't drop the partial line. Wave-2 saw this race.
4. **Don't introduce a CLI for `subagent_spawn`.** Phase 3 pivoted to @tool dogfood; phase 6 follows. Coder may be tempted to add `tools/task/main.py` because the original spec had it — that spec pre-dates the pivot.
5. **Don't share a single bridge between picker and user-chat handlers.** Concurrency-isolation matters. Two bridges, same extra_hooks dict.
6. **Don't store `last_assistant_message` field directly in the ledger** as a primary contract. It's a runtime convenience. The ledger row stores `result_summary` (truncated) for forensic listing; the actual delivered text goes through `adapter.send_text` only.
7. **Don't forget to add `"Task"` to `allowed_tools` ONLY when agents registered.** Empty `agents={}` + `Task` in allowed_tools → model gets confused (no targets); model errors. Conditional include (RQ1 sketch).

---

## Plan refinement suggestions

### From devil wave-1 already absorbed by this research

- C-1: dropped CLI; picker accepts handler-with-picker-bridge (RQ1, RQ8).
- C-2: `_effective_allowed_tools` removed from spec; conditional `Task` inclusion in `_build_options` (RQ1).
- C-3: re-spike against 0.1.63 deferred to coder running `respike_0163.py`; static-source verdicts above.
- C-4: picker bridge uses `claude_voice_timeout=900s` via `timeout_override` (RQ2).
- C-5: streaming JSONL fallback with tail-completeness guard (RQ5).
- H-2: picker registered on `_bg_tasks` via `_spawn_bg_supervised` (RQ3).
- H-6: picker awaits dispatch inline; no per-tick `create_task` (RQ3).
- H-7: branch ordering of `recover_orphans` locked (RQ4).
- H-9: scheduler-origin double-delivery resolved by SKILL.md (RQ6).

### Remaining spec contradictions (coder MUST resolve)

1. **`tools/task/main.py` listed in `detailed-plan.md` §15.1 / `implementation.md` §1 commit table.** Both files say create the CLI. RQ1 says drop. **Coder honours RQ1.** Update the commit-5 row in `implementation.md` §1 to "subagent @tool surface + skill" (no CLI).
2. **`bridge/hooks.py::_validate_python_invocation` referenced in `detailed-plan.md` §7.4 + §9.** Function does not exist on `main`. RQ1 confirms drop. **Coder skips entirely.**
3. **Skill `skills/task/SKILL.md` body in `detailed-plan.md` §8** lists `python tools/task/main.py` examples. **Rewrite for `subagent_*` @tool calls.** Decision tree:
   - Owner asks to delegate a long task → use `subagent_spawn`.
   - Owner already in chat asking to delegate inline → consider `Task` tool (synchronous).
   - Owner asks "what's job 42 status" → `subagent_status`.
   - Owner asks "cancel job 42" → `subagent_cancel`.
4. **`SubagentSettings.picker_tick_s` + `notify_throttle_ms`** specced in `implementation.md` §3.3. Keep them; defaults 1.0s + 500ms.
5. **Pitfall #1 SDK version pin.** Soften: `if claude_agent_sdk.__version__ not in {"0.1.59","0.1.60","0.1.61","0.1.62","0.1.63"}: log.warning(...)`. Don't crash.

---

## Key references

- `claude-agent-sdk==0.1.63` PyPI: https://pypi.org/project/claude-agent-sdk/0.1.63/
- SDK `types.py` (installed): `.venv/lib/python3.12/site-packages/claude_agent_sdk/types.py` — definitive schema for AgentDefinition, hook inputs, message types.
- SDK `client.py` (installed): `.venv/lib/python3.12/site-packages/claude_agent_sdk/client.py` — `ClaudeSDKClient.stop_task` signature.
- SDK `_internal/query.py` (installed): wire format for `stop_task` and `interrupt`.
- Phase 5 scheduler: `src/assistant/scheduler/{loop,store,dispatcher,cron}.py` + `src/assistant/tools_sdk/scheduler.py` — canonical templates for picker, ledger, @tool surface, tests.
- Phase 4 memory: `src/assistant/tools_sdk/memory.py` — canonical configure_X / `_CTX` pattern.
- Pre-wipe phase 6 specs: `plan/phase6/{description,detailed-plan,implementation,spike-findings}.md` — ~95% applicable; deltas above.
- Pre-wipe spike artifacts: `spikes/phase6_s0_native_subagent.py`, `spikes/phase6_s0_report.json`, `spikes/phase6_s1_contextvar_hook.py`, `spikes/phase6_s2_subagent_sandbox.py` — runnable on 0.1.59; coder updates for 0.1.63 in `spikes/respike_0163.py`.

---

## Coder readiness check

Before commit 1:

- [ ] Read this file end-to-end.
- [ ] Read `plan/phase6/{description,detailed-plan,implementation,spike-findings}.md`.
- [ ] Run `spikes/respike_0163.py` (when coder writes it from the spec
      in RQ-RESPIKE) and confirm Q5 / Q1 / Q4 / Q7 behaviour matches
      static-source verdicts.
- [ ] If any verdict differs, raise to orchestrator BEFORE writing code.
- [ ] Apply spec contradictions resolution (above) — drop CLI, no
      `_validate_python_invocation`, no `tools/task/main.py`.

Pipeline: GO once the spike is run + green.
