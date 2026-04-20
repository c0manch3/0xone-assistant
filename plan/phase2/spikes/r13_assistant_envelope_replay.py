"""R13 — assistant envelope replay probe for claude-agent-sdk 0.1.59.

Goal: verify that feeding a prior assistant turn as a streaming-input envelope
(type="assistant", role="assistant", content=[{"type":"text",...}]) is honored
by the SDK/CLI — i.e., the model sees the assistant reply and can reference it
in the next user turn.

Design notes:
- R1 spike (probe_r1_prompt_iterable) showed that feeding two USER envelopes
  back-to-back in streaming-input works: user2="What name?" → model replies
  with "Vita" from user1. So user-envelope replay IS honored.
- This probe's question: does the SDK ALSO accept and honor an "assistant"
  envelope interleaved between two user envelopes?
- If YES, we can enrich history_to_user_envelopes to also replay assistant
  turns (full multi-turn fidelity).
- If NO (SDK ignores it or errors), we stick with the synthetic-note fallback.

Test design:
- probe_with_assistant: user1 ("give me your LUCKY_NUMBER") + assistant1
  (text contains the SENTINEL string "LUCKY_NUMBER=424242") + user2
  ("What was the LUCKY_NUMBER you told me? Reply with ONLY the number").
  If assistant envelope is honored, model replies "424242". If ignored,
  model has no way to know it and says something like "I didn't say one".
- probe_without_assistant (baseline): same user1 + user2, no assistant
  between. Model never saw 424242. Control for false-positive.

A stricter post-filter: search for the literal string "424242" in the reply.
Baseline's probability of hallucinating "424242" is ~1/1e6; negligible.

Cost: ~$0.02. Run once; report to plan/phase2/spikes/r13_assistant_envelope_replay.json.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    query,
)

SESSION_ID = "chat-r13-probe"
SENTINEL = "424242"


async def _run_stream(stream) -> dict:
    opts = ClaudeAgentOptions(
        allowed_tools=[],
        setting_sources=None,
    )
    reply = ""
    error: str | None = None
    result_meta: dict = {}
    try:
        async for msg in query(prompt=stream, options=opts):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if getattr(b, "text", None):
                        reply += b.text
            elif isinstance(msg, ResultMessage):
                result_meta = {
                    "stop_reason": getattr(msg, "stop_reason", None),
                    "cost_usd": msg.total_cost_usd,
                    "duration_ms": getattr(msg, "duration_ms", None),
                    "num_turns": getattr(msg, "num_turns", None),
                }
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    return {"reply": reply, "error": error, "result_meta": result_meta}


async def probe_with_assistant_envelope() -> dict:
    """Assistant envelope contains SENTINEL; user2 asks for it. If envelope
    is honored, reply will contain SENTINEL."""
    async def stream():
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": "What's my LUCKY_NUMBER? Please tell me now.",
            },
            "parent_tool_use_id": None,
            "session_id": SESSION_ID,
        }
        yield {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": f"Your LUCKY_NUMBER is {SENTINEL}. I'll remember it.",
                    }
                ],
                "model": "claude-opus-4-6",
            },
            "parent_tool_use_id": None,
            "session_id": SESSION_ID,
        }
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": "Please repeat back my LUCKY_NUMBER. Reply with ONLY the 6-digit number, nothing else.",
            },
            "parent_tool_use_id": None,
            "session_id": SESSION_ID,
        }

    r = await _run_stream(stream())
    r["scenario"] = "with_assistant_envelope"
    r["contains_sentinel"] = SENTINEL in r["reply"]
    return r


async def probe_without_assistant_baseline() -> dict:
    """Control: same user1 + user2, no assistant envelope. Model never saw
    SENTINEL; reply should NOT contain it."""
    async def stream():
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": "What's my LUCKY_NUMBER? Please tell me now.",
            },
            "parent_tool_use_id": None,
            "session_id": SESSION_ID,
        }
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": "Please repeat back my LUCKY_NUMBER. Reply with ONLY the 6-digit number, nothing else.",
            },
            "parent_tool_use_id": None,
            "session_id": SESSION_ID,
        }

    r = await _run_stream(stream())
    r["scenario"] = "without_assistant_baseline"
    r["contains_sentinel"] = SENTINEL in r["reply"]
    return r


async def main() -> None:
    report: dict = {"sdk": "claude-agent-sdk==0.1.59", "cli": "2.1.114"}
    report["probes"] = []

    print("=== Probe 1: baseline (user-only, no sentinel in input) ===", flush=True)
    try:
        r1 = await asyncio.wait_for(probe_without_assistant_baseline(), timeout=60)
    except asyncio.TimeoutError:
        r1 = {"scenario": "without_assistant_baseline", "error": "TimeoutError"}
    report["probes"].append(r1)
    print(json.dumps(r1, indent=2, ensure_ascii=False), flush=True)

    print("=== Probe 2: assistant envelope contains sentinel ===", flush=True)
    try:
        r2 = await asyncio.wait_for(probe_with_assistant_envelope(), timeout=60)
    except asyncio.TimeoutError:
        r2 = {"scenario": "with_assistant_envelope", "error": "TimeoutError"}
    report["probes"].append(r2)
    print(json.dumps(r2, indent=2, ensure_ascii=False), flush=True)

    # Verdict:
    #   envelope_accepted: SDK did not error
    #   honored: probe 2 contains sentinel AND probe 1 does not
    envelope_accepted = r2.get("error") is None
    honored = r2.get("contains_sentinel", False) and not r1.get("contains_sentinel", False)
    report["verdict"] = {
        "envelope_accepted": envelope_accepted,
        "envelope_honored": honored,
        "recommendation": (
            "assistant envelope replay OK — can enrich history_to_user_envelopes"
            if envelope_accepted and honored
            else "assistant envelope NOT honored — keep synthetic-note fallback"
        ),
    }

    out = pathlib.Path(__file__).with_suffix(".json")
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nreport written to {out}", flush=True)
    print(f"verdict: {report['verdict']}", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
