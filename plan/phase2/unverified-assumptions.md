# Phase 2 — Unverified SDK assumptions (verification checklist)

These four behavioural assumptions about `claude-agent-sdk==0.1.59` shape the
phase-2 implementation but were NOT empirically proven during the spike or
the rework. Each requires a live, OAuth-authenticated Claude Code CLI to
verify. They are recorded here as a checklist for whoever does manual QA
(typically the bot owner) — the rework removed the fake xfail tests that
used to "document" them, because a hard-coded `raise AssertionError` masked
as `xfail` produced a permanently green-but-meaningless signal.

Once any item is verified, **delete it from this file** and either lift the
corresponding workaround from the codebase (U1/U2/U5) or relax the related
defence (none for U3 today).

---

## U1 — Direct replay of `tool_use` / `tool_result` blocks in history envelopes

**Status (2026-04-20):** PARTIALLY RESOLVED by R13. Assistant envelopes with
text blocks ARE honored by the SDK (verified in
`plan/phase2/spikes/r13_assistant_envelope_replay.py`). The v2.1 implementation
replays user AND assistant turns verbatim via `history_to_sdk_envelopes`.

**What remains unverified (narrower scope):** specifically feeding an
assistant envelope whose `content` list contains `tool_use` + `tool_result`
mixed with text — the R13 probe only validated text-only assistant replay.

**How to verify (cost: ~$0.02).**

1. In turn 1, have the model call a Bash tool (e.g. `run python
   tools/ping/main.py`). Capture the full AssistantMessage.content list
   (TextBlock? + ToolUseBlock + ToolResultBlock — note ToolResultBlock
   lives on `role='user'` not `role='assistant'` per B5).
2. In turn 2, feed exactly that envelope sequence plus a follow-up
   `"what tool did you just call?"` user envelope.
3. Observe whether the SDK raises, returns `is_error=True`, or the model
   correctly references the tool_use call.

