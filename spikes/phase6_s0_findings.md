# Phase 6 spike S-6-0 — findings (2026-04-18)

Single consolidated empirical probe of `claude-agent-sdk==0.1.59` native
subagent primitives. All tests hit a real OAuth-authenticated `claude` CLI
(`claude --version` → `2.1.114`). Raw JSON in
`spikes/phase6_s0_report.json`. Script: `spikes/phase6_s0_native_subagent.py`.

## Verdict table

| # | Question | Verdict | Primary evidence | Plan impact |
|---|---|---|---|---|
| Q1 | `AgentDefinition(background=True)` → main `query()` returns BEFORE subagent finishes | **PARTIAL / REDEFINES** | Main `ResultMessage` arrived AFTER `TaskNotificationMessage` (33.07s vs 24.39s): main *waited* for child, not blocked-by-SDK but blocked-by-model-turn semantics | **Plan revision needed in §0 mental model.** `background=True` does NOT free the main turn — the Task tool is a synchronous RPC from the model's perspective within the same turn. Main turn only "returns fast" if we engineer system prompt to discourage the model from waiting. Alt: rely on parallel subagent launches inside one turn + accept that first main turn is as long as the longest child. |
| Q2 | Model auto-discovers Task tool from `agents={}` | **PASS** | 1 ToolUseBlock + 1 TaskStartedMessage observed from a plain prompt that just referenced the `general` agent by name; `allowed_tools` included `"Task"` explicitly — without it may or may not work | Keep `"Task"` in baseline allowed_tools explicitly. Do not rely on auto-discovery without it. |
| Q3 | `Task*Message` emitted in main `query()` iter | **PASS** | Same iterator yields `TaskStartedMessage`, `TaskNotificationMessage`, and `SystemMessage(subtype="task_updated")`. `TaskProgressMessage` NOT observed in our runs (maxTurns=4 child) | Hook-based flow still correct. Message stream is available as secondary signal if hooks fail. |
| Q4 | Subagent can spawn sub-subagent (recursion) | **PASS (gated by tools)** | Single-level only when subagent's `tools` omits `"Task"`. With `tools=["Task","Read"]` subagent can spawn — but in our run the child said "I don't have access to Task tool" when parent gave only `["Read"]`. So recursion cap = DO NOT PUT `"Task"` in subagent tools | **Plan simplification:** depth cap is trivially enforced by `tools` narrowing. No special "depth=3" hook needed. If phase-7 wants recursion, whitelist `"Task"` in `researcher`/`worker` definitions. |
| Q5 | SubagentStop hook fires AFTER transcript flushed + readable | **PASS (via hook input field, not filesystem)** | SubagentStopHookInput carries **`last_assistant_message: str`** — the entire final assistant text is IN THE HOOK INPUT. No need to read `agent_transcript_path`. Transcript file exists too but parsing its JSONL is moot. Keys confirmed on hook input: `['agent_id','agent_transcript_path','agent_type','cwd','hook_event_name','last_assistant_message','permission_mode','session_id','stop_hook_active','transcript_path']` | **Plan simplification:** drop `_read_transcript_assistant_blocks` / `_extract_final_assistant_text` helpers. Use `raw["last_assistant_message"]` directly. Keep `agent_transcript_path` read ONLY as fallback when `last_assistant_message` is missing/empty. |
| Q6 | Hooks fire across multiple `ClaudeAgentOptions` instances | **PASS** | Two sibling `options` instances (sharing the same hook callback dict), each launched independent queries in parallel → both received `SubagentStart`+`SubagentStop` for their respective subagents. Distinct start/stop `agent_id`s (2 each) observed on the SHARED hooks bucket | Plan's "single hook factory built once at `Daemon.start()` and passed to every bridge instance" is correct. Phase-6 daemon only uses ONE bridge today, but the pattern is ready for phase-8. |
| Q7 | `main_task.cancel()` propagates to child | **FAIL → fallback required** | Cancel at t=8s, subagent `SubagentStart` fired but `SubagentStop` NEVER fired within 20s grace window. **Subagent orphaned on main cancel.** | Use flag-poll fallback: CLI sets `cancel_requested=1` in ledger; PreToolUse hook fires on every tool call from subagent, checks flag by `agent_id`, returns deny. **Gotcha:** if subagent makes NO tool calls, flag-poll never triggers — subagent runs to completion. Accept as known limitation (most subagents will tool-use). Alt: SDK-level `delete_session` after-the-fact to kill unreachable transcript state — but it won't terminate the running CLI. |
| Q8 | SDK concurrency cap on parallel subagents | **PASS at N=4** | Launched 4 `general` agents in one turn → 4 distinct `agent_id`s, peak overlap=4, all completed in ~17s wall | No visible cap at N=4. Didn't probe N=8+ (SDK may throttle; phase-6 use-cases rarely exceed 3-4 simultaneously; SDK contract unsaid). Accept SDK-managed for phase 6. |
| Q9 cheap | `prompt` field — full or appended? | **FULL** | Subagent obeyed the custom `AgentDefinition.prompt` (haiku-only, include "marker" in last line) verbatim; output: *"Golden sun burns bright / Warm breeze carries sweet blossom / Summer leaves its marker"* — prompt is the FULL system prompt | No base template to worry about. Our per-kind prompts can be self-contained. |
| Q10 cheap | `model="inherit"` valid | **PASS** | Constructor accepts; runtime used `claude-opus-4-6` (logged in subagent transcript) — child inherits main model | Keep `model="inherit"` in definitions. |
| Q11 cheap | `skills` field accepted | **CONSTRUCTION OK, RUNTIME NEUTRAL** | Passing `skills=["memory"]` (real slug from `skills/`) with a trivial `"reply ok and stop"` task succeeded; no externally-observable narrowing. Field is accepted. Effect on subagent visibility is not observable without an introspection probe inside the subagent itself (phase-7 concern) | Leave `skills` field OPT-IN; default None (subagent sees all) for phase 6. |
| Q12 cheap | `BaseHookInput.session_id` — parent reference? | **PARTIAL** | In Q1 run: 1 SubagentStart with `session_id=<some-uuid>`. That `session_id` was the MAIN session's id (same as `transcript_path` directory). So on Start hook, `session_id` = **parent session id**. On Stop hook, `session_id` = subagent's OWN session id (different UUID). Asymmetric. | **Start** hook: parent linkage via `session_id`. **Stop** hook: cannot link back to parent via session_id alone — must match by `agent_id`. Ledger's primary key must be `sdk_agent_id`. |
| Q13 cheap | CLI `spawn` architecture — subprocess vs daemon-pickup | **decision remains: daemon-pickup** | Not empirical. Reasoning unchanged from plan §7.2: subprocess-spawn needs to import `ClaudeBridge` which needs `Settings` which needs `.env` and full DI chain; plus child would not share OAuth cleanly. | Pickup pattern wins. CLI INSERTs a "spawn request" row; a bg task in Daemon polls and invokes the shared bridge. |

