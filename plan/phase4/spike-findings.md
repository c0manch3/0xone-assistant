# Phase 4 — Spike Findings (2026-04-17)

Empirical results for Task 0 (S-A: SDK per-skill hook semantics) and
Task 0b (S-B: `tool_result.content` shape in DB). All claims here come
from running two executable probes against the live `claude` CLI +
`claude-agent-sdk==0.1.59` on this host (macOS Darwin 24.6.0, OAuth auth
only, no `ANTHROPIC_API_KEY`).

Artifacts:

- `/Users/agent2/Documents/0xone-assistant/spikes/sdk_per_skill_hook.py`
  (+ `sdk_per_skill_hook_report.json`, 5 cases, all completed).
- `/Users/agent2/Documents/0xone-assistant/spikes/history_toolresult_shape.py`
  (+ `history_toolresult_shape_report.json`, 3 Bash variants, all completed).

---

## S-A: SDK per-skill hook semantics (detailed-plan §0)

### S-A.1 `init.data["skills"]` — source of truth for active skills

Probe (`C0_init_observation`) created a tmp project tree containing two
SKILL.md files under `skills/sa/` (allowed-tools `[Read]`) and
`skills/sb/` (allowed-tools `[Bash]`), mirrored the phase-2 bootstrap
symlink `.claude/skills -> ../skills`, then ran a turn with
`setting_sources=["project"]`. Observed:

```json
"init_data": {
  "skills": ["update-config", "debug", "simplify", "batch", "loop",
             "schedule", "claude-api", "sb", "sa"]
}
```

**Conclusion A.1.a.** `init.data["skills"]` is a **merged name-only list**
of (a) user-scope skills under `~/.claude/skills/` + `~/.claude/plugins/...`
and (b) project-scope skills from `<cwd>/.claude/skills/` (which is the
symlink phase-2 `ensure_skills_symlink` creates).

**Conclusion A.1.b.** The list is **names only** — no `allowed_tools`,
no `description`, nothing per-skill beyond the name. Whatever phase-4
code wants to reason about the `allowed_tools` union must read it from
`bridge/skills.py::build_manifest` (file-system scan), NOT from
`init.data["skills"]`.

**Conclusion A.1.c.** The `setting_sources=["project"]` option does not
by itself cause the SDK to walk `<cwd>/skills/` — a `.claude/skills`
symlink (or directory) inside `cwd` is load-bearing. Phase 2's
`bridge/bootstrap.py::ensure_skills_symlink` already satisfies this;
phase 4 does not need to revisit it.

**Conclusion A.1.d.** `init.data["skills"]` appears in the SDK stream as
part of the first `SystemMessage(subtype="init")`. Phase-2
`bridge/claude.py:200-214` already extracts it into `InitMeta.skills`.
That field is authoritative for _which_ skills the model could dispatch
during the turn, but not for what tools they're allowed.

### S-A.2 `allowed_tools` in `ClaudeAgentOptions` — observed behaviour

Three cases probed on this host (OAuth'd, user-level
`~/.claude/settings.json` with a broad `permissions.allow` list):

| Case | `options.allowed_tools` | Installed skill `allowed-tools` | Prompt | Pre-hook calls (observed) |
|---|---|---|---|---|
| CA | `["Read"]` | sa=`[Read]`, sb=`[Bash]` | "Use sa to Read sb/SKILL.md" | `Read` fired once |
| CA2 | `["Read"]` | sa=`[Read]`, sb=`[Bash]` | "Run `echo hello` via Bash" | **`Bash` fired** — options did NOT block |
| CB | `[]` (lockdown) | sb=`[Bash]` | "Run `echo hi` via Bash" | **`Bash` fired** — empty list did NOT block |
| CC | `["Bash","Read"]` | perm=no `allowed-tools` key | "Run `echo baseline` via Bash" | `Bash` fired as expected |

**Conclusion A.2.a.** On a host with a permissive user-level
`settings.json` (the typical developer / OAuth setup this project runs
on), `ClaudeAgentOptions.allowed_tools` is **not a hard gate**. Bash
fired even when we passed `allowed_tools=[]` and `allowed_tools=["Read"]`.
The `permissions` section in user settings **takes precedence** over the
programmatic option. This matches the CLI behaviour where
`~/.claude/settings.json` `permissions.allow` is the final authority.

**Conclusion A.2.b.** `allowed_tools` is therefore best understood in
phase 4 as a **hint / advisory set**, not a sandbox. The real defence
layer is phase-2 `PreToolUse` hooks (`make_pretool_hooks`) — which this
project already uses for Bash argv allowlist, file path-guard, and
WebFetch SSRF. Those hooks always fire and always get to deny.

