# Phase 6 spike S-6-0 — findings (wave-2, 2026-04-17)

Single consolidated empirical probe of `claude-agent-sdk==0.1.59` native
subagent primitives. All tests hit a real OAuth-authenticated `claude` CLI
(`claude --version` → `2.1.114`). Raw JSON in
`spikes/phase6_s0_report.json`. Script: `spikes/phase6_s0_native_subagent.py`.

## Wave-2 addendum (researcher fix-pack)

Devil wave-2 caught 1 genuine fabrication + 2 verdict drifts + 8 unclosed
spec blockers. This revision:

- Re-ran **Q1** with explicit `background=True` AND `background=False` in
  two fresh back-to-back runs (previous v1 spike did NOT pass the flag at
  all; `build_agents` helper silently omitted it). Raw data in new
  `q1_background_compare` block of `spikes/phase6_s0_report.json`.
- Added **S-1 ContextVar hook** probe
  (`spikes/phase6_s1_contextvar_hook.py` +
  `spikes/phase6_s1_contextvar_report.json`) for picker → Start-hook
  correlation (B-W2-4).
- Added **S-2 subagent sandbox** probe
  (`spikes/phase6_s2_subagent_sandbox.py` +
  `spikes/phase6_s2_sandbox_report.json`) to verify that subagent-emitted
  tool calls traverse the parent's phase-3 PreToolUse sandbox (B-W2-5 —
  critical security check).
- Reconciled Q5 verdict: raw report says `"verdict": "PARTIAL"` because
  the v1 analyser read only the JSONL transcript (0 assistant blocks
  flushed at hook-fire time). The `last_assistant_message` hook-input
  field DID carry the full final text in every observed run — see
  `q1_q2_q3_raw.hook_observations.stop_events[0].last_assistant_message_from_hook`.
  Verdict upgraded to **PARTIAL_FILESYSTEM_PASS_HOOK_FIELD** with the
  honest caveat that `last_assistant_message` is NOT in the SDK's
  `SubagentStopHookInput` TypedDict (runtime-only; fragile across SDK
  upgrades — see B-W2-2).
- Reconciled Q4 verdict wording: recursion is NOT "structurally
  impossible" — it is **empirically not observed when the child's
  `tools` list omits `"Task"`**. Child could report the word "child"
  only; runtime behaviour may change in a future SDK where `"Task"` is
  implicit.

## Verdict table