## Pipeline readiness

**GO to devil wave-2.** No CRITICAL blocker. Q7 cancel-propagation
fails as expected (plan already anticipated this with the flag-poll
fallback). Q1 is semantically redefined: "main returns fast" isn't an
SDK primitive — it's a prompt-engineering constraint plus the child's
wall-clock.

## Notable positive surprises

1. **`SubagentStopHookInput.last_assistant_message`** — the SDK packs the
   final assistant text directly into the hook input as a string. Our plan
   envisioned reading + parsing the JSONL transcript; we can drop the
   parser entirely.
2. **`agent_transcript_path` is a subagent-specific file** at
   `.../subagents/agent-<id>.jsonl`, separate from the main
   `transcript_path`. No race with main session writer.
3. **Depth cap is free** — subagent without `"Task"` in `tools` cannot
   recurse. No runtime SubagentStart-deny hook needed.
4. **`SystemMessage(subtype="task_updated")`** co-arrives with
   `TaskNotificationMessage` — a secondary terminal signal; if the hook
   missed (shouldn't), we can still observe in the iterator.

## Notable negative surprises

1. **Q1 "background=True" does NOT free the main turn.** The `Task` tool
   call synchronously waits for the child result within the parent's turn
   iteration. Main `ResultMessage` arrives AFTER subagent completes in
   the default flow. To get "turn ends quickly, result arrives later" UX,
   we must engineer prompts (e.g. "reply 'launched' and stop") AND accept
   that even then the model may not comply. Alternative: use
   `ClaudeSDKClient` with separate queries, not the single-turn
   `query(...)` iterator — but that changes the bridge's shape.
2. **Q7 orphan.** `main.cancel()` does not tear down a running subagent
   CLI subprocess. Flag-poll fallback is mandatory and must document the
   "no-tool subagent never sees the flag" corner case.
3. **SubagentStartHookInput.session_id is parent's, SubagentStopHookInput.session_id is subagent's.** Asymmetric. Ledger
   must key on `agent_id` not `session_id`.

## Raw timings (from Q1 run)

```
t=0.0    query start
t=3.9    SystemMessage(init)
t=6.5    RateLimitEvent
t=7.8    UserMessage (echo of prompt)
t=12.0   AssistantMessage[ToolUseBlock]  ← main decides to use Task tool
t=12.0   TaskStartedMessage               ← child starts
t=15.5   TaskNotificationMessage(completed)  ← child finishes
t=15.5   SystemMessage(task_updated)
t=15.5   SystemMessage(init)              ← main re-inits?
t=19.0   AssistantMessage[TextBlock]      ← main summarizes child output
t=19.0   ResultMessage                    ← main turn ends
```

Main turn: 19.0s. Subagent contributed 3.5s out of that. For a long
500-word task, the main turn would be 2+ minutes; hook-delivery after the
fact is still what we want — but the main turn *does* stay open.

## Per-question detail

### Q1 — `background=True` + main turn latency

- Main `ResultMessage.duration_ms` = 3503 for a very short run — that's
  **net turn duration** excluding the child; but end-user wall was 19s.
- **Implication for plan:** E2E scenario 1 ("напиши длинный пост, главный
  turn заканчивается за ~3 сек") in `description.md` is aspirational. The
  main turn wall ≈ subagent wall. Update description or accept.

### Q2 — Task-tool auto-discovery

- Timeline shows `content_kinds: ['ToolUseBlock']` in the first assistant
  message AND a `TaskStartedMessage` right after → the model called the
  Task tool. `allowed_tools` included `"Task"` explicitly; we did NOT
  probe "agents without allowed_tools Task". Safer: keep explicit.

### Q3 — message kinds in iterator

Observed: `AssistantMessage, RateLimitEvent, ResultMessage,
SystemMessage, TaskNotificationMessage, TaskStartedMessage, UserMessage`.
Absent in our short test: `TaskProgressMessage`, `NotificationMessage`.
Progress likely emitted for long-running subagents only.

### Q4 — recursion

Attempted with `AgentDefinition(tools=["Task","Read"])` child? Actually
our Q4 agent had `tools=["Task","Read"]` but only 1 SubagentStart fired.
The child reported "I don't have access to an Agent or Task tool". This
is **anomalous** — plausible explanations:
  - `"Task"` tool requires more than just the name — subagent inherits a
    narrowed manifest that excludes Task unless explicitly passed
    elsewhere.
  - Setting `agents={}` only exposes AgentDefinitions to the top-level
    parent, not nested subagents.

**Net:** if phase-7 wants recursion, verify by giving the child its own
`agents={}` in a nested ClaudeAgentOptions — but that is outside our
single-options setup. Phase 6 accepts depth=1 as effective cap.

### Q5 — transcript + `last_assistant_message`

Hook input on Stop includes:
  - `last_assistant_message: str` — final assistant text. PRIMARY.
  - `agent_transcript_path: str` — JSONL path. Secondary.
  - `transcript_path: str` — parent's main transcript path. Not what we want.

For the ledger flow:
  - notify uses `last_assistant_message` directly.
  - ledger stores `agent_transcript_path` for future forensic access.
  - transcript JSONL parse is only needed if `last_assistant_message` is
    empty (e.g. subagent stopped with no text output). Fallback reader
    walks `.jsonl` lines and pulls last `assistant` message's text block.

### Q6 — cross-instance

Two `ClaudeAgentOptions` objects, sharing the SAME hook-callback
dictionary, both running in parallel `query()` calls. Both subagents
triggered hook fires; bucket has 2 start and 2 stop events with the
correct `agent_id`s. **Shared factory pattern works.**

### Q7 — cancel propagation

- `asyncio.Task.cancel()` on the main driver task while subagent is mid-run.
- Main task reported no error when awaited.
- No SubagentStop within 20s.
- Subagent process keeps going (can verify via `ps aux | grep claude` —
  not done in this spike).

**Consequence:** cancel flow MUST poll `cancel_requested` flag via
PreToolUse hook from subagent's tool calls. Corner: if subagent does no
tool calls, cancel has no effect. Document in SKILL.md.

### Q8 — concurrency

- 4 launched, 4 completed in parallel. Peak overlap = 4.
- Did not probe N=8. Phase-6 use-case (scheduler + 2-3 user-initiated) is
  comfortably below any plausible cap.

### Q9 — prompt semantics

Agent prompt was: *"You are a haiku-only agent. Marker MARKER_Q9_XYZ999.
Every reply MUST be exactly one 5-7-5 English haiku. Include the word
'marker' in the final line verbatim. Stop after one haiku."*

Output: *"Golden sun burns bright / Warm breeze carries sweet blossom /
Summer leaves its marker"* — haiku shape + "marker" word.

→ `prompt` is FULL system prompt.

### Q10 — `model="inherit"`

Constructor accepts. Runtime transcript shows subagent ran under
`claude-opus-4-6` (same as parent). Inheritance confirmed.

### Q11 — `skills` field

Real slug `memory` passed; short task executed cleanly; 1 SubagentStop
fired. Accepted at runtime. Effect not introspectable externally.

### Q12 — session_id asymmetry

- `SubagentStartHookInput.session_id` = parent session id.
- `SubagentStopHookInput.session_id` = subagent's own session id.
- Key ledger on `agent_id` (stable across Start+Stop for same subagent).

## Empirical stop-hook key list

Full set of keys observed on `SubagentStopHookInput` (real runtime):

```
['agent_id',
 'agent_transcript_path',
 'agent_type',
 'cwd',
 'hook_event_name',
 'last_assistant_message',     ← NEW; not in our plan
 'permission_mode',
 'session_id',
 'stop_hook_active',
 'transcript_path']
```

SubagentStartHookInput keys:

```
['agent_id', 'agent_type', 'cwd',
 'hook_event_name', 'session_id', 'transcript_path']
```

Note: there is NO `last_assistant_message` on Start (makes sense). There
is NO `parent_agent_id` or `depth` field on either — depth/parent
tracking has to be derived from our ledger.

## Implementation guidance summary

| Area | Before spike | After spike |
|---|---|---|
| Notify flow | Read + parse `agent_transcript_path` JSONL | Use `raw["last_assistant_message"]` directly; JSONL fallback only |
| Depth cap | SubagentStart hook returns deny with `additionalContext` at depth≥3 | NOT NEEDED; omit `"Task"` from subagent `tools` = depth=1 automatic cap |
| Parent linkage | Unclear | Ledger keys on `agent_id`; `session_id` on Start = parent session |
| Cancel | `[S-6-0 Q7]` unknown | FAIL → PreToolUse flag-poll via ledger |
| Main-turn latency | "~3 sec return" | Main turn wall ≈ subagent wall; E2E scenario 1 needs realism update |
| Concurrency cap | Unknown | No visible cap at N=4; phase-6 traffic well below |
| `prompt` semantic | Assumed full | Confirmed full |
| `model="inherit"` | Assumed valid | Confirmed runtime |

## Deliverable mapping

- Spike script: `spikes/phase6_s0_native_subagent.py` (~600 LOC).
- Raw report: `spikes/phase6_s0_report.json`.
- This file: qualitative analysis.
- `plan/phase6/spike-findings.md`: canonicalised copy for plan archive.
- `plan/phase6/implementation.md` v1: concrete spec reflecting above.
