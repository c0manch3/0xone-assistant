"""R10 — session_id collision probe for claude-agent-sdk 0.1.59.

Goal: passing the same `session_id` in streaming-input mode to two concurrent
`query()` calls — what happens?
  - Does the SDK track server-side state keyed by session_id?
  - Do the two JSONL session files collide?
  - Can the SDK crash / hang / corrupt?

Method: asyncio.gather two queries with an explicit session_id envelope, both
with chat-id "test-collide-42". After completion check ~/.claude/projects/...
for the resulting .jsonl file(s).

This is a $0.04 spike; run once.
"""
from __future__ import annotations

import asyncio
import json
import pathlib

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    query,
)

SESSION_ID = "chat-collide-42"


async def one_query(payload: str) -> dict:
    async def stream():
        yield {
            "type": "user",
            "message": {"role": "user", "content": payload},
            "parent_tool_use_id": None,
            "session_id": SESSION_ID,
        }

    opts = ClaudeAgentOptions(
        allowed_tools=[],
        max_turns=1,
        setting_sources=None,
    )
    reply = ""
    result = {}
    async for msg in query(prompt=stream(), options=opts):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if getattr(b, "text", None):
                    reply += b.text
        if isinstance(msg, ResultMessage):
            result = {
                "session_id": getattr(msg, "session_id", None),
                "stop_reason": getattr(msg, "stop_reason", None),
                "duration_ms": getattr(msg, "duration_ms", None),
                "total_cost_usd": msg.total_cost_usd,
                "usage": dict(msg.usage or {}),
            }
    return {"payload": payload, "reply": reply, "result": result}


async def main() -> None:
    # Two parallel queries with the same session_id
    try:
        r1, r2 = await asyncio.gather(
            one_query("Respond literally: APPLE"),
            one_query("Respond literally: ORANGE"),
            return_exceptions=True,
        )
    except Exception as e:  # noqa: BLE001
        print(f"gather raised: {e!r}")
        return

    print("=== Query 1 ===")
    print(json.dumps(r1, indent=2, default=str))
    print("=== Query 2 ===")
    print(json.dumps(r2, indent=2, default=str))

    # Inspect ~/.claude/projects for newly created JSONL files
    home = pathlib.Path.home()
    projs = home / ".claude" / "projects"
    cwd_bits = str(pathlib.Path.cwd()).replace("/", "-")
    candidates = list(projs.glob(f"*{cwd_bits[-30:]}*/*.jsonl"))
    # Filter last 120s
    import time
    cutoff = time.time() - 120
    recent = [p for p in candidates if p.stat().st_mtime > cutoff]
    print(f"\n=== Recent JSONL in {projs} ===")
    for p in recent:
        print(f"  {p} size={p.stat().st_size}")

    report = {
        "r1": r1,
        "r2": r2,
        "recent_jsonl": [{"path": str(p), "size": p.stat().st_size} for p in recent],
    }
    out = pathlib.Path(__file__).with_suffix(".json")
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nReport → {out}")

    # Verdict
    r1_sess = (r1 or {}).get("result", {}).get("session_id") if isinstance(r1, dict) else None
    r2_sess = (r2 or {}).get("result", {}).get("session_id") if isinstance(r2, dict) else None
    print("\n=== Verdict ===")
    print(f"Requested session_id: {SESSION_ID}")
    print(f"Returned session_id call 1: {r1_sess}")
    print(f"Returned session_id call 2: {r2_sess}")
    if r1_sess == r2_sess:
        print("→ SDK HONORED the collision (same session_id returned).")
    else:
        print("→ SDK REASSIGNED session_id per call (safe for our use case).")


if __name__ == "__main__":
    asyncio.run(main())
