# Phase 10 — WebSearch built-in tool (default-OFF, billed)

## Goal

Add the Claude Agent SDK built-in **`WebSearch`** tool to the
0xone-assistant bot behind a default-OFF feature flag, and document the
already-shipped **`WebFetch`** tool. The design mirrors the phase-8
vault and phase-9 `render_doc` gating pattern: a pydantic
`BaseSettings` subclass mounted on the root `Settings`, a
`websearch_tool_visible` kwarg on `ClaudeBridge`, a conditional
`allowed_tools.append("WebSearch")` in `_build_options`, and owner-only
Daemon wiring in `main.py`.

## Billing rationale & default-OFF decision

`WebSearch` is a SERVER-SIDE, BILLED tool:

- **$10 / 1000 searches** = $0.01 each, **NO volume discount**.
- Each search counts as one use regardless of the number of results.
- Search **results additionally count as INPUT tokens** (on top of the
  per-search fee) and persist across later turns — a result-heavy turn
  costs more than the raw $0.01 implies.
- The fee applies **regardless of the OAuth / Pro / Max subscription
  path**; the subscription does not discount it. (If a search errors, it
  is not billed.)

Because the tool spends real money, the master gate defaults to
`False`. With `WEBSEARCH_ENABLED` unset the daemon is observably
identical to the pre-phase10 baseline: no `WebSearch` tool advertised,
no new MCP server, no new state files.

## OAuth invariant

No `ANTHROPIC_API_KEY` / `api_key` field is introduced anywhere.
`WebSearchSettings` has only `enabled`, `subagent_enabled`, and
`max_budget_usd`. The CLI session under `~/.claude/` remains the sole
auth path.

> **Live-integration unknown (must validate on first deploy):** whether
> `WebSearch` works end-to-end under the project's OAuth-only invariant
> is NOT proven. There is documented history of Agent SDK / `claude -p`
> under OAuth billing as metered API usage, a separate "Agent SDK
> credit" pool, and an org-admin "enable web search in the Claude
> Console" gate whose interaction with the subscription path is
> unverified. The disabled no-trace path is fully covered by tests; the
> **enabled** path requires a live owner smoke (see implementation.md
> §Live smoke AC).

## WebFetch — already fully shipped (no code change)

`WebFetch` was implemented in an earlier phase and needs no new work:

- Baseline `allowed_tools` entry at `claude.py:376`.
- Two-layer SSRF `PreToolUse` guard `make_webfetch_hook`
  (`hooks.py`): Layer-1 literal IP/hostname block via
  `WEBFETCH_BLOCKED_HOSTNAMES` + `ipaddress` categories; Layer-2 DNS
  `getaddrinfo` off the event loop + `ipaddress` category check.
- Registered as `HookMatcher(matcher="WebFetch")` at `claude.py:337`.
- Present in the `general` + `researcher` subagent tool lists.

Phase 10 only **documents** WebFetch and adds it to the new
`web-audit.log` PostToolUse trail (shared with WebSearch).

## Security posture

### SSRF hook inapplicability (correct)

`make_webfetch_hook` reads a client-side `tool_input.url` and does
DNS / `ipaddress` checks. It matches only `WebFetch`. `WebSearch`
executes **server-side inside Anthropic's infra** — there is no client
URL to SSRF-guard, so the hook is **correctly inapplicable**. No
attempt is made to extend it to WebSearch.

### Prompt-injection defense (untrusted web content)

`WebSearch` results and `WebFetch`ed pages are untrusted text
re-entering the model. Unlike memory / scheduler / subagent content,
web content is NOT nonce-wrapped (the SDK exposes no server-side
result-wrapping hook for the built-in). Defense is therefore
documentation-level: a new `## Web search` section in `system_prompt.md`
instructs the model to treat all search results and fetched pages as
untrusted DATA, never as instructions, and to **never let web content
trigger a state-changing tool** (`memory_write`, `vault_push_now`,
`Write`/`Edit`, exfiltration). The researcher subagent prompt carries
the same instruction.

**Injection blast radius (documented residual risk):** in the owner
turn, `WebSearch` / `WebFetch` coexist with `memory_write`,
`vault_push_now` (when enabled), and `Write`. A malicious result page
is a plausible exfiltration trigger. Prose defenses are defeatable; the
residual risk is accepted for a single-user bot and bounded by the
default-OFF gate plus `max_budget_usd`. Every web call is audited to
`web-audit.log` (0o600) for post-mortem forensics.

### Privacy export (documented tradeoff)

Unlike `WebFetch` (owner-chosen URL), `WebSearch` queries are
**model-composed from private vault/memory context** and leave the box
to Anthropic + third-party search providers — they can leak inferences
the owner never typed. The system prompt instructs the model not to
embed personal identifiers (home address, full names, health terms,
account numbers) into queries without need. The owner should weigh this
before flipping `WEBSEARCH_ENABLED=true`.

## Cost / safety controls (grounded in installed SDK 0.1.63)

1. **Default-OFF env gate** (`WEBSEARCH_ENABLED=false`) — primary
   control.
2. **Owner-only exposure** — only the owner/user bridge opts in; picker
   and audio bridges keep the default `False`.
3. **Separate subagent gate** (`WEBSEARCH_SUBAGENT_ENABLED`, default
   `False`, validated to require `WEBSEARCH_ENABLED=true`) — see below.
4. **`max_turns`** — already bounds tool-call count per turn.
5. **`ClaudeAgentOptions.max_budget_usd`** (`WEBSEARCH_MAX_BUDGET_USD`,
   optional, default `None`) — the only hard per-turn USD brake the SDK
   surfaces. Applied **only** when websearch is visible. Caps the WHOLE
   turn's USD (tokens + searches), so a value set too low can clip long
   non-search turns; ~$0.50–$1.00 is a reasonable starting point.

### CORRECTION vs the task brief — domain/max_uses controls are OUT of scope

`ClaudeAgentOptions` in SDK 0.1.63 exposes **NO**
`allowed_domains` / `blocked_domains` / `max_uses` / `user_location`
fields for the built-in WebSearch tool. Those are Messages-API
`web_search_20250305` server-tool DEFINITION params, not surfaced by the
Agent SDK or the CLI built-in, and there is no `extra_args` CLI flag for
them either. Phase 10 does NOT claim to wire any of them. `max_uses`
(per-request search-count cap) is unavailable via the SDK, which is why
`max_budget_usd` is the only hard brake.

## Bridge & subagent exposure decisions

- **Owner bridge** (`main.py`) — receives
  `websearch_tool_visible=settings.websearch.enabled` plus the optional
  budget. This is the only bridge that can search interactively.
- **Picker bridge / audio bridge** — constructed WITHOUT the kwarg, so
  they keep the default `False`. Background audio jobs and the subagent
  picker must never silently rack up billed searches.
- **`researcher` subagent** — the `AgentDefinition.tools` list (NOT the
  picker bridge's top-level `allowed_tools`) governs the subagent
  session. The shared `sub_agents` dict is passed to both the owner and
  picker bridges, so adding `WebSearch` to `researcher.tools` really
  does grant billed search to the **unattended, picker-dispatched**
  researcher (`maxTurns=15`) — the least-supervised, most-expensive
  path. We therefore gate it behind the **separate** flag
  `WEBSEARCH_SUBAGENT_ENABLED` (default `False`), so an owner can have
  interactive search WITHOUT unattended background search. `general` /
  `worker` stay WebFetch-only / Bash-only regardless. This corrects the
  original spec's (false) claim that "WebSearch never leaks to
  background paths".
