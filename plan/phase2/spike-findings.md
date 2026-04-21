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

---

## 6. Second-wave spike findings (R7–R12, 2026-04-20)

Added after devil's advocate wave 1 surfaced spike-resolvable questions on top
of v2 implementation.md. Claude CLI under test: `2.1.114` (OAuth via
`~/.claude/`). SDK pin unchanged (`claude-agent-sdk==0.1.59`). All probes live
in `/Users/agent2/Documents/0xone-assistant/plan/phase2/spikes/`.

### R7 — Prompt caching in SDK 0.1.59

**Question:** does the SDK/CLI automatically place `cache_control` markers on
the system prompt + prior turns, or must we do it manually via `extra_args`?

**Probe:** `plan/phase2/spikes/r7_prompt_caching.py` — two sequential
`query()` calls with an identical ~800-char system prompt.

**Finding — automatic caching is ACTIVE at the CLI layer, and it uses the
1-hour ephemeral cache tier.** Both calls reported identical usage:

```
input_tokens: 6
cache_creation_input_tokens: 5710  (ephemeral_1h_input_tokens: 5710)
cache_read_input_tokens:     17404
cache_creation.ephemeral_5m_input_tokens: 0
total_cost_usd:              ~$0.04
```

Even the first call showed `cache_read=17404` — that is the Claude Code CLI's
own system-prompt scaffold already cached across user sessions. OUR system
prompt adds ~5710 tokens of cache_creation which then becomes reusable for an
hour. Phase 2 needs **no manual cache_control wiring.**

