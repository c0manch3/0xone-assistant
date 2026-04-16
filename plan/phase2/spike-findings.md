# Phase 2 — SDK Spike Findings (R1–R5)

**Date:** 2026-04-15
**SDK under test:** `claude-agent-sdk==0.1.59` (Python)
**Claude Code CLI:** `2.1.109` (auth = OAuth via `~/.claude/`, no API key)
**Probes (reference sources, kept in repo):**

- `/Users/agent2/Documents/0xone-assistant/spikes/sdk_probe.py` — R1/R3/R4/R5-attempt/R2-attempt
- `/Users/agent2/Documents/0xone-assistant/spikes/sdk_probe2.py` — R2 retries, R5 with allowed_tools
- `/Users/agent2/Documents/0xone-assistant/spikes/sdk_probe3.py` — PreToolUse hook, thinking via `effort=high`, can_use_tool w/o allowed_tools
- `/Users/agent2/Documents/0xone-assistant/spikes/sdk_probe_report.json`, `sdk_probe2_report.json`, `sdk_probe3_report.json` — machine-readable run artefacts.

All probes ran against **real SDK + real OAuth** — no mocks.

---

## 1. Empirical answers

| ID | Question | Answer | Evidence |
|----|----------|--------|----------|
| **R1** | Multi-turn history mode for our `ClaudeBridge` | **Use `query(prompt=AsyncIterable[dict], options=...)` streaming-input mode.** Each history element is an SDKUserMessage envelope: `{"type":"user", "message":{"role":"user","content": <str or list of blocks>}, "parent_tool_use_id": None, "session_id": <stable id>}`. Assistant turns from history do NOT need to be re-streamed — they're already persisted when using `resume=session_id`. For our architecture we will **not** rely on `resume` (we keep history in our DB), so we manually emit all prior turns as `"type":"user"` envelopes with the full conversation reconstructed, plus the new user message last. `ClaudeSDKClient` streaming loop is a valid alternative (bidirectional), but adds long-lived state we don't need for stateless handler calls. | `sdk_probe.py::probe_r1_prompt_iterable` — envelope shape works end-to-end; SDK echoes "Vita" from prior turn in same stream. `_internal/client.py:152` branch `isinstance(prompt, AsyncIterable)`. |
| **R1 alt** | Does `resume=session_id` work? | **Yes** — passing `ClaudeAgentOptions(resume=session_id)` continues a server-side session. However, this requires us to trust server-side session storage, bypassing our ConversationStore. **Decision: do NOT use `resume`** in phase 2 — our DB is the source of truth; session continuity is reconstructed per-call. | `sdk_probe.py::probe_r1_multi_turn_via_resume` returned "42" after resume. |
| **R2** | ThinkingBlock mechanics | **Knob:** `ClaudeAgentOptions(max_thinking_tokens=N, effort="high")` → ThinkingBlocks do appear. The docs-suggested `thinking={"type":"enabled","budget_tokens":N}` TypedDict is accepted at Python level but the CLI rejects anything other than the literals `enabled/adaptive/disabled` (`extra_args` route fails too with the same reason). **Block shape:** `.thinking: str` + `.signature: str` (opaque, ~1–2 KB). **Model-dependent:** `claude-opus-4-6` returned thinking; older models may not. **Feeding back to SDK:** SDK refuses cross-session thinking replay — **do not re-emit ThinkingBlocks in `history_to_sdk_messages`** (confirmed by detailed-plan §Q10; spike did not stress this — flagged as *unverified* below). | `sdk_probe3.py::probe_thinking_via_env` → 1 ThinkingBlock, attrs `['signature','thinking']`, sig_len=1336, preview starts "The user wants me to determine if 1009 is prime...". |
| **R3** | `setting_sources=["project"]` + skills discovery | **Works end-to-end.** With `cwd=project_root` + `setting_sources=["project"]`, a skill dropped into `<project_root>/.claude/skills/<name>/SKILL.md` (YAML frontmatter with `name`, `description`) is discovered and invoked by the model. The `SystemMessage(subtype="init")` payload now includes top-level keys `skills` and `plugins`, confirming registration. **No `plugins=` or explicit `skills=` option is needed.** SKILL.md is the canonical contract. `allowed-tools` frontmatter is a list; we kept `[Bash]` — honored. | `sdk_probe.py::probe_r3_setting_sources_and_skills` — assistant replied exactly "PONG_PROBE"; init message keys include `skills`, `plugins`. |
| **R4** | `async for message in query(...)` stream semantics | **Messages come already fully assembled, not incrementally.** Types observed in order: `SystemMessage(subtype="init")` → optional `RateLimitEvent` → one or more `AssistantMessage(content=list[Block], model=...)` → `ResultMessage`. `AssistantMessage.content` is a complete list of blocks (TextBlock/ThinkingBlock/ToolUseBlock/ToolResultBlock) for that assistant turn — **not a per-token stream**. If you want token-level streaming, set `include_partial_messages=True` (not used in phase 2). `ResultMessage` fields: `subtype` ("success"/etc), `duration_ms`, `num_turns`, `total_cost_usd`, `usage` (dict with input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, service_tier, server_tool_use), `session_id`, `stop_reason`, `model`. | `sdk_probe.py::probe_r4_basic_query` — captured raw types and ResultMessage fields; `_internal/client.py:157` (`parse_message`). |
| **R5** | Permission callback — `can_use_tool=` vs `hooks={"PreToolUse": [...]}` | **Both exist; semantics differ in a critical way.** (1) `can_use_tool: Callable[[tool_name: str, tool_input: dict, ctx: ToolPermissionContext], Awaitable[PermissionResultAllow \| PermissionResultDeny]]` — fires **only** when CLI would otherwise prompt the user (i.e., the tool is NOT in `allowed_tools` AND permission_mode needs confirmation). In our empirical run with `allowed_tools=["Bash"]`, callback was **never invoked** (Bash auto-approved). Without `allowed_tools`, permission_mode=`default` blocked silently — still no callback. (2) `hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[fn])]}` — **fires on every tool call** regardless of `allowed_tools`. Hook signature: `async def hook(input_data: dict, tool_use_id: str \| None, ctx: dict) -> dict`. `input_data` keys: `session_id, transcript_path, cwd, permission_mode, hook_event_name, tool_name, tool_input, tool_use_id`. To deny: return `{"hookSpecificOutput": {"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason": "<msg>"}}`. To modify input: return `{"hookSpecificOutput": {"hookEventName":"PreToolUse","permissionDecision":"allow","updatedInput": {...}}}` (TS docs; not verified on Python in spike). **Decision for phase 2:** use `hooks` API, not `can_use_tool`. Two matchers: (a) Bash pre-hook regex deny on `.env/.ssh/secrets/.db/token/password`; (b) file-tool path-guard on `Read/Write/Edit/Glob/Grep` via a second HookMatcher with `matcher="Read\|Write\|Edit\|Glob\|Grep"`. | `sdk_probe3.py::probe_pretooluse_hook` — hook fired twice (echo allowed, `cat .env` denied with our message); `sdk_probe3.py::probe_can_use_tool_no_allowed` — zero callback invocations. |

