# Incident S13 — Skill marker silently dropped on multi-turn history replay

**Status:** fix bundle landed (2026-04-21); awaiting owner retest before
the commit is cut.

**Severity:** critical. Phase-2 skill plumbing appeared broken end-to-end
to the owner, blocking phase acceptance for ~10 hours.

**Discovered by:** owner (Vitaliy) during phase-2 smoke test. Three
independent investigative agents (researcher, general-purpose tracer,
qa-engineer) triangulated the root cause from the CLI JSONL journal.

---

## 1. Symptom

The owner exercised the ping skill as the phase-2 smoke criterion:

> owner → bot: "use the ping skill"
> expected reply: "PONG-FROM-SKILL-OK ... Phase 2 skill plumbing жив."
> actual reply: "Йо. Чё делаем?"

The "Йо. Чё делаем?" text is the model's greeting from the **very first**
turn of phase-2 chat ("Йо" was the owner's first test message before any
skills existed). Every subsequent prompt — whether it referenced skills,
numbers, anything — came back with the same stale greeting. Owner's own
observation: *"всегда одинаковый ответ"*. That was the clue.

Nothing in the bot's Telegram output hinted at a failure: the HTTP
roundtrip completed, `turn_complete` was logged in the daemon log, cost
was accounted, tokens were billed. From outside everything looked fine.

## 2. Forensic timeline

Session `214e91ee` (name redacted to the 8-char prefix) in the CLI's
JSONL journal was the clearest specimen. Reading the file shows 5
`user` envelopes queued in stream-input order before the CLI starts
responding:

1. `user: Йо` (originally turn 1, day 0)
2. `user: remember 777333`
3. `user: what number did I give you?`
4. `user: try the ping skill`
5. `user: use the ping skill` (← the CURRENT message)

Each of those 5 envelopes was a separate object in the stream. The CLI
treats `type: "user"` envelopes as **pending prompts**, not as context,
and processes them sequentially. The assistant envelopes interleaved in
the journal told the story:

- iteration 1 answered "Йо" → model greeted: "Йо. Чё делаем?" →
  `ResultMessage` A.
- iteration 2 answered "remember 777333" → model confirmed → `RM` B.
- iteration 3 answered "what number..." → model recalled 777333 → `RM` C.
- iteration 4 answered "try the ping skill" → model invoked `Skill` tool,
  skill ran, produced PONG marker text → `RM` D.
- iteration 5 answered "use the ping skill" → model produced the
  marker reply again → `RM` E.

Our `bridge.ask` returned immediately after yielding `RM` A. Iterations
B–E existed **only in the CLI journal** — our handler never saw a single
block from them. So only the greeting landed in Postgres, and only the
greeting was emitted to Telegram. Every turn.

## 3. Root cause

Two independent bugs that combined into the observed symptom:

### Bug #1 — `bridge/claude.py` returned too early

```python
if isinstance(message, ResultMessage):
    ...
    yield message
    return   # <-- this
```

The SDK explicitly allows multiple `ResultMessage` per `query()` call —
one per API iteration. `return`-ing on the first shut the generator down
and the `async for` in the handler terminated without seeing the
remaining blocks.

### Bug #2 (root) — `bridge/history.py` emitted one envelope per row

The previous implementation walked the history rows and yielded one SDK
stream-input envelope per consecutive `(turn_id, role)` group. For the
session above that produced 5 distinct `user` envelopes. Because the CLI
queues each `user` envelope as a pending prompt, this **forced** the
multi-iteration behaviour above — without any tool-use actually being
requested by the current turn. The bug was latent: every phase-2 turn
after the first shipped with N pending prompts but only the first got a
chance to reply to the caller.

Bug #2 would be merely inefficient (extra API calls, extra tokens) if
Bug #1 didn't drop everything after the first iteration. Bug #1 would be
merely a future-proofing oversight if Bug #2 didn't routinely trigger
multi-iteration flow on every turn. Together they silently swallowed the
entire skill result.

## 4. Why it escaped the test suite

`test_history_assistant_replay.py` asserted the per-row envelope shape
as if it were the contract, locking the behaviour in place. `test_claude_handler.py`
fed the handler a scripted, single-iteration stream, which Bug #1 handled
just fine. No existing test sent N prior user rows through the real SDK
pipeline. The `R13` live probe validated that assistant envelopes are
accepted by the SDK, but it ran with a single envelope per call and did
not exercise queue semantics.

