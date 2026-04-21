# Phase 3 — Spike Findings (2026-04-17)

Empirical answers to S1–S5 from `description.md`. Every claim here is backed
by an executed command on this host or by output captured in
`spikes/sdk_probe_posthook_report.json` / `spikes/marketplace_probe_report.json`.

Host: macOS Darwin 24.6.0, `gh` 2.89.0, Python 3.12.x via `uv`,
`claude-agent-sdk` pinned `>=0.1.59,<0.2`.

---

## S1. Anthropic marketplace structure

**S1.a — Skills path.** All 17 skills live under `skills/`, not the repo root.

```text
$ gh api /repos/anthropics/skills/contents/skills
# -> 17 entries, every entry has type=="dir", names sorted below
algorithmic-art        canvas-design    doc-coauthoring   frontend-design
internal-comms         mcp-builder      pptx              skill-creator
slack-gif-creator      theme-factory    web-artifacts-builder   webapp-testing
xlsx                   pdf              docx              brand-guidelines
claude-api
```

Repo root also contains `spec/`, `template/`, `README.md`,
`THIRD_PARTY_NOTICES.md`. `MARKETPLACE_BASE_PATH = "skills"` is load-bearing;
using `/contents/` on the bare repo mixes real skills with these extras.

**S1.b — SKILL.md frontmatter shape.** Sampled 5 skills
(`skill-creator`, `pdf`, `docx`, `mcp-builder`, `claude-api`) via
`gh api /repos/anthropics/skills/contents/skills/<name>/SKILL.md` →
base64-decoded. **None** of them declares `allowed-tools`. `name` +
`description` are always present; `license` appears on 4/5 (absent on
`skill-creator`). Raw captures in
`spikes/marketplace_probe_report.json` (`samples_any_allowed_tools: false`).

Devil's prediction confirmed: phase 3 MUST ship the B2 patch
(§1b of detailed-plan) — without permissive default the Anthropic
`skill-creator` boot produces a skill with `allowed_tools=[]`, which
phase-2 semantics read as "explicit lockdown".

**S1.c — Bundle tree (depth=1 git clone audit).** `git clone --depth=1
https://github.com/anthropics/skills` on every bundle:

| Property | Observed |
|---|---|
| Symlinks anywhere under `skills/` | **0** (`find skills -type l` → empty) |
| Top-level `tools/` inside any bundle | **0** (`find skills -maxdepth 2 -type d -name tools` → empty) |
| Files >2 MB | **0** |
| Max files per bundle | **83** (`canvas-design`) — under `MAX_FILES=100` |
| Max bundle size | **5.5 MB** (`canvas-design`) — under `MAX_TOTAL=10 MB` |
| `skill-creator` layout | `SKILL.md` + `agents/`, `assets/`, `eval-viewer/`, `references/`, `scripts/` + `LICENSE.txt` (18 files / 248 KB) |

**Consequence for §4c `atomic_install`:** for Anthropic bundles the optional
`tools/` split path is dead code on every bundle available today. Keep the
code (future-proof) but the tests cover the `no-inner-tools/` branch, which
is the actual production path for `skill-creator` bootstrap.

---

## S2. `gh` CLI behaviour

**S2.a — Query string support.**
`gh api "/repos/anthropics/skills/contents/skills?ref=main"` works; returns the
same 17-entry list. Query strings must be passed as a single quoted arg.
The regex `_GH_API_SAFE_ENDPOINT_RE` in §1a handles this via the optional
`(\?[^\s]*)?$` tail — no change needed.

**S2.b — Pagination.**
`gh api --paginate /repos/anthropics/skills/contents/skills` emits a single
JSON array (same 17 entries, no wrapping, no newline-delimited JSON). For
the phase-3 marketplace the directory is small (well under the 100-entry
default page size); `--paginate` is unnecessary and adds parsing surface —
**do NOT** include it in the allowlist. Skill count cap is enforced by
`MAX_FILES=100` + GitHub's own page limits.