**If verified safe → change.** The current `_classify_block` already routes
tool_use to `role='assistant'` and tool_result to `role='user'` (B5). If
replay works, no additional change is needed — the v2.1 code already emits
those envelopes. If replay fails, the fallback is to filter tool_use /
tool_result rows out of `history_to_sdk_envelopes` and reintroduce the
synthetic-note shim (reverting R13's simplification for tool rows only).

---

## U2 — SDK rejects cross-session ThinkingBlock replay

**Assumption.** A `ThinkingBlock` produced in one session and re-fed into
another causes the SDK to reject the call (signature mismatch). We therefore
filter `block_type == 'thinking'` rows out of every history envelope.

**How to verify (cost: ~$0.05; thinking-enabled model required).**

1. Run a turn with `ClaudeAgentOptions(max_thinking_tokens=2000,
   effort="high")` and capture a `ThinkingBlock` (`.thinking` and
   `.signature` strings).
2. Build a fresh session's user envelope whose `content` includes that exact
   ThinkingBlock dict.
3. Call `query(...)` and look for a hard rejection (exception or
   `ResultMessage(is_error=True, ...)`).

**If verified safe → change.** Drop the `block_type == 'thinking'` filter
in `history_to_user_envelopes` to preserve full reasoning history.

---

## U3 — Skill discovery via `.claude/skills -> ../skills` symlink

**Assumption.** With `cwd=project_root` and `setting_sources=["project"]`,
the SDK reads `<project_root>/.claude/skills/<name>/SKILL.md` transparently
when `.claude/skills` is a symbolic link rather than a real directory. The
unit test `tests/test_u3_symlink_skill_discovery.py` validates that **our**
manifest builder traverses the symlink, but does NOT exercise the SDK side.

**How to verify (cost: ~$0.01).**

1. Run the daemon (`just run`) with the bootstrap symlink in place
   (`ls -la .claude/skills` should report `-> ../skills`).
2. From the Telegram chat: `use the ping skill`.
3. Inspect logs for `sdk_init` and confirm the `skills` list contains
   `'ping'`. The bot should respond with the parsed `pong` value.

**If verification fails → fallback.** Replace the symlink with a real
directory and have phase-3 skill-installer write into both
`<project_root>/skills/<name>/` and `<project_root>/.claude/skills/<name>/`
(or change `bootstrap.ensure_skills_symlink` to copy on push instead of
symlinking).

---

## U5 — `HookMatcher(matcher=...)` accepts regex patterns

**Assumption.** SDK treats `matcher=` as an exact tool-name string, not a
regex. We therefore register seven `HookMatcher`s (Bash, Read, Write, Edit,
Glob, Grep, WebFetch). If the SDK accepted regex, we could collapse to two
(`Bash` + `(Read|Write|Edit|Glob|Grep|WebFetch)`).

**How to verify (cost: ~$0.01).**

1. Register a single `HookMatcher(matcher="Re.*", hooks=[probe])` and run a
   turn that triggers `Read`. Inspect whether `probe` was invoked.
2. Repeat with `matcher="(Read|Write)"`.

**If verified safe → change.** Collapse the seven matchers in
`bridge/claude.py::_build_options` to two; keep the same hook closures.

---

## U9 — WebFetch SSRF guard residual: TOCTOU DNS rebinding

**Assumption.** Our two-layer WebFetch guard (literal hostname strings +
`socket.getaddrinfo` → `ipaddress` category check, see spike-findings §R9
and `implementation.md §2.1 _make_webfetch_hook`) blocks every direct SSRF
attempt we can enumerate. A motivated attacker controlling a DNS zone can
still evade it: return a public IP to our `getaddrinfo` call, then rotate
RR-records so the CLI resolves a private IP microseconds later. Our hook
returns `allow`; the fetch hits `127.0.0.1` / `169.254.169.254` / the
internal network.

**How to verify (cost: $0; requires setting up an attacker DNS server).**

1. Run a local `dnsmasq` with a zone serving two A records that alternate
   in priority (or a tiny python+dnslib responder).
2. Send a prompt that triggers WebFetch against `http://<attacker-host>/`
   where the first getaddrinfo hit returns a public IP (e.g. `93.184.216.34`)
   and the second returns `169.254.169.254`.
3. Observe whether the hook allows and the fetch reaches the private IP.

**If verified exploitable (high confidence a priori) → mitigations.** This is
well-documented in the OWASP SSRF cheatsheet as "unsolvable at application
layer". Options:

- OS-level egress ACL (`iptables`/`pf` blocking RFC1918/link-local outbound
  from the daemon's UID). Hardest, most effective.
- Pin the resolution: `socket.getaddrinfo` once in the hook, then pass the IP
  (not the hostname) to WebFetch. Requires CLI tool to accept IP-literal URL
  with `Host:` header override — current CLI does not expose this.
- Remove `WebFetch` from `allowed_tools` entirely until phase 6+ hardening.

**Phase-2 posture:** accept the residual risk. The bot is single-user on an
owner workstation, not on EC2 (no IMDS target) and not on a corporate LAN
with juicy targets. Document in `README.md` "Security considerations" section
so the owner knows that WebFetch is best-effort-guarded. The two-layer hook
still blocks the trivial cases (direct IP-literal URLs, localhost), which
covers ~99% of real prompt-injection attempts.

---

## U10 — Assistant envelope shape stability across SDK versions

**Assumption.** R13 (2026-04-20) verified live on `claude-agent-sdk==0.1.59`
+ CLI `2.1.114` that streaming-input envelopes of shape
`{"type":"assistant","message":{"role":"assistant","content":[...],"model":...}}`
are accepted AND honored (model sees prior assistant turns in context). A
future SDK or CLI point-release could tighten schema validation — for
example, reject envelopes where `session_id` on the inner message is
missing or mismatches, or require `model` to be a valid model id — and
silently break history replay.

**Impact if broken.** `history_to_sdk_envelopes` would emit envelopes the
SDK rejects OR quietly drops. In the drop case the bot loses multi-turn
continuity (which was U1's original symptom before R13 resolved it). In
the reject case, the bridge raises `ClaudeBridgeError("sdk error: …")` and
the user sees a generic failure message.

**How to verify on any SDK upgrade (cost: ~$0.02).**

1. Re-run `uv run python plan/phase2/spikes/r13_assistant_envelope_replay.py`
   after any `claude-agent-sdk` or `claude` CLI upgrade.
2. Check the JSON report: `verdict.envelope_accepted` AND
   `verdict.envelope_honored` must both be `true`.
3. If either is `false`, fall back to the synthetic-note approach (drop
   assistant rows from `history_to_sdk_envelopes`, reintroduce
   `[system-note: в прошлом ходе ассистент ответил …]`-style summaries).

**Tracked via:** `tests/test_u10_assistant_envelope_shape_live.py` with
marker `requires_claude_cli` — skipped in CI, owner-invoked on upgrade.

**Phase-2 posture:** accept this risk. The SDK is pinned
`claude-agent-sdk>=0.1.59,<0.2` so minor releases can change behaviour but
we see the upgrade and re-run R13. Major (0.2) requires a new spike anyway.

---

## Why these are not automated tests

* Each verification requires an authenticated `claude` CLI plus a real model
  turn (~$0.01–$0.05 each). CI does not (and should not) hold OAuth
  credentials.
* The previous attempt at automation used `@pytest.mark.xfail(strict=False)`
  with a hard-coded `raise AssertionError` — this always reported `xfail`,
  never `xpass`, so it produced no signal whether the assumption held. Worse,
  it gave a false sense of "we have a regression test for this".
* Manual checklist + structured audit log entries (`pretool_decision`,
  `sdk_init`) is the honest representation of where we are.