**Conclusion A.2.c.** For phase 4 Q8 "intersection with baseline" — the
implementation MUST still compute the intersection and pass it as
`allowed_tools`, but the operator-visible effect depends on whether the
user has locked down `~/.claude/settings.json`. Document this loudly.
In a stricter env (CI, server, no user-level `permissions.allow`), the
option is expected to gate — we just can't verify that from this host.

### S-A.3 Per-skill hook attribution — `HookContext` + `input_data`

Every `pre_hook` invocation across all 4 cases yielded an empty
`ctx_attrs` dict and the following `input_data` keys:

```text
['cwd', 'hook_event_name', 'permission_mode', 'session_id',
 'tool_input', 'tool_name', 'tool_use_id', 'transcript_path']
```

Fields explicitly confirmed **absent**: `active_skill`, `source_skill`,
`skill_name`, `agent_id` (present but always `None`), `agent_type`
(present but always `None`), any `skill`-containing key anywhere.

**Conclusion A.3.** The SDK does **not** partition hook execution by
skill. A `PreToolUse` hook receives a tool call with no marker of
"which skill invoked it". Phase-4 Q8 must therefore be implemented as:

> The allowed-tools union is computed **once per turn** across all
> installed skills (sorted/deduped), intersected with the global
> baseline, and passed via `options.allowed_tools`. Per-skill
> narrowing of hooks on a per-turn basis is **impossible with SDK
> 0.1.59**.

This makes B1 / B4 / D3 in the detailed-plan concrete:

- B1 closure: `_effective_allowed_tools` cannot know "this is a memory
  turn vs skill-installer turn". It must pass the union of every
  installed skill's `allowed_tools`.
- B4 closure: if the user has `skill-installer` (Bash) + `memory`
  (Bash, Read) + `ping` (Bash) installed, SDK sees `allowed_tools =
  {Bash, Read}`. Memory cannot be narrowed to exclude `skill-installer`'s
  Bash ability for the same turn.