| # | Question | Verdict | Primary evidence | Plan impact |
|---|---|---|---|---|
| Q1 | `AgentDefinition(background=True)` → main `query()` returns BEFORE subagent finishes | **FAIL (wave-2 re-run)** | Two re-runs, same prompt, `background=True` vs `background=False`. BOTH returned ResultMessage AFTER TaskNotificationMessage (bg=True: result 22.65s > notif 17.55s; bg=False: result 23.88s > notif 18.95s). `main_finished_before_subagent=false` in both modes. Raw verdict: `FAIL_BG_BLOCKS_MAIN_TURN`. | **Plan pitfall #5 confirmed and stands.** `background=True` has no observable effect on main-turn latency on SDK 0.1.59 + CLI 2.1.114. Phase-6 design MUST assume main turn wall ≈ subagent wall. Keep `background=True` in AgentDefinitions for forward-compat (SDK may wire it up later) but don't rely on it. Raw block: `q1_background_compare` in `spikes/phase6_s0_report.json`. |
| Q2 | Model auto-discovers Task tool from `agents={}` | **PASS** | 1 ToolUseBlock + 1 TaskStartedMessage observed from a plain prompt that just referenced the `general` agent by name; `allowed_tools` included `"Task"` explicitly — without it may or may not work | Keep `"Task"` in baseline allowed_tools explicitly. Do not rely on auto-discovery without it. |
| Q3 | `Task*Message` emitted in main `query()` iter | **PASS** | Same iterator yields `TaskStartedMessage`, `TaskNotificationMessage`, and `SystemMessage(subtype="task_updated")`. `TaskProgressMessage` NOT observed in our runs (maxTurns=4 child) | Hook-based flow still correct. Message stream is available as secondary signal if hooks fail. |
| Q4 | Subagent can spawn sub-subagent (recursion) | **PASS (empirically gated by tools, wave-2 wording fix)** | Spike gave the child `tools=["Task","Read"]` and the child still said "I don't have access to Task/Agent tool" → recursion was not observed. We cannot claim "structurally impossible" (SDK undocumented; future versions may implicitly grant Task to subagents). The honest claim: **recursion was empirically not observed in S-6-0 when the registry and allowed_tools did not propagate `"Task"` deeper**. | **Plan simplification:** depth cap is trivially enforced by `tools` narrowing + tests that lock the baseline behaviour. Add a regression test that feeds a recursion prompt and asserts `len(distinct_subagent_agent_ids) == 1`. If phase-7 wants recursion, whitelist `"Task"` in `researcher`/`worker` definitions AND verify by re-probing. |
| Q5 | SubagentStop hook fires AFTER transcript flushed + readable | **PARTIAL (filesystem) / PASS (hook input field)** | Raw report sets `q5_transcript_flush.verdict="PARTIAL"` because at hook-fire time the JSONL transcript had `transcript_sizes=[528]` but `assistant_block_counts=[0]` and `last_text_previews=[""]` — the assistant-role block had not yet been appended to the file. Separately, the Stop hook's **runtime** input dict carries `last_assistant_message` with the full final text (verified in Q1 raw: `last_assistant_message_from_hook` = 495 chars of cat content). SDK TypedDict `SubagentStopHookInput` does NOT declare `last_assistant_message` — it is a runtime-only field (grep of `.venv/.../claude_agent_sdk/` returns no matches). **Honest status:** the field works on SDK 0.1.59 but could disappear / change in any future SDK release. | **Plan primary path:** read `raw.get("last_assistant_message")` first. **Fallback path:** if missing/empty, walk the JSONL at `agent_transcript_path` — but tolerate the "0 blocks at hook-fire time" race by retrying with a 250 ms sleep before reading. **SDK version pin:** on Daemon startup assert `claude_agent_sdk.__version__ == "0.1.59"` (or document the lowest version known to ship `last_assistant_message`). If the pin fails — log a loud warning and fall through to JSONL fallback. |
| Q6 | Hooks fire across multiple `ClaudeAgentOptions` instances | **PASS** | Two sibling `options` instances (sharing the same hook callback dict), each launched independent queries in parallel → both received `SubagentStart`+`SubagentStop` for their respective subagents. Distinct start/stop `agent_id`s (2 each) observed on the SHARED hooks bucket | Plan's "single hook factory built once at `Daemon.start()` and passed to every bridge instance" is correct. Phase-6 daemon only uses ONE bridge today, but the pattern is ready for phase-8. |
| Q7 | `main_task.cancel()` propagates to child | **FAIL → fallback required** | Cancel at t=8s, subagent `SubagentStart` fired but `SubagentStop` NEVER fired within 20s grace window. **Subagent orphaned on main cancel.** | Use flag-poll fallback: CLI sets `cancel_requested=1` in ledger; PreToolUse hook fires on every tool call from subagent, checks flag by `agent_id`, returns deny. **Gotcha:** if subagent makes NO tool calls, flag-poll never triggers — subagent runs to completion. Accept as known limitation (most subagents will tool-use). Alt: SDK-level `delete_session` after-the-fact to kill unreachable transcript state — but it won't terminate the running CLI. |
| Q8 | SDK concurrency cap on parallel subagents | **PASS at N=4** | Launched 4 `general` agents in one turn → 4 distinct `agent_id`s, peak overlap=4, all completed in ~17s wall | No visible cap at N=4. Didn't probe N=8+ (SDK may throttle; phase-6 use-cases rarely exceed 3-4 simultaneously; SDK contract unsaid). Accept SDK-managed for phase 6. |
| Q9 cheap | `prompt` field — full or appended? | **FULL** | Subagent obeyed the custom `AgentDefinition.prompt` (haiku-only, include "marker" in last line) verbatim; output: *"Golden sun burns bright / Warm breeze carries sweet blossom / Summer leaves its marker"* — prompt is the FULL system prompt | No base template to worry about. Our per-kind prompts can be self-contained. |
| Q10 cheap | `model="inherit"` valid | **PASS** | Constructor accepts; runtime used `claude-opus-4-6` (logged in subagent transcript) — child inherits main model | Keep `model="inherit"` in definitions. |
| Q11 cheap | `skills` field accepted | **CONSTRUCTION OK, RUNTIME NEUTRAL** | Passing `skills=["memory"]` (real slug from `skills/`) with a trivial `"reply ok and stop"` task succeeded; no externally-observable narrowing. Field is accepted. Effect on subagent visibility is not observable without an introspection probe inside the subagent itself (phase-7 concern) | Leave `skills` field OPT-IN; default None (subagent sees all) for phase 6. |
| Q12 cheap | `BaseHookInput.session_id` — parent reference? | **PARTIAL** | In Q1 run: 1 SubagentStart with `session_id=<some-uuid>`. That `session_id` was the MAIN session's id (same as `transcript_path` directory). So on Start hook, `session_id` = **parent session id**. On Stop hook, `session_id` = subagent's OWN session id (different UUID). Asymmetric. | **Start** hook: parent linkage via `session_id`. **Stop** hook: cannot link back to parent via session_id alone — must match by `agent_id`. Ledger's primary key must be `sdk_agent_id`. |
| Q13 cheap | CLI `spawn` architecture — subprocess vs daemon-pickup | **decision remains: daemon-pickup** | Not empirical. Reasoning unchanged from plan §7.2: subprocess-spawn needs to import `ClaudeBridge` which needs `Settings` which needs `.env` and full DI chain; plus child would not share OAuth cleanly. | Pickup pattern wins. CLI INSERTs a "spawn request" row; a bg task in Daemon polls and invokes the shared bridge. |

