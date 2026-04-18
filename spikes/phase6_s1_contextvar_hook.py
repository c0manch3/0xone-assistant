"""Phase 6 wave-2 spike S-1 — ContextVar propagation into SDK hooks.

Background (B-W2-4):
  The SubagentRequestPicker needs to correlate a pending CLI request to
  the SubagentStart hook fire. Three options considered:

    A) ContextVar — picker sets a ContextVar before `bridge.ask()`; the
       on_subagent_start hook reads it. Cleanest if the SDK preserves
       contextvars across its internal Task.create boundary.
    B) Per-request hook factory — closure captures request_id; picker
       merges into options for that single query. Works but fragile
       (forces us to rebuild `ClaudeAgentOptions` per request, losing
       the "one hook factory at Daemon.start" invariant).
    C) Synthetic prompt marker — picker prepends `<request:N>` to the
       task_text; hook parses. Ugly; coupling through user-visible text.

Goal: verify empirically that a ContextVar set in the caller's scope is
      visible inside the SubagentStart hook callback.

asyncio semantics: `asyncio.create_task` copies the current context, so
ANY task the SDK spawns inside the same event loop inherits our var.
The SDK's `query(...)` is an async generator running on the CURRENT
coroutine, so the hook callback — called via `await cb(...)` from the
generator — has our context by default. This probe confirms it.

Run: uv run python spikes/phase6_s1_contextvar_hook.py
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import time
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    HookContext,
    HookMatcher,
    query,
)

HERE = Path(__file__).resolve().parent
CWD = HERE.parent
REPORT = HERE / "phase6_s1_contextvar_report.json"

# The ContextVar under test. Mirrors how picker would carry a request_id.
CURRENT_REQUEST_ID: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "phase6_current_request_id", default=None
)


async def run() -> dict[str, Any]:
    observations: list[dict[str, Any]] = []

    async def on_start(
        input_data: Any, tool_use_id: str | None, ctx: HookContext
    ) -> dict[str, Any]:
        raw = cast(dict[str, Any], input_data)
        observations.append(
            {
                "event": "SubagentStart",
                "agent_id": raw.get("agent_id"),
                "agent_type": raw.get("agent_type"),
                "ctxvar_seen": CURRENT_REQUEST_ID.get(),
                "at": time.monotonic(),
            }
        )
        return {}

    async def on_stop(
        input_data: Any, tool_use_id: str | None, ctx: HookContext
    ) -> dict[str, Any]:
        raw = cast(dict[str, Any], input_data)
        observations.append(
            {
                "event": "SubagentStop",
                "agent_id": raw.get("agent_id"),
                "ctxvar_seen": CURRENT_REQUEST_ID.get(),
                "at": time.monotonic(),
            }
        )
        return {}

    hooks = {
        "SubagentStart": [HookMatcher(hooks=[on_start])],
        "SubagentStop": [HookMatcher(hooks=[on_stop])],
    }

    agents = {
        "general": AgentDefinition(
            description="Ctxvar probe agent",
            prompt="Reply with 'ok' and stop. Do not use tools.",
            tools=["Read"],
            model="inherit",
            maxTurns=2,
            background=True,
        ),
    }
    opts = ClaudeAgentOptions(
        cwd=str(CWD),
        setting_sources=None,
        max_turns=3,
        allowed_tools=["Task", "Read"],
        hooks=hooks,
        agents=agents,
        system_prompt=(
            "Delegate the user's request to the `general` agent via the "
            "Task tool. After dispatch, reply 'done' and stop."
        ),
    )

    # Run two back-to-back queries sequentially, each with its own
    # request_id, to ensure the hook reads the RIGHT one per call.
    per_call_results: list[dict[str, Any]] = []
    for req_id in (1001, 1002):
        token = CURRENT_REQUEST_ID.set(req_id)
        before_len = len(observations)
        try:
            async for _msg in query(
                prompt=(
                    f"[req {req_id}] Use the `general` agent to reply 'ok'. "
                    "Then reply 'done' yourself."
                ),
                options=opts,
            ):
                pass
        finally:
            CURRENT_REQUEST_ID.reset(token)
        per_call_results.append(
            {
                "request_id": req_id,
                "observations_new": observations[before_len:],
            }
        )

    # Analyse.
    all_seen = [obs for obs in observations if obs["event"] == "SubagentStart"]
    all_req_ids = [obs["ctxvar_seen"] for obs in all_seen]
    per_call_verdicts = []
    for pr in per_call_results:
        saw_start = [
            o for o in pr["observations_new"] if o["event"] == "SubagentStart"
        ]
        correct = all(o["ctxvar_seen"] == pr["request_id"] for o in saw_start)
        per_call_verdicts.append(
            {
                "request_id": pr["request_id"],
                "start_events": len(saw_start),
                "ctxvar_matched_all": correct,
                "values_seen": [o["ctxvar_seen"] for o in saw_start],
            }
        )

    verdict = (
        "PASS"
        if per_call_verdicts
        and all(v["ctxvar_matched_all"] and v["start_events"] > 0 for v in per_call_verdicts)
        else "FAIL"
    )

    return {
        "verdict": verdict,
        "per_call": per_call_verdicts,
        "all_start_ctxvars_seen": all_req_ids,
        "total_observations": len(observations),
        "raw_observations": observations,
        "note": (
            "PASS means asyncio.ContextVar set by the caller is visible "
            "inside the hook callback — picker pattern (B-W2-4 option A) "
            "is viable."
        ),
    }


def main() -> None:
    out = asyncio.run(run())
    REPORT.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"verdict": out["verdict"], "per_call": out["per_call"]}, indent=2))
    print(f"wrote {REPORT}")


if __name__ == "__main__":
    main()