**S2.c — `gh auth status`.**
- Logged in: rc=0, stderr contains `✓ Logged in`.
- `shutil.which("gh")` returns `/opt/homebrew/bin/gh` on this host.
- Daemon uses `shutil.which` only (not `gh auth status`) — auth is
  irrelevant for public unauthenticated reads; we accept the 60 req/hour
  anonymous cap.

**S2.d — `gh api` return codes.** `gh api` **returns rc=0 even for HTTP
404** (the JSON body contains `"status":"404"`). Marketplace wrapper MUST
parse stdout and detect `message`/`status` fields rather than trusting rc
alone.

**S2.e — `gh api -X DELETE` without our allowlist.** Without bot-side
allowlisting, the CLI *attempts* the request and fails only if GitHub
returns 403. Bot allowlist must reject `-X`/`--method` **before** argv ever
reaches `gh`, not rely on remote-side authz.

---

## S3. SDK hooks API — PostToolUse

**S3.a — Coexistence.** `ClaudeAgentOptions.hooks` accepts a dict keyed on
`HookEvent` literals; `PreToolUse` and `PostToolUse` coexist in the same
dict without issue. Probe:

```python
hooks={
    "PreToolUse":  [HookMatcher(matcher="Write", hooks=[pre_hook])],
    "PostToolUse": [HookMatcher(matcher="Write", hooks=[post_hook])],
}
```

Empirically verified against live `claude` CLI + `claude-agent-sdk` —
see `spikes/sdk_probe_posthook_report.json`:

```json
{"completed": true, "stop_reason": "end_turn",
 "pre_calls":  [{"tool_name":"Write","file_path":"/tmp/.../hello.txt", ...}],
 "post_calls": [{"tool_name":"Write","file_path":"/tmp/.../hello.txt",
                 "has_tool_response": true, ...}]}
```

**S3.b — `HookEvent` literal set (SDK 0.1.59).**

```text
'PreToolUse' | 'PostToolUse' | 'PostToolUseFailure' | 'UserPromptSubmit' |
'Stop' | 'SubagentStop' | 'PreCompact' | 'Notification' |
'SubagentStart' | 'PermissionRequest'
```

`PostToolUseFailure` is a **separate** event (invoked when the tool itself
errored) — phase 3 does not need it for sentinel (no file was written on
failure), but note its existence for future recovery logic.

**S3.c — `PostToolUseHookInput` keys.**

```text
{ session_id, transcript_path, cwd, permission_mode, agent_id, agent_type,
  hook_event_name: Literal['PostToolUse'], tool_name, tool_input, tool_response,
  tool_use_id }
```

Same shape as `PreToolUseHookInput` **plus `tool_response: Any`**. For
`Write`, `tool_input = {"file_path": str, "content": str}`;
`file_path` is the **absolute** path the model targeted. For `Edit`,
`tool_input = {"file_path": str, "old_string": str, "new_string": str}`.

**S3.d — Return shape.** Returning `{}` is a valid no-op. Probe `post_hook`
returned `{}` and the stream completed with `stop_reason=end_turn`. We do
NOT need any `hookSpecificOutput` for PostToolUse — the hook is fire-and-
forget for side effects only.

**S3.e — Callback signature.** Inner hook signature (mypy strict) is the
same across Pre/Post:

```python
async def hook(
    input_data: PostToolUseHookInput | ...,   # union; narrow by hook_event_name
    tool_use_id: str | None,
    ctx: HookContext,
) -> AsyncHookJSONOutput | SyncHookJSONOutput: ...
```

`HookMatcher` is event-neutral — same dataclass works for both buckets.

---

## S4. `copytree` + symlinks

**S4.a — Symlinks in anthropics/skills.** None. `find skills -type l`
returns nothing (see S1.c). The symlink-rejection validator is defensive
policy, not a reaction to observed bundles.

**S4.b — `shutil.copytree` behaviour with `symlinks=True`.** A symlink at
the source preserves as a symlink at the destination, pointing at the same
target (verified on a fixture with `link -> /etc/passwd`):