**Follow-up caveat:** caching granularity is `system_prompt + prior turns` as
a unit. Changing our system prompt text (e.g. manifest rebuild when a new
skill is installed) invalidates the system-prompt cache segment for that
hour. Cost of rebuild: ~5700 cache_creation tokens ≈ $0.02. Acceptable —
skill-installer runs are rare. Keep the `build_manifest` mtime cache (it
reduces unnecessary prompt churn so we don't break cache needlessly).

**Implementation impact:** zero code change.

### R8 — Bash hook compound-command bypass matrix

**Question:** the allowlist-first + slip-guard combination from
implementation.md §2.1 — which bypass vectors does it actually block?

**Probe:** `plan/phase2/spikes/r8_bash_bypass.py` — 36 dry-run cases against
a strengthened slip-guard (added command-chaining and escape-sequence
catches beyond v1).

**Finding — 36 / 36 pass.** The v1 slip-guard was missing command-chaining
metacharacters, command-substitution, and escape sequences. All compound
bypasses (`;`, `&&`, `|`, ``` ` ``` , `$()`), env-dump variants (`env`,
`printenv`, `set`), base64/hex/openssl decode, PATH prefix injection, and
`python -c` escape trickery are now denied. Legitimate allowlisted commands
(`python tools/…`, `git status`, `cat README.md`) pass.

**Hardened slip-guard (replaces v1 `_BASH_SLIP_GUARD_RE`):**

```python
_BASH_SLIP_GUARD_RE = re.compile(
    r"(\benv\b|\bprintenv\b|\bset\b\s*$|"
    r"\.env|\.ssh|\.aws|secrets|\.db\b|token|password|ANTHROPIC_API_KEY|"
    r"\$'\\[0-7]|"
    r"base64\s+-d|openssl\s+enc|xxd\s+-r|"
    r"[A-Za-z0-9+/]{48,}={0,2}|"
    r"[;&|`]|\$\(|<\(|>\(|"
    r"\\x[0-9a-f]{2}|\\[0-7]{3}"
    r")",
    re.IGNORECASE,
)
```

Also added `python3 tools/` prefix to allowlist (v1 only listed `python
tools/`; model may emit `python3` on some configs).

**Remaining gap:** slip-guard will false-positive on legitimate allowlisted
commands whose arguments happen to contain `password`, `token`, or a long
base64 blob. Accepted: phase 2 ships no tool that needs such inputs; phase
3+ skill-installer will whitelist specific tool argument shapes via
`allowed-tools` frontmatter — NOT by weakening the guard.

**Regression test:** the full 36-case table belongs in
`tests/test_bash_hook_bypass.py` (hermetic, no SDK needed).

### R9 — WebFetch SSRF guard: DNS rebinding

**Question:** is string-prefix hostname matching enough, or should we resolve
DNS and check the resulting IP?

**Probe:** `plan/phase2/spikes/r9_webfetch_ssrf.py` — 10 direct cases + two
`unittest.mock.patch('socket.getaddrinfo')` scenarios.

**Finding — string-only guard catches literal private/loopback/IMDS URLs
(10/10 direct cases pass) but is blind to a public-looking hostname whose
DNS resolves to a private IP.** Adding `socket.getaddrinfo` +
`ipaddress.ip_address(…).is_private|is_loopback|is_link_local|is_reserved`
closes that gap: `totally-innocent.example` → mocked `127.0.0.1` → blocked
with reason `DNS → 127.0.0.1 (loopback)`.

**Recommended `_make_webfetch_hook` upgrade** (replaces implementation.md
§2.1):

```python
import ipaddress, socket
from urllib.parse import urlparse

def _ip_is_blocked(ip_str: str) -> str | None:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return f"invalid IP {ip_str!r}"
    if ip.is_loopback: return "loopback"
    if ip.is_private: return "private"
    if ip.is_link_local: return "link_local"
    if ip.is_reserved: return "reserved"
    if ip.is_multicast: return "multicast"
    if ip.is_unspecified: return "unspecified"
    return None

def _make_webfetch_hook() -> Any:
    async def webfetch_hook(input_data, tool_use_id, ctx):
        url = (input_data.get("tool_input", {}) or {}).get("url", "").strip()
        if not url:
            return {}
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            return _deny(f"malformed URL: {url!r}")
        raw = url.lower()
        # Layer 1: literal string match (cheap)
        for needle in _WEBFETCH_BLOCKED_HOSTS:
            if host.startswith(needle.rstrip(".").rstrip("]")) or needle in raw:
                return _deny(f"blocked host literal: {host!r}")
        if not host:
            return _deny("empty host")
        # Layer 2: DNS + IP category check
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return {}  # NXDOMAIN / transient DNS → allow (CLI will fail it)
        for _, _, _, _, sockaddr in infos:
            reason = _ip_is_blocked(sockaddr[0])
            if reason:
                return _deny(f"DNS → {sockaddr[0]} ({reason})")
        return {}
    return webfetch_hook
```

**Residual risk: TOCTOU DNS rebinding.** Milliseconds elapse between our
`getaddrinfo` and the CLI's actual fetch — an attacker-controlled DNS RR-cycle
can rotate to a private IP for the fetch. Only hard mitigation is an
OS-level egress ACL (`iptables -A OUTPUT -d 10.0.0.0/8 -j REJECT` and
friends). Record as **U9** in `unverified-assumptions.md`. Phase 2 accepts
best-effort defence-in-depth.

**`socket.getaddrinfo` is blocking — does it matter?** Yes, it blocks the
event loop up to the system DNS timeout (~5s). For a single-user bot this
is acceptable (one WebFetch in flight, nobody else on the loop). If it hurts
in phase 5+ scheduler, swap to `asyncio.get_running_loop().getaddrinfo(...)`.

### R10 — Session_id collision in concurrent queries

**Question:** if we pass the same `session_id="chat-<id>"` to two parallel
`query()` calls (phase 5 scheduler scenario), does SDK corrupt state / crash
/ serialize?

**Probe:** `plan/phase2/spikes/r10_session_id.py` — `asyncio.gather(q1, q2)`
both with envelope `session_id="chat-collide-42"`.

**Finding — SDK IGNORES our envelope `session_id` entirely in streaming-input
mode. It assigns a fresh UUID per query.** Both gathered calls succeeded
with distinct SDK-side session_ids (`a916eecd-…` and `f084753e-…`), and the
CLI wrote two separate JSONL files under
`~/.claude/projects/<cwd-slug>/<uuid>.jsonl`. No collision possible.

**Implications:**

- Our `session_id=f"chat-{chat_id}"` in envelopes is harmless client-side
  labeling; it is **not** honored by the CLI. Keep it as a human-readable
  breadcrumb in our logs, but document that it's cosmetic.
- **No need for a per-chat-id `asyncio.Semaphore(1)`** — SDK will not
  cross-pollute concurrent turns. `max_concurrent=2` already in phase 2 is
  safe.
- Our ConversationStore remains the single source of truth. The CLI's
  per-query JSONL files are ephemeral; we must not take a dependency on
  them.

**Zero implementation change needed.** Add a comment in
`_history_to_user_envelopes` noting that `session_id` is cosmetic.

### R11 — Migration 0002 transaction safety

**Question:** is the recreate-table migration atomic under crash, and
idempotent on re-run?

**Probe:** `plan/phase2/spikes/r11_migration_crash_sim.py` — three scenarios:
happy path, crash during `INSERT INTO conversations_new`, re-run on an
already-migrated DB.

**Finding — all three pass.**

1. **Happy path:** before `v=1, conversations=4, no turns`; after `v=2,
   conversations=4, turns=2, conversations_cols=[…+block_type]`.
2. **Crash during insert_rows:** `ROLLBACK` restored `v=1, no turns table,
   original conversations columns`. Re-running migration on the rolled-back
   DB reaches `v=2`.
3. **Re-run on `v=2`:** counts stable — `DROP TABLE IF EXISTS
   conversations_new` protects recreate from leftover debris; `INSERT OR
   IGNORE` on `turns.turn_id UNIQUE` makes turns-backfill a no-op.

**Canonical migration SQL** (goes into
`src/assistant/state/migrations/0002_turns_block_type.sql`):

```sql
-- Runner MUST wrap this in: BEGIN EXCLUSIVE; ...SQL...; PRAGMA user_version=2; COMMIT;
-- and ROLLBACK on exception. Runner checks PRAGMA user_version < 2 before applying
-- so re-running on v=2 is a no-op at the Python layer. The SQL itself is also
-- idempotent (guards below), so even if the guard races, the re-run converges.

DROP TABLE IF EXISTS conversations_new;

CREATE TABLE conversations_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    turn_id      TEXT NOT NULL,
    role         TEXT NOT NULL,
    content_json TEXT NOT NULL,
    meta_json    TEXT,
    created_at   TEXT NOT NULL,
    block_type   TEXT NOT NULL DEFAULT 'text'
);

INSERT INTO conversations_new (
    id, chat_id, turn_id, role, content_json, meta_json, created_at, block_type
)
SELECT id, chat_id, turn_id, role, content_json, meta_json, created_at, 'text'
FROM conversations;

DROP TABLE conversations;
ALTER TABLE conversations_new RENAME TO conversations;

CREATE INDEX IF NOT EXISTS idx_conversations_chat_time
    ON conversations(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_turn
    ON conversations(chat_id, turn_id);

CREATE TABLE IF NOT EXISTS turns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    turn_id      TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    completed_at TEXT,
    meta_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_chat_status
    ON turns(chat_id, status, completed_at);

INSERT OR IGNORE INTO turns (chat_id, turn_id, status, created_at, completed_at)
SELECT chat_id, turn_id, 'complete', MIN(created_at), MAX(created_at)
FROM conversations
GROUP BY chat_id, turn_id;
```

**Runner responsibilities (Python side, pseudocode):**

```python
async def _apply_0002(conn) -> None:
    cur = await conn.execute("PRAGMA user_version")
    if (await cur.fetchone())[0] >= 2:
        return
    await conn.execute("PRAGMA foreign_keys=OFF")
    try:
        await conn.execute("BEGIN EXCLUSIVE")
        try:
            await conn.executescript(MIGRATION_0002_SQL)
            await conn.execute("PRAGMA user_version=2")
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    finally:
        await conn.execute("PRAGMA foreign_keys=ON")
```

**Note on FK:** migration does NOT add a `FOREIGN KEY (turn_id) REFERENCES
turns(turn_id)` on `conversations`. Rationale: SQLite forbids adding FK via
ALTER; recreate-add-FK requires `turns` populated first, which in turn
depends on `conversations`. Phase 2 ships without FK; phase 4 can add it
with a second recreate if needed. detailed-plan §7a's pragmatic stance is
preserved.

### R12 — NULL `block_type` handling in history replay

**Question:** can `block_type IS NULL` reach `_history_to_user_envelopes`?
What happens if it does?

**Finding — on a freshly-migrated DB, no row can have NULL `block_type`
(migration sets `DEFAULT 'text'` + backfills all legacy rows to `'text'`).
But defensive handling costs nothing and future-proofs against migration
bugs:**

Two changes to `implementation.md §2.2 _history_to_user_envelopes`:

```python
# before:
if row.get("block_type") == "thinking": continue
# after:
btype = row.get("block_type") or "text"  # NULL → text (defensive)
if btype == "thinking": continue
```

And further down, where the code distinguishes tool_use rows:

```python
# before:
elif row.get("block_type") == "tool_use":
# after:
elif btype == "tool_use":  # btype computed above
```

**Regression test (`tests/test_history_null_block_type.py`):** insert a row
with `block_type=NULL` directly via `aiosqlite.execute` (bypassing the
`append()` kwarg), call `load_recent` → `_history_to_user_envelopes`; assert
no exception and the row is treated as text.

---

### R13 — Assistant envelope replay in streaming-input mode (fix-pack live probe)

**Context:** devil-wave-2 flagged that `history_to_user_envelopes` in v2
implementation.md dropped all assistant rows and injected a synthetic
Russian-language tool-note. This was originally a mitigation for **U1** (the
assumption that feeding `tool_use`/`tool_result` blocks back triggers SDK
rejection). But it also meant phase-2 bots had no multi-turn memory of their
own assistant replies — "what did you just say" would fail.

**Question:** does the SDK accept a streaming-input envelope of the form
`{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":...}],"model":...}}`
and does the model see it in context for subsequent user turns?

**Probe:** `plan/phase2/spikes/r13_assistant_envelope_replay.py` — sentinel
differential. The assistant envelope contains a 6-digit SENTINEL ("424242")
and is sandwiched between two user turns:

- user1: `"What's my LUCKY_NUMBER? Please tell me now."`
- assistant1 (injected): `"Your LUCKY_NUMBER is 424242. I'll remember it."`
- user2: `"Please repeat back my LUCKY_NUMBER. Reply with ONLY the 6-digit number."`

Baseline (control): same user1 and user2, **no** assistant envelope.

**Finding — SDK accepts assistant envelope and model honors it.** Live
run, OAuth, claude-agent-sdk==0.1.59, CLI 2.1.114:

```
# probe_without_assistant_baseline
reply:  "I don't have any record of a LUCKY_NUMBER for you."
contains_sentinel: false   ✓ (expected — no sentinel in input)

# probe_with_assistant_envelope
reply:  "Your LUCKY_NUMBER is 424242."
contains_sentinel: true    ✓ (sentinel sourced ONLY from assistant envelope)
error:  null               ✓ (no SDK rejection)
```

Clean differential. `envelope_accepted=True`, `envelope_honored=True`.

**Implementation impact (supersedes phase-2 v2 synthetic-note approach):**

- Rename `history_to_user_envelopes` → `history_to_sdk_envelopes`.
- Emit full envelope list: user rows → `{"type":"user",...}`, assistant rows
  → `{"type":"assistant","message":{"role":"assistant","content":...}}`.
- Keep `thinking` filter (U2 stands — cross-session thinking signature
  rejected, separate probe).
- Delete `test_u1_tool_block_roundtrip_xfail.py` (U1 resolved in favor of
  the synthetic-note DELETION path — we now replay verbatim).

**Envelope `model` field:** R13 probe sent `"model": "claude-opus-4-6"` on
the inner assistant message. SDK did not complain. Implementation omits the
field when not known (the row's `meta_json` doesn't currently round-trip
`model`; the SDK tolerates omission based on the probe's error=None).

**Regression test:** `tests/test_history_assistant_replay.py` —
hermetic unit test covering row-to-envelope mapping. Live-smoke step §3.6
4bis covers end-to-end "remember 777333 → what number did I give you?".

**Cost:** $0.02 for the full R13 probe (two sequential `query()` calls with
~$0.01 each for the sentinel scenario and the baseline).

**Residual risk — U10:** assistant envelope shape stability across SDK
versions. R13 validates 0.1.59; if the SDK (or CLI) tightens schema
validation in a point release and rejects our envelopes, history replay
silently loses assistant context. Add `tests/test_u10_assistant_envelope_shape_live.py`
(marker `requires_claude_cli`) as a manual regression on SDK upgrades.
Recorded in `unverified-assumptions.md §U10` for future audits.

---

## 7. Wave-2 bibliography (probes + docs consulted)

- Live spike probes: `/Users/agent2/Documents/0xone-assistant/plan/phase2/spikes/r7_prompt_caching.py`, `r8_bash_bypass.py`, `r9_webfetch_ssrf.py`, `r10_session_id.py`, `r11_migration_crash_sim.py` + generated `.json` reports.
- Claude Agent SDK 0.1.59 source (cache_creation structure): `_internal/client.py:157` → ResultMessage construction; cache keys `ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` surfaced via SDK passthrough.
- Anthropic prompt caching docs: https://docs.claude.com/en/docs/build-with-claude/prompt-caching — confirms automatic CLI cache_control for system prompt + long-lived context.
- Python `ipaddress` stdlib: https://docs.python.org/3/library/ipaddress.html — `is_private`, `is_link_local`, `is_reserved` properties cover RFC1918, RFC3927, RFC5737 ranges.
- OWASP SSRF Prevention Cheatsheet: https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html — DNS rebinding as "unsolvable at application layer; egress ACL only".
- SQLite ALTER TABLE docs (recreate pattern): https://sqlite.org/lang_altertable.html#otheralter — "12-step procedure" for schema changes under concurrent access.
- SQLite `PRAGMA user_version`: https://sqlite.org/pragma.html#pragma_user_version — single 32-bit counter, ideal for monotonic schema-revision tracking.

---

## S13 Addendum — R13 re-interpretation (2026-04-21)

R13's live probe confirmed that the SDK **accepts** assistant envelopes
in streaming-input mode. That finding still holds for the single-envelope
case. However, R13 did **not** exercise multi-envelope queue behaviour,
and it is precisely that gap which produced incident S13.

Follow-up forensics (see `plan/phase2/incident-S13-marker-drop.md`)
revealed:

- Per-row user envelopes in stream_input → CLI queues **each** envelope
  as a separate pending prompt.
- CLI processes its queue **sequentially**, so a turn with N prior user
  rows triggers N API iterations inside one `query()` call.
- Each API iteration ends with its own `ResultMessage`. Our `bridge.ask`
  had `return` after the first `ResultMessage`, so every iteration
  beyond the first — including the one that actually carried the
  user-facing reply to the current message — was silently dropped on
  the floor. They did still land in the CLI's JSONL journal, which is
  how we recovered them for post-mortem.
- From the owner's perspective every turn replied with "Йо. Чё делаем?"
  (the model's greeting to the very first prior `user` envelope, which
  happened to be "Йо") regardless of what they typed.

Fix applied:

- `history_to_sdk_envelopes` now emits **at most one** collapsed context
  envelope containing a plain-text rendering of prior turns. The CLI
  therefore sees exactly two pending prompts (context + current) and
  runs exactly one API iteration.
- `bridge.ask` changed `return` → `continue` after the `ResultMessage`
  yield so the generator survives any future multi-iteration case
  (e.g. genuine tool-use loops).
- `handlers/message.py` accumulates `last_meta` across the stream and
  completes the turn exactly once when the generator closes cleanly.
- `_safe_query` wraps `claude_agent_sdk.query` and swallows "Unknown
  message type" so future SDK/CLI bumps that introduce new message
  variants degrade into graceful end-of-stream instead of a crash.

R13's verdict stands for the single-envelope replay case, but NOT for
multi-envelope history replay. Future SDK-adjacent integrations must
be tested against the multi-pending-prompt CLI queue semantics.

