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

**Assumption.** The SDK refuses (or at minimum behaves badly with) a user
envelope whose `content` list contains raw `tool_use` and `tool_result`
blocks reconstructed from a prior session. We therefore strip those blocks in
`bridge/history.py::history_to_user_envelopes` and replace them with a
synthetic Russian-language system-note listing tool names that ran.

**How to verify (cost: ~$0.01 per attempt).**

1. Build a `prompt_stream` async generator that yields one envelope of the
   form documented in `bridge/history.py` but with `content=[ToolUseBlock,
   ToolResultBlock, TextBlock("continue")]`, then a follow-up `text` user
   envelope.
2. Call `query(prompt=stream, options=ClaudeAgentOptions(...))` against the
   live SDK with `setting_sources=["project"]`.
3. Observe whether the SDK raises, returns an `is_error=True` ResultMessage,
   or accepts the envelope cleanly.

**If verified safe → change.** Drop the synthetic-note fallback in
`history_to_user_envelopes` and emit tool_use/tool_result verbatim. Phase-4
memory plumbing wins multi-turn fidelity.

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