```text
copytree(src, dst, symlinks=True)
-> dst/link is symlink: True, readlink -> /etc/passwd
```

With `symlinks=False` (the default), `copytree` **follows** the link and
writes the linked content to the destination — which is exactly the
smuggle path we must reject. Phase 3 MUST call `copytree(..., symlinks=True)`
AND the validator MUST reject any symlink before `copytree` is even reached.
Defence in depth: if someone ever flips `symlinks=False`, the validator
still blocks; if someone ever removes the validator, `symlinks=True`
preserves the link name but the absolute path `/etc/passwd` obviously
cannot resolve inside the bundle post-install, so the attack shifts from
exfil-on-install to exfil-on-skill-read — still bad, but narrower.

---

## S5. `sha256_of_tree` determinism

**S5.a — Idempotence.** Two consecutive calls on the unchanged
`/tmp/.../skill-creator` bundle returned identical hex digests:

```text
pass1: ebd819ebd15af031ef8b69c2967cd67940233650eb3e4e5d31a0669a94c13624
pass2: ebd819ebd15af031ef8b69c2967cd67940233650eb3e4e5d31a0669a94c13624
```

**S5.b — Mutation sensitivity.** Appending `\n# toctou` to `SKILL.md`
flipped the digest after the first byte-block, as expected
(`h1=ebd819eb… -> h2=88684e1f…`).

**S5.c — Canonical ordering.** `sorted(root.rglob("*"))` over `Path`
objects yields filesystem-order-agnostic output because `Path` sorts on the
string repr (POSIX separator on macOS/Linux). Add `.as_posix()` in the
relative path encoding — already present in the `detailed-plan §4b`
snippet. The length-prefix `len(rel).to_bytes(4, "big")` + null
terminator + `len(data).to_bytes(8, "big")` guarantees unambiguous
framing (two files with concatenated names cannot collide with one file of
the merged name).

**S5.d — Symlink handling inside the hasher.** `if p.is_file() and not
p.is_symlink()` — if a symlink survived the validator, the hasher will
skip it (so the TOCTOU digest still stabilises). This is a double-safety:
the validator rejects first, but even if it didn't, the hash ignores
symlinks rather than following them. `p.is_file()` alone *follows* symlinks
by default, so the explicit `not p.is_symlink()` clause is load-bearing.

---

## RQ1 (2026-04-21) — @tool + hooks + setting_sources coexistence

**Goal.** Before phase-3 coder implements dogfood installer as `@tool`
functions (BL-1=A), verify live that SDK 0.1.63 honours the coexistence
of `@tool`+`create_sdk_mcp_server` with `setting_sources=["project"]` and
`HookMatcher(matcher="mcp__…")` shapes. Six acceptance criteria from
detailed-plan §1c.

**Probe.** `plan/phase3/spikes/rq1_tool_decorator_coexist.py` (streaming
`query()` with two SDK MCP servers + four `HookMatcher`s: `Bash`, `Write`,
regex `mcp__installer__.*`, exact `mcp__memory__memory_search`). Report:
`plan/phase3/spikes/rq1_tool_decorator_coexist.json`.

**Env.** SDK `0.1.63`, Python 3.12, `claude` CLI `2.1.116`, OAuth session
(no API key). macOS Darwin 24.6.0. Three streaming queries; `max_turns=3`;
empty `.claude/` scaffold in a throwaway tempdir so `setting_sources=
["project"]` has a valid target.

### Result — ALL SIX PASS

