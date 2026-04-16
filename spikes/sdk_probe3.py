"""Probe 3: get can_use_tool to actually fire + verify PreToolUse hooks as alternative."""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    query,
)

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent


def banner(t: str) -> None:
    print("\n" + "=" * 72)
    print(" ", t)
    print("=" * 72)


async def probe_can_use_tool_no_allowed() -> dict[str, Any]:
    banner("can_use_tool WITHOUT allowed_tools — should fire callback for each Bash")
    out: dict[str, Any] = {"calls": []}

    async def guard(tool_name: str, tool_input: dict[str, Any], ctx: Any) -> Any:
        out["calls"].append({"tool": tool_name, "cmd": tool_input.get("command", "")[:80]})
        print(f"  >>> guard {tool_name}: {tool_input}")
        return PermissionResultAllow(updated_input=tool_input)

    try:
        opts = ClaudeAgentOptions(cwd=str(PROJECT_ROOT), can_use_tool=guard)

        async def stream():
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "Run `echo probe3-ok` via Bash then say DONE.",
                },
                "parent_tool_use_id": None,
                "session_id": "p3-1",
            }

        text: list[str] = []
        async for m in query(prompt=stream(), options=opts):
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if type(b).__name__ == "TextBlock":
                        text.append(b.text)
        out["text"] = " ".join(text)[:400]
    except Exception as e:
        out["error"] = repr(e)
        traceback.print_exc()
    return out


async def probe_pretooluse_hook() -> dict[str, Any]:
    banner("PreToolUse hook signature and behavior")
    out: dict[str, Any] = {"hook_calls": []}

    async def bash_guard(
        input_data: dict[str, Any], tool_use_id: str | None, ctx: Any
    ) -> dict[str, Any]:
        out["hook_calls"].append(
            {
                "keys": list(input_data.keys()),
                "tool_name": input_data.get("tool_name"),
                "tool_input": input_data.get("tool_input"),
                "tool_use_id": tool_use_id,
                "ctx_attrs": [a for a in dir(ctx) if not a.startswith("_")],
            }
        )
        print(f"  >>> PreToolUse hook: {input_data}")
        cmd = input_data.get("tool_input", {}).get("command", "")
        if ".env" in cmd or ".ssh" in cmd:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "probe3: secrets blocked",
                }
            }
        return {}

    try:
        opts = ClaudeAgentOptions(
            cwd=str(PROJECT_ROOT),
            hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[bash_guard])]},
            allowed_tools=["Bash"],
        )

        async def stream():
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "First run `echo probe3-hook-ok` via Bash. Then attempt `cat .env` via Bash. Report.",
                },
                "parent_tool_use_id": None,
                "session_id": "p3-hook",
            }

        final: list[str] = []
        async for m in query(prompt=stream(), options=opts):
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if type(b).__name__ == "TextBlock":
                        final.append(b.text)
        out["text"] = " ".join(final)[:600]
    except Exception as e:
        out["error"] = repr(e)
        traceback.print_exc()
    return out


async def probe_thinking_via_env() -> dict[str, Any]:
    """Second try at thinking — use effort=high or env var CLAUDE_THINKING."""
    banner("thinking via effort/max_thinking_tokens")
    out: dict[str, Any] = {}
    try:
        opts = ClaudeAgentOptions(
            max_thinking_tokens=4000,
            effort="high",
        )
        tblocks = 0
        async for m in query(
            prompt="Think about whether 1009 is prime. Show your reasoning, final answer only.",
            options=opts,
        ):
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if type(b).__name__ == "ThinkingBlock":
                        tblocks += 1
                        out.setdefault(
                            "first",
                            {
                                "attrs": [a for a in dir(b) if not a.startswith("_")],
                                "text_preview": (getattr(b, "thinking", "") or "")[:160],
                                "sig_len": len(getattr(b, "signature", "") or ""),
                            },
                        )
        out["count"] = tblocks
        print("thinking blocks:", tblocks)
    except Exception as e:
        out["error"] = repr(e)
        traceback.print_exc()
    return out


async def main() -> int:
    rep: dict[str, Any] = {}
    rep["can_use_tool"] = await probe_can_use_tool_no_allowed()
    rep["pretooluse_hook"] = await probe_pretooluse_hook()
    rep["thinking"] = await probe_thinking_via_env()
    (HERE / "sdk_probe3_report.json").write_text(json.dumps(rep, default=str, indent=2))
    banner("DONE probe3")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