---

## 2. Corrections to detailed-plan's assumptions

| Detailed-plan §  | Assumption | Reality (from spike) |
|---|---|---|
| §8 `bridge/claude.py` — `can_use_tool=_make_path_guard(...)` | Use `can_use_tool` callback for path-guard + Bash pre-hook | **Use `hooks={"PreToolUse": [...]}` instead.** `can_use_tool` only fires when CLI would prompt — if we put `Bash/Read/Write/...` in `allowed_tools` (needed so CLI doesn't ask), the callback is silently skipped. Hooks fire unconditionally. |
| §8 "Точная форма зависит от R5 spike" | Uncertainty between `can_use_tool` and `hooks` | Resolved → `hooks`. |
| §2 `ClaudeSettings.thinking` via `extra_args={"thinking":...}` (implied) | pass budget via `extra_args` | Use `ClaudeAgentOptions(max_thinking_tokens=N, effort="high")`. `extra_args={"thinking": json.dumps({...})}` fails at CLI level ("Allowed choices are enabled, adaptive, disabled"). Phase 2 will likely **not** enable thinking by default (cost + plan Q10 says "write but don't re-feed"); but if enabled, use the dedicated fields. |
| §8 "SDK принимает `AsyncIterable[dict]` когда `can_use_tool` задан" | Streaming-input mode required only for `can_use_tool` | Actually `can_use_tool` **requires** streaming input (validated in `client.py:57-61`); but streaming input works fine **without** `can_use_tool` too. Our `history_to_sdk_messages` will use streaming input regardless, to support both (a) emitting full prior turns as `"type":"user"` envelopes and (b) the permission flow if we ever flip back to `can_use_tool`. |
| §7 `history_to_sdk_messages` returns "list[dict]" | Output is list passed to SDK | SDK consumes an `AsyncIterable[dict]`. Adapter must be an `async def gen(): yield ...` that yields envelopes one by one (SDK reads from the iterator). |
| §R3 "plugins=" alternative considered | `plugins=` param might carry skills | `plugins=` is a separate concept (`SdkPluginConfig`); skills are discovered through `setting_sources=["project"]` reading `.claude/skills/*/SKILL.md`. No need for `plugins=`. |

---

## 3. Gotchas discovered during spike

1. **`ThinkingConfigEnabled/Adaptive/Disabled` are TypedDicts**, not dataclasses — must be passed as plain `dict` literals: `{"type": "enabled", "budget_tokens": N}`. Calling them as functions (`ThinkingConfigEnabled(budget_tokens=2000)`) crashes in `subprocess_cli.py:307` with `KeyError: 'type'` because the `type` literal is missing.
2. **CLI rejects custom `thinking` JSON via `extra_args`**: `--thinking <mode>` accepts only `enabled|adaptive|disabled` (no budget). Budget goes in `max_thinking_tokens` (top-level option). This is a divergence between TypeScript docs and the Python CLI shim.
3. **`ToolPermissionContext` (for `can_use_tool`) and hooks `ctx` are different types.** For hooks, the third arg is a plain dict (`ctx_attrs` = `['clear','copy',...]` → it's just a dict). For `can_use_tool`, it's a dataclass with `signal, suggestions, tool_use_id, agent_id`.
4. **`SystemMessage(subtype="init")` is extremely useful** — contains `cwd, tools, mcp_servers, model, permissionMode, skills, plugins, memory_paths`. Log it on every bridge call for diagnostics.
5. **`RateLimitEvent` is emitted inline** in the message stream between init and assistant — handler must not crash on unknown message types. Current SDK's `parse_message` already `None`-skips unknowns in the top-level loop (`client.py:157-160`).
6. **`UserMessage.content`** can be `str` OR `list[ContentBlock]`. When feeding history, passing `str` for plain user text is simplest; only wrap as `[{"type":"text","text":...}]` when mixing multi-block content.
7. **`resume=session_id` uses CLI-side session store** (`~/.claude/projects/.../*.jsonl`). If we ever adopt it, those files become production data — treat them accordingly. We're sticking with our ConversationStore for phase 2.
8. **`setting_sources=["project"]` also triggers loading `.claude/settings.json` if present** — if our project ever gains a `.claude/settings.json` with custom permissions, it will be merged into the CLI session. Today we have none (the directory only holds the `skills/` symlink).
9. **Hook return shape** is nested: `{"hookSpecificOutput": {"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason": "..."}}`. Missing `hookEventName` inside `hookSpecificOutput` silently ignores the decision.
10. **Symlink `.claude/skills -> ../skills`**: with `cwd=project_root`, the CLI picks it up just like a real directory. No special handling needed. But the probe SKILL was written to `.claude/skills/probe_ping/` directly (not via symlink) — the symlink path is still **assumed** to work identically. Low risk given CLI treats paths transparently, but **flag as unverified** — coder should confirm with the real `skills/ping/` via symlink.

---

## 4. What remains UNVERIFIED by this spike

Items that are load-bearing in the plan but I could not fully empirically prove in-session (flagged so coder/review doesn't treat them as gospel):

| # | Claim | Why unverified |
|---|-------|---|
| U1 | Feeding persisted `ToolUseBlock` / `ToolResultBlock` back to SDK via streaming input works (i.e., the `history_to_sdk_messages` collapse into `message.content: list[dict]` with mixed block types) | Probe only tested plain text history. Need a round-trip test where first turn produces a tool_use/tool_result pair, and second turn's SDK input contains those blocks, without SDK complaining. |
| U2 | Re-emitting `ThinkingBlock` from history actually fails (plan §Q10 asserts "SDK refuses cross-session thinking") | Not empirically checked. Coder should add a regression test: inject a thinking block in history → SDK either accepts or errors; the skip rule is defensive. |
| U3 | `.claude/skills` as a **symlink** (vs real directory) is treated identically by `setting_sources=["project"]` | Probe wrote directly into the real directory. Coder should smoke-test the symlink path before marking phase 2 green. |
| U4 | `PreToolUse` hook `updatedInput` modification works in Python SDK | TS docs support it; Python internal code path around `_handle_control_request` does handle `hookJSONOutput`, but no spike test verified input mutation. We do not need mutation in phase 2 (deny-only) — informational. |
| U5 | The `HookMatcher(matcher="Read\|Write\|Edit\|Glob\|Grep")` regex form for multiple tool names | Probe used `matcher="Bash"` (exact). The matcher field is documented to support regex but we didn't validate. **Safer plan:** register 5 separate `HookMatcher` entries, one per tool name. |
| U6 | `effort="high"` is stable across models and doesn't bump cost in unexpected ways | Works on `claude-opus-4-6`; unclear on Sonnet. If we ever switch models, re-run the thinking probe. |

---

## 4a. Post-devil's-review addendum (2026-04-15, v2 of implementation.md)

Devil's review над `implementation.md` v1 подсветил несколько производных выводов, которые полезно хранить рядом с первичным spike-отчётом:

- **R5 refinement:** `HookMatcher` count = 1 Bash + 5 file-tools + 1 WebFetch = **7 матчеров**. v1 implementation.md ошибочно указывал "5". WebFetch-hook добавлен как SSRF-guard (blocks private/link-local/metadata hosts) — security baseline.
- **R2 practice:** phase 2 стартует с `thinking_budget=0` (thinking off). Если когда-нибудь включим — `ClaudeAgentOptions(max_thinking_tokens=N, effort="high"|...)`. Новый regression-тест `test_u2_cross_session_thinking_rejected_xfail` защищает инвариант "не переподаём thinking".
- **U1 mitigation (архитектурное решение, не spike):** вместо замалчивания пропущенных tool_use/tool_result блоков в истории — модели сообщается synthetic system-note `[system-note: в прошлом ходе были вызваны инструменты: X, Y. Результаты получены.]`. Препятствует повторному вызову тех же инструментов при реплее истории.
- **Bash prefilter reassessment:** v1 regex признан недостаточным (4 bypass'а доказаны: `env`/`printenv`, octal escape, glob, base64-decode). В v2 переведён в defence-in-depth; primary control = allowlist-first (explicit prefix whitelist + `cat` только внутри `project_root`).
- **`dataclasses.replace(ClaudeAgentOptions, ...)`:** технически работает (ClaudeAgentOptions — dataclass, подтверждено `probe_options_fields`), но в v2 избегаем — `system_prompt` передаётся прямо в `__init__`. Ближе к sample-коду из `sdk_probe*.py`, меньше surface для surprises.

Раздел §4 (UNVERIFIED) расширен: U1/U2/U5 прикрыты xfail-тестами; U3 — regular unit test + manual smoke step.

---

## 5. Bibliography

- SDK source tree inspected: `/Users/agent2/.cache/uv/archive-v0/L2hRBggEhtq5pQwTPhx4o/lib/python3.12/site-packages/claude_agent_sdk/` — specifically `_internal/client.py:45–164` (process_query), `_internal/query.py:264–316` (control request handler), `types.py:1209,1257` (can_use_tool + control permission request shape).
- `https://docs.claude.com/en/api/agent-sdk/python` — Python SDK reference (cross-checked for `ClaudeAgentOptions` and streaming-input mode docs).
- GitHub `anthropics/claude-agent-sdk-python` — used offline via installed package source (same as above cache path).
- `claude-agent-sdk==0.1.59` release notes (via `uv pip show`).