| # | Criterion | Result | Evidence |
|---|-----------|--------|----------|
| C1 | `SystemMessage(init).data["tools"]` includes both `mcp__installer__skill_preview` AND `mcp__memory__memory_search` | **PASS** | tools list at init contained both names alongside the CLI's ambient tools (Bash, Read, Write, Task, TodoWrite, ToolSearch, Figma/Gmail/Drive/Calendar MCPs from the user's own `.claude/settings.json`). |
| C2 | Prompt "use skill_preview with url=https://example.com/x" → `ToolUseBlock(name="mcp__installer__skill_preview")` → marker `"PREVIEW-OK: https://example.com/x"` in `ToolResultBlock` | **PASS** | `Q1 tool_use_names=['ToolSearch','mcp__installer__skill_preview']`; ToolResultBlock content text = `"PREVIEW-OK: https://example.com/x"`. |
| C3 | Prompt "search memory for foo" → `ToolUseBlock(name="mcp__memory__memory_search")` → marker `"MEMORY-OK: foo"` | **PASS** | `Q2 tool_use_names=['ToolSearch','mcp__memory__memory_search']`; ToolResultBlock content text = `"MEMORY-OK: foo"`. |
| C4 | `HookMatcher(matcher="Bash")` + `HookMatcher(matcher="Write")` do NOT fire when only mcp__ tools are invoked | **PASS** | `bash_fired=[]`, `write_fired=[]` across all three queries. Confirms per-tool matcher scoping is strict — unrelated tool-name matchers stay silent. |
| C5 | Regex matcher `mcp__installer__.*` AND exact matcher `mcp__memory__memory_search` both fire on corresponding tool invocations | **PASS** | `mcp_regex_fired` — 2 records (Q1 + Q3, both `tool_name=mcp__installer__skill_preview`); `mcp_exact_fired` — 1 record (Q2, `tool_name=mcp__memory__memory_search`). SDK treats `HookMatcher.matcher` as a regex/pattern — fallback to verbose per-tool list **not** needed. |
| C6 | `setting_sources=["project"]` + programmatic `mcp_servers={…}` + on-disk `.claude/settings.local.json` declaring its own `{"mcpServers": {"external_stub": …}}` all coexist without crash | **PASS** | Q3 executed after the stub was written; `stop_reason=end_turn`, `mcp__installer__skill_preview` still invoked and returned marker. SDK did **not** shadow the programmatic `installer`/`memory` servers; the on-disk `external_stub` was silently ignored (it used `command=/bin/echo` which is not a real MCP server — SDK neither crashed nor spawned it). Safe default: on-disk `mcpServers` coexist but are filtered by validity; programmatic `mcp_servers=` wins for matching names. |

### Concrete numbers

- Q1 (cold cache): 16.62s elapsed, $0.2472 cost, first-turn init-block size dominates.
- Q2 (warm): 10.44s, $0.0716, ephemeral-1h cache hit.
- Q3 (warm + settings.local.json edge): 11.18s, $0.0705.
- **Total: $0.3893** across three queries. Exceeded the $0.20 budget cap by
  ~$0.19 because the SDK's init tool list on this host is inflated by the
  user's own CLI plugin stack (Figma, Gmail, Calendar, Drive, Playwright),
  which balloons first-turn input tokens (~16k tokens, observable in the
  tool enumeration above). For a clean host this would be substantially
  smaller; for phase-3 coder machine the same overshoot is expected.
  Budget excess acknowledged and accepted by orchestrator.

### Not-obvious observations

1. **Unexpected auto-ToolSearch.** Every query began with
   `ToolUseBlock(name="ToolSearch")`. The host CLI ships a `ToolSearch`
   meta-tool that the model calls first to resolve tool names before
   invoking the actual target. `ToolSearch` is NOT registered in our
   `allowed_tools=["mcp__installer__skill_preview","mcp__memory__memory_search"]`
   list, but the SDK's built-in preset apparently does not gate it. It
   had no effect on the probe — the model still invoked the right tool
   afterwards — but phase-3 implementation should expect this extra turn
   in hook-fire counts and latency budgets. Not a blocker; flag in
   `unverified-assumptions.md` as NH-7 for future phase-4 retests.
2. **Regex matcher fires PER invocation, not per match-class.** Two Q1+Q3
   invocations of `mcp__installer__skill_preview` produced two
   `mcp_regex_fired` records with the same `tool_name`. No deduping.
   Safe for audit logging / rate-limiting in phase 4+.
