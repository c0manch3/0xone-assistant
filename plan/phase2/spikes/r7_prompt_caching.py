"""R7 — Prompt caching probe for claude-agent-sdk 0.1.59.

Goal: determine whether SDK/CLI automatically inserts `cache_control` markers
on the system prompt + prior turns, or whether we must set them manually.

Method: two back-to-back `query()` calls with the same system_prompt; look at
ResultMessage.usage.cache_read_input_tokens on the second call. If > 0 → auto
caching is in play. If 0 → we need cache_control.

Runs against live OAuth (claude CLI 2.1.114+).
"""
from __future__ import annotations

import asyncio
import json

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    query,
)

SYSTEM_PROMPT = (
    "You are a deterministic echo probe. "
    "Reply with exactly the digits you are told, nothing else. "
    "Keep this lengthy reminder constant so that back-to-back calls can be "
    "compared for prompt caching behaviour: ABCDEFGHIJKLMNOPQRSTUVWXYZ "
    "1234567890 0987654321 ZYXWVUTSRQPONMLKJIHGFEDCBA. "
    "Do not improvise; numbers only."
) * 4  # make it ~800 chars so caching threshold (1024 tokens ~= 4KB) might cross


async def one_call(prompt: str) -> dict:
    opts = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=[],
        max_turns=1,
        setting_sources=None,  # bypass project-local .claude
    )
    usage: dict = {}
    reply = ""
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if getattr(block, "text", None):
                    reply += block.text
        if isinstance(msg, ResultMessage):
            usage = dict(msg.usage or {})
            usage["total_cost_usd"] = msg.total_cost_usd
            usage["model"] = getattr(msg, "model", None)
    return {"reply": reply, "usage": usage}


async def main() -> None:
    print("=== Call 1 ===")
    r1 = await one_call("Say 111")
    print(json.dumps(r1, indent=2))
    print("=== Call 2 (should benefit from cache) ===")
    r2 = await one_call("Say 222")
    print(json.dumps(r2, indent=2))

    cache_read_1 = r1["usage"].get("cache_read_input_tokens", 0) or 0
    cache_read_2 = r2["usage"].get("cache_read_input_tokens", 0) or 0
    cache_creation_1 = r1["usage"].get("cache_creation_input_tokens", 0) or 0
    cache_creation_2 = r2["usage"].get("cache_creation_input_tokens", 0) or 0

    print("\n=== Summary ===")
    print(f"Call 1: cache_creation={cache_creation_1}, cache_read={cache_read_1}")
    print(f"Call 2: cache_creation={cache_creation_2}, cache_read={cache_read_2}")
    if cache_read_2 > 0:
        print("VERDICT: Automatic prompt caching IS in play on call 2.")
    elif cache_creation_1 > 0 and cache_read_2 == 0:
        print(
            "VERDICT: Cache was written on call 1 but NOT read on call 2 "
            "(different sessions / fresh cache key each call)."
        )
    elif cache_creation_1 == 0 and cache_read_2 == 0:
        print(
            "VERDICT: No cache activity. System prompt probably below 1024-token "
            "threshold OR SDK/CLI does not set cache_control automatically."
        )

    # Write machine-readable report
    import pathlib
    out = pathlib.Path(__file__).with_suffix(".json")
    out.write_text(json.dumps({"call1": r1, "call2": r2}, indent=2))
    print(f"\nReport → {out}")


if __name__ == "__main__":
    asyncio.run(main())
