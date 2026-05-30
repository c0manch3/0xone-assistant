# Phase 10 — implementation steps

## Ordered changes

1. **`src/assistant/config.py`** — add `WebSearchSettings(BaseSettings)`
   (`env_prefix="WEBSEARCH_"`) with:
   - `enabled: bool = False` — owner-bridge master gate.
   - `subagent_enabled: bool = False` — separate gate for the
     researcher subagent; validated to require `enabled=True`.
   - `max_budget_usd: float | None = None` — optional per-turn USD cap;
     validated `> 0` when set.
   Mount on root `Settings` as
   `websearch: WebSearchSettings = Field(default_factory=WebSearchSettings)`
   right after `render_doc`. No `api_key` field (OAuth invariant).

2. **`src/assistant/bridge/claude.py`** —
   - Add `websearch_tool_visible: bool = False` and
     `websearch_max_budget_usd: float | None = None` kwargs to
     `__init__` (next to `render_doc_tool_visible`); store both.
   - In `_build_options`, after the `render_doc` block:
     `if self._websearch_tool_visible: allowed_tools.append("WebSearch")`.
     NO `mcp_servers` entry (built-in tool).
   - After the `agents` block, set
     `opts_kwargs["max_budget_usd"]` only when websearch is visible AND
     a cap is configured.

3. **`src/assistant/main.py`** — on the OWNER bridge `ClaudeBridge(...)`
   pass `websearch_tool_visible=self._settings.websearch.enabled` and
   `websearch_max_budget_usd=self._settings.websearch.max_budget_usd`.
   Leave the audio bridge and picker bridge at default `False`.

4. **`src/assistant/subagent/definitions.py`** — in `build_agents`,
   conditionally append `"WebSearch"` to the researcher's tools when
   `settings.websearch.subagent_enabled` is True. `general` / `worker`
   unchanged. Researcher prompt updated with the untrusted-web-content
   instruction.

5. **`src/assistant/bridge/hooks.py`** — in `make_posttool_hooks`, add an
   `on_web_tool` audit hook (same compact JSONL shape as the memory /
   scheduler audits, distinct `web-audit.log` at 0o600) and register it
   under `HookMatcher(matcher="WebSearch")` and
   `HookMatcher(matcher="WebFetch")`.

6. **`src/assistant/bridge/system_prompt.md`** — add a `## Web search`
   section: memory-vs-web guidance, cost & privacy awareness, and the
   untrusted-web-content injection-defense paragraph.

7. **`.env.example`** — add the `WEBSEARCH_*` vars (all commented,
   default off).

8. **`plan/phase10/description.md` + `implementation.md`** — created
   (planning ran read-only).

## Test plan

`tests/test_phase10_websearch_gate.py` (mirrors
`tests/test_phase8_disabled_invariants.py`):

- Config defaults: `enabled` / `subagent_enabled` False, budget None.
- `subagent_enabled=True` without `enabled=True` raises.
- `max_budget_usd <= 0` raises.
- Default bridge → `WebSearch` NOT in allowed_tools, no `websearch`
  mcp_servers entry.
- `websearch_tool_visible=True` → `WebSearch` in allowed_tools, still no
  mcp_servers entry.
- Explicit `websearch_tool_visible=False` overrides `enabled=True`
  (picker / audio invariant).
- Owner-wiring mirror enables the tool.
- Baseline tools (Bash/Read/.../WebFetch/Skill) present regardless.
- `max_budget_usd` flows to options only when visible AND configured;
  `None` leaves SDK default.
- Researcher gains `WebSearch` iff `subagent_enabled`; interactive
  `enabled` alone does not leak to the researcher; general/worker
  unchanged.

Run:

```
.venv/bin/python -m pytest tests/test_phase10_websearch_gate.py \
  tests/test_phase8_disabled_invariants.py \
  tests/test_bridge_subagent_options.py -q
```

## Live smoke AC (deploy-after-every-phase non-negotiable)

The static tests only cover the DISABLED no-trace path. Because
OAuth + WebSearch billing is an unverified live-integration unknown, the
owner must, on the VPS:

1. Flip `WEBSEARCH_ENABLED=true`, restart, issue a real search query.
2. Confirm it returns under OAuth **without** demanding an
   `ANTHROPIC_API_KEY` (if it nudges for an API key, REVERT — that would
   violate the OAuth non-negotiable).
3. Confirm `max_budget_usd` caps as expected (if configured) and that a
   `web-audit.log` line is written.
4. Confirm an injection-y result page does NOT trigger a vault / memory /
   Write call.
5. With the flag back OFF, confirm the daemon is byte-identical in
   behaviour to the pre-phase10 baseline (no WebSearch advertised, no new
   MCP server, no new state files).

## Deferred / open

- **PostToolUse result nonce-wrapping** — whether SDK 0.1.63 honors a
  PostToolUse hook that REWRITES tool_response content (to wrap web
  results in untrusted tags like memory/scheduler) is UNVERIFIED.
  Phase 10 ships the audit hook (return `{}` no-op, known to work) +
  prose defense, and defers mechanical result-wrapping to a spike.
- **Domain / `max_uses` controls** — confirmed non-existent in SDK
  0.1.63; permanently out of scope.