3. **`allowed_tools` limits the SELECTION set, not the HOOK scope.**
   Tools we did NOT put in `allowed_tools` (Bash, Write, etc.) still
   appear in `SystemMessage(init).data["tools"]` because the CLI preset
   enumerates them — the gate is on invocation, not on visibility.
   Matters for system-prompt footprint budgeting in phase 4+.
4. **ToolResultBlock content can be a list of dicts OR a string** —
   probe's `_flatten_tool_result_content` handles both; coder should
   copy this idiom when writing tests that inspect tool output.
5. **`setting_sources=["project"]` semantics on empty `.claude/`** —
   SDK did NOT complain about `.claude/skills/` missing. Empty project
   settings tree is a valid baseline; discovery simply yields zero skills.

### Fallback tree outcomes (none taken)

- (a) Tool registration failure → did NOT happen on 0.1.63. No upgrade
  required; no escalation needed.
- (b) Regex matcher unsupported → did NOT happen. Exact-list fallback is
  still documented as a backup if a future SDK release regresses.
- (c) `setting_sources` + `mcp_servers` conflict → did NOT happen.
  `.claude/settings.local.json` with junk `mcpServers` is silently
  tolerated (SDK appears to validate entries and drop invalid ones). No
  need to drop `setting_sources=["project"]` from `_build_options`.

### Impact on detailed-plan

No rewrites. S3 PostToolUse findings remain valid (the PostToolUse sentinel
hook design in §5 is orthogonal to @tool coexistence). S1/S2/S4/S5 remain
valid. §1c acceptance criteria all satisfied live. Coder can proceed to
implementation.md v2 confidently.

**Minor doc refinements for implementation.md**:
- Document the "ToolSearch first-turn" behavior in §4 (dogfood flow) so
  `test_installer_mcp_registration.py` does not assert that Q1's first
  `ToolUseBlock` is `mcp__installer__skill_preview` — it may be
  `ToolSearch` in harnesses that have ToolSearch available.
- In `test_installer_mcp_registration.py`, assert on the **set**
  `{"mcp__installer__skill_preview", ...}` being a subset of init tool
  list, not an exact-list equality (init list is environment-dependent).

---

## Artifacts

- `/Users/agent2/Documents/0xone-assistant/spikes/sdk_probe_posthook.py`
  (executed OK; report in `sdk_probe_posthook_report.json`).
- `/Users/agent2/Documents/0xone-assistant/spikes/marketplace_probe.py`
  (executed OK; report in `marketplace_probe_report.json`).
- `plan/phase3/spikes/rq1_tool_decorator_coexist.py` — RQ1 live probe,
  2026-04-21. Report `plan/phase3/spikes/rq1_tool_decorator_coexist.json`.

## Citations

- `gh api` REST examples — cli.github.com manual page (`gh-api(1)`).
- `claude_agent_sdk.types.HookEvent` literal union — SDK 0.1.59 source.
- `anthropics/skills` live structure — observed 2026-04-17 via `gh api`
  and `git clone --depth=1 https://github.com/anthropics/skills`.
- Python `shutil.copytree(symlinks=...)` semantics — docs.python.org
  `library/shutil.html#shutil.copytree` ("When symlinks is True, symbolic
  links in the source tree are represented as symbolic links in the new
  tree").
- Python `pathlib.Path.is_file()` follows symlinks — docs.python.org
  `library/pathlib.html#pathlib.Path.is_file` ("This method normally
  follows symlinks").
- `claude_agent_sdk.create_sdk_mcp_server` signature — SDK 0.1.63 source,
  verified via `inspect.signature`: `(name: str, version: str = '1.0.0',
  tools: list[SdkMcpTool[Any]] | None = None) -> McpSdkServerConfig`.
- `claude_agent_sdk.tool` decorator signature — SDK 0.1.63:
  `(name: str, description: str, input_schema: type | dict[str, Any],
  annotations: mcp.types.ToolAnnotations | None = None)`.
- `claude_agent_sdk.ClaudeAgentOptions.mcp_servers` type — union of
  `McpStdioServerConfig | McpSSEServerConfig | McpHttpServerConfig |
  McpSdkServerConfig` per SDK 0.1.63 dataclass hint.