- D3 closure (devil's follow-up): the ticket "future memcache skill
  requires Redis" is moot — any new tool must go into the global
  baseline first, then any skill can declare it.

### S-A.4 `hook_event_name` literal

`hook_event_name` on all captured `input_data` was always the
`PreToolUse` literal (we only registered `PreToolUse` in the probe).
Phase-3 probe already confirmed `PostToolUse` literal.

### S-A.5 Empirical vs claim — what is unverified

- **UNVERIFIED in strict-permissions env.** Behaviour in an env where
  `~/.claude/settings.json` has NO `permissions.allow` (fresh clone, CI,
  service account) was not observed here. Phase 4 implementation should
  still ship `options.allowed_tools` because its behaviour in that env
  is likely the advertised gate. Flag in the implementation plan.
- **UNVERIFIED: per-skill dispatch trace.** When the model actually
  auto-selects a skill (not when we hand-code "use sb"), is there a
  subagent boundary? `agent_id`/`agent_type` are plumbed in
  `HookContext` as `None`. Suggests phase 3/4 will never see a non-None
  here unless we run sub-agents, which we don't.

---

## S-B: `tool_result.content` shape in ConversationStore

Three Bash-invoking skills probed:

1. `pingjson` — `print(json.dumps({"pong": True}))` → single-line JSON.
2. `multiline` — 3 lines including Cyrillic and a JSON-ish literal.
3. `errorexit` — `sys.exit(7)` with stderr `boom`.

### S-B.1 Raw SDK shape

`ToolResultBlock` (from SDK 0.1.59 `dataclasses`) has fields
`tool_use_id: str`, `content: str | list[dict[str, Any]] | None`,
`is_error: bool | None`.

For every variant, `content` arrived as a **plain `str`**:

| Variant | `is_error` | `content_type` | `content` value |
|---|---|---|---|
| `pingjson` | `False` | `str` | `'{"pong": true}'` |
| `multiline` | `False` | `str` | `'line one\nline two with unicode: жена\nline three with special chars: {"x":1}'` |
| `errorexit` | `True` | `str` | `'Exit code 7\nboom'` |

`is_error` is carried at the **block level** (a field of `ToolResultBlock`),
not inside `content`. The SDK prepends `"Exit code N\n"` to the captured
stderr/stdout for non-zero exits.

### S-B.2 Enclosing SDK message type (important!)

Every `ToolResultBlock` was observed **inside a `UserMessage`**, not an
`AssistantMessage`:

```text
message_trace: [SystemMessage, RateLimitEvent, AssistantMessage,
                UserMessage,  <-- tool_result lives here
                AssistantMessage, ResultMessage]
```

**Phase-2 code gap (consequential for phase 4).** The bridge in
`src/assistant/bridge/claude.py:199-231` only extracts blocks from
`AssistantMessage.content` and explicitly skips `UserMessage`
(line 231 comment). Accordingly, `ConversationStore` in phase 2/3
**never stored any `tool_result` rows.** The handler's `_classify` has
a `ToolResultBlock` branch, but it is **dead code** — the bridge never
yields such a block.

This reframes phase-4 Q1 (synthetic summary). The plan assumed tool_result
rows existed in history. They don't. So Q1 requires **two** changes, in
order:

1. **Bridge change** — `bridge/claude.py` must yield blocks from
   `UserMessage.content` too, at least for `ToolResultBlock`, so the
   handler's existing `_classify` branch starts writing `tool_result`
   rows to the DB.
2. **History change** — `history.py` then sees those rows and emits
   the 2 KB snippet per Q1.

Without (1), the Q1 synthetic summary has nothing to summarise.

### S-B.3 Persisted `content_json` shape

The handler wrapper dumps the block list as JSON with
`json.dumps(blocks, ensure_ascii=False, default=str)`. Observed rows
(exactly as phase-4 `_render_tool_summary` will see them after the
bridge fix):

```text
pingjson:
[{"type":"tool_result",
  "tool_use_id":"toolu_01GZwVsgRDW3mVTkHeWW3qZP",
  "content":"{\"pong\": true}",
  "is_error":false}]

multiline:
[{"type":"tool_result",
  "tool_use_id":"toolu_016zamzCBpX3vja55dMwbxs8",
  "content":"line one\nline two with unicode: жена\nline three with special chars: {\"x\":1}",
  "is_error":false}]

errorexit:
[{"type":"tool_result",
  "tool_use_id":"toolu_011YN5HpssiVPKqyQPF4G9D1",
  "content":"Exit code 7\nboom",
  "is_error":true}]
```

The DB-level readback (`SELECT content_json FROM conversations WHERE
block_type='tool_result'`) was empty only because the tmp DB in the
probe did not have a matching `turns` row for the FK; the in-memory
`spying_append` captured the bytes the code would have written. This
is a probe artefact, not a production concern — phase 2 handler inserts
the `turn_id` before block rows under a transaction-less flow, so the
FK is satisfied there.

### S-B.4 Concrete formula for `_render_tool_summary`

Given the observed shape (`content: str`, `is_error: bool` block-level),
the phase-4 `history.py::_render_tool_summary` can be concrete:

```python
def _render_tool_summary(tool_name: str, results: list[dict]) -> str:
    snippets = []
    for r in results:
        content = r.get("content")
        is_err = bool(r.get("is_error"))
        if isinstance(content, list):
            # Defensive: SDK may return list[{type,text}] in future; we
            # never observed it in 0.1.59 with Bash. Join text-blocks,
            # placeholder-ise anything else.
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(str(b.get("text", "")))
                elif isinstance(b, dict):
                    parts.append(f"[{b.get('type','unknown')}]")
            text = "".join(parts)
        elif isinstance(content, str):
            text = content
        elif content is None:
            text = ""
        else:
            text = repr(content)[:200]
        if len(text) > TOOL_RESULT_TRUNCATE:
            text = text[:TOOL_RESULT_TRUNCATE].rstrip() + "...(truncated)"
        prefix = "ошибка" if is_err else "результат"
        snippets.append(f"{prefix} {tool_name}: {text}")
    return "\n".join(snippets)
```

Defensive `list` branch is there purely for future-proofing — the
phase-4 coder does not need an SDK-list test in MVP, only a defensive
unit test with a fake list payload. All three real Bash variants are
`str`.

### S-B.5 Multi-byte safety

Python `str[:N]` slices on code-point boundaries, which is safe for
UTF-8 (no half-rune splits). The `multiline` variant included
`'жена'` (Cyrillic) and was stored unchanged under `ensure_ascii=False`.
No bytes/image handling needed in MVP — the SDK does not produce
non-str Bash results in 0.1.59. Phase 5+ if WebFetch/Read returns
images, revisit with a list-branch test.

### S-B.6 `is_error` semantics

When `Bash` invokes `python tools/x/main.py` and exits non-zero, the
SDK prepends `"Exit code N\n"` to the captured stderr+stdout and sets
`ToolResultBlock.is_error=True`. Phase-4 `_render_tool_summary` should
prefix the snippet with Russian `"ошибка"` instead of `"результат"` so
the model on history replay knows this was a failed invocation.

---

## Citations

- `claude_agent_sdk.types.ToolResultBlock` — dataclass source inspected
  via `inspect.getsource`, SDK `0.1.59`.
- `UserMessage` / `AssistantMessage` — same source, documented field
  `content: str | list[ContentBlock]` and `content: list[ContentBlock]`
  respectively.
- Phase-2 `bridge/claude.py:199-231` — explicit `UserMessage -- skip`
  comment; confirmed no tool_result persistence path.
- Phase-3 `spike-findings.md:96-162` — PostToolUse probe, reused for
  the per-skill hook probe harness pattern.
- Anthropic `claude-agent-sdk-python` `HookEvent` literal union — SDK
  source used to confirm no `active_skill` field on any event.