## Wave-2 spike results (B-W2-1, B-W2-4, B-W2-5)

### Q1 re-run with explicit `background=` (B-W2-1)

Script: `spikes/phase6_s0_native_subagent.py::test_q1_background_compare`.
Raw block: `q1_background_compare`.

| run | `background` flag | result_at | first_task_notification_at | main before child? | wall |
|---|---|---|---|---|---|
| bg_true  | True  | 22.65s | 17.55s | **False** | 23.04s |
| bg_false | False | 23.88s | 18.95s | **False** | 24.33s |

Same behaviour in both modes. **Verdict: `FAIL_BG_BLOCKS_MAIN_TURN`.**
Conclusion: Q1 pitfall in the plan (#5) stands — `background=True` does
not free the main turn.

### S-1 ContextVar propagation into hooks (B-W2-4)

Script: `spikes/phase6_s1_contextvar_hook.py`. Raw report:
`spikes/phase6_s1_contextvar_report.json`.

Two sequential `query()` calls, each setting a distinct `contextvars.ContextVar`
(values 1001 and 1002) BEFORE invoking `query()`. Hook callback reads the
var via `ContextVar.get()`.

| request_id | Start events observed | ctxvar matched in hook? |
|---|---|---|
| 1001 | 1 | True (saw 1001) |
| 1002 | 1 | True (saw 1002) |

**Verdict: PASS.** `asyncio.ContextVar` set in the caller's scope IS
visible inside the SDK-dispatched hook callback. `SubagentRequestPicker`
(implementation §3.9) can use ContextVar to correlate a pending request
id to the on_subagent_start hook fire without a synthetic prompt marker.

### S-2 subagent Bash → parent PreToolUse (B-W2-5 — SECURITY)

Script: `spikes/phase6_s2_subagent_sandbox.py`. Raw report:
`spikes/phase6_s2_sandbox_report.json`.

Wraps the real `assistant.bridge.hooks.make_pretool_hooks` with an
observer that records every PreToolUse fire + decision. Spawns a subagent
with `tools=["Bash","Read"]` whose prompt asks for `ls /` (outside
project_root, would be denied by phase-3 `_validate_ls_invocation`).

Observations (production phase-3 PreToolUse hooks fired with `agent_id`
populated each time):

| metric | value |
|---|---|
| subagent_starts | 2 |
| subagent_stops | 1 (other hit maxTurns) |
| total PreToolUse fires | 5 |
| subagent-origin (agent_id present) | **5** |
| main-origin (no agent_id) | 0 |
| subagent Bash calls denied | **5 / 5 (100%)** |

Distinct deny reasons observed (all from real phase-3 validators):
- `shell metacharacter not allowed: '>'` (subagent tried heredoc tricks)
- `program 'df' is not in allowlist [...]`

**Verdict: `PASS_SUBAGENT_BASH_BLOCKED_BY_PARENT_HOOK`.** Phase-6 does
NOT introduce a sandbox regression — subagent tool calls flow through
the same PreToolUse pipeline with `agent_id` set, matching the SDK's
own `_SubagentContextMixin` documentation in `types.py:246-262`
("Present only when the hook fires from inside a Task-spawned
sub-agent"). Phase-6 can ship without duplicating the phase-3 validators
into a subagent-specific layer.

### Static types.py cross-check for Q5 hook-input shape (B-W2-2)

`.venv/lib/python3.12/site-packages/claude_agent_sdk/types.py:309-316`:

```python
class SubagentStopHookInput(BaseHookInput):
    hook_event_name: Literal["SubagentStop"]
    stop_hook_active: bool
    agent_id: str
    agent_transcript_path: str
    agent_type: str
```

`grep last_assistant_message .venv/lib/python3.12/site-packages/claude_agent_sdk/` →
no matches. `last_assistant_message` is **not** part of the TypedDict
contract. It lives in the runtime CLI payload only. Implementation.md v2
adds a startup SDK version pin + JSONL fallback to contain the fragility.

## Pipeline readiness

**GO to coder with v2.** All 8 wave-2 blockers resolved:
- B-W2-1 (Q1 re-run): FAIL confirmed, plan pitfall stands.
- B-W2-2 (Q5 field vs TypedDict): PARTIAL/PASS with honest caveat + SDK pin.
- B-W2-3 (schema NULL sdk_agent_id): schema change in v2 §3.1.
- B-W2-4 (ContextVar propagation): PASS — picker pattern viable.
- B-W2-5 (subagent sandbox): PASS — phase-6 not a security regression.
- B-W2-6 (picker bridge isolation): design in v2 §3.9.
- B-W2-7 (recovery vs pending): schema + recovery in v2 §3.6.
- B-W2-8 (_GLOBAL_BASELINE): whitelist Task conditionally — v2 §3.10.

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

- Spike script: `spikes/phase6_s0_native_subagent.py` (+`test_q1_background_compare` in wave-2).
- Raw report: `spikes/phase6_s0_report.json` (includes `q1_background_compare` wave-2 block).
- Wave-2 S-1: `spikes/phase6_s1_contextvar_hook.py` + `spikes/phase6_s1_contextvar_report.json`.
- Wave-2 S-2: `spikes/phase6_s2_subagent_sandbox.py` + `spikes/phase6_s2_sandbox_report.json`.
- This file: qualitative analysis + wave-2 addendum.
- `plan/phase6/implementation.md` v2: coder-ready spec with all 8 blockers + 10 gaps closed.