## 5. Fix applied — bundle A+B+C+D

All four fixes are correlated; applying any one in isolation leaves the
system broken.

### Fix A — `src/assistant/bridge/claude.py`

Replace `return` with `continue` after yielding a `ResultMessage`, so
the generator consumes the entire SDK stream until the SDK closes it
naturally. Multi-iteration responses now propagate every block.

### Fix B — `src/assistant/bridge/history.py` (root fix)

Rewrite `history_to_sdk_envelopes` to emit **at most one** collapsed
context envelope. The envelope's content is a plain-text rendering of
prior turns, wrapped in `[Previous conversation context — …]` markers
so the model recognises it as context rather than a live prompt. The
caller appends the current user message as a second envelope, so the
CLI queue is always exactly two pending prompts and runs exactly one
API iteration. Thinking blocks are still dropped per U2; tool_use and
tool_result rows render as compact annotated lines.

### Fix C — `src/assistant/handlers/message.py`

Accumulate `last_meta` inside the `async for`, and call
`complete_turn(turn_id, meta=last_meta)` exactly once after the loop
exits cleanly. Previously `complete_turn` fired on each `ResultMessage`
— now that we're consuming multi-iteration streams (post Fix A) we must
only finalise after the whole stream lands.

### Fix D — `_safe_query` wrapper in `bridge/claude.py`

Thin async generator that wraps `claude_agent_sdk.query` and swallows
`Unknown message type` errors, logging a warning and terminating
gracefully. Future SDK/CLI bumps routinely introduce new message
variants (e.g. `rate_limit_event` appeared mid-phase); crashing the
whole turn on an unfamiliar type is the wrong default.

## 6. Diff summary

```
src/assistant/bridge/claude.py
  + async def _safe_query(*args, **kwargs)  # Fix D
  - async for message in query(prompt=..., options=...)
  + async for message in _safe_query(prompt=..., options=...)
  - return   # after yielding ResultMessage
  + continue # keep consuming — Fix A

src/assistant/bridge/history.py  (full rewrite — Fix B)
  - per-row / per-consecutive-group envelope emission
  + single collapsed context envelope (plain-text render, wrapped in
    context markers)

src/assistant/handlers/message.py
  - complete_turn called inside the async-for, on every ResultMessage
  + last_meta accumulator; complete_turn called once after the loop
    exits cleanly
```

## 7. Regression coverage

New test: `tests/test_ping_marker_reaches_db.py`

Feeds the handler a scripted multi-block stream matching the real
ping-skill invocation path:

```
TextBlock("let me check the ping")
ToolUseBlock(Skill, ping)
ToolResultBlock(tu_1, "Launching skill: ping")
TextBlock("PONG-FROM-SKILL-OK ... Phase 2 skill plumbing жив.")
ResultMessage(success)
```

Asserts:

- `conversations` contains rows with `block_type` in `{text, tool_use,
  tool_result}` (all four stream blocks persisted);
- at least two `assistant`/`text` rows (narration + marker);
- emit chunks concatenated contain `PONG-FROM-SKILL-OK`;
- `turns.status` transitions to `complete`.

Updated: `tests/test_history_assistant_replay.py` — rewritten for the
collapsed single-envelope shape. Key assertions now:

- empty history → `[]`
- non-empty history → exactly one envelope, text body contains every
  prior user/assistant text plus context markers
- thinking rows are not present in the rendered context
- tool_use/tool_result rows render as annotated one-liners

## 8. Lesson

**Per-row SDK stream-input envelopes are incompatible with multi-pending-
prompt CLI queue semantics.** Any caller that wants to replay prior
context must compress it to a single envelope — otherwise the CLI will
happily answer every prior turn again, on the owner's tokens, before
getting to the current message. This is not documented prominently in
the SDK; we discovered it through the journal.

The generator-level `return` in `bridge.ask` was a secondary factor.
Even with the multi-envelope history fix, a future tool-use loop could
legitimately produce multi-iteration output; `continue` is the correct
default for a pass-through streamer.

Finally: **owner's subjective reports contain load-bearing data**. The
phrase "всегда одинаковый ответ" instantly narrowed the search space
from "skill pipeline broken" to "first-queued-prompt wins every turn" —
a classification nobody's unit tests happened to cover.
