"""Second probe: fix R2 (ThinkingConfigEnabled), re-verify R5 (can_use_tool actually invoked)."""

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
    ThinkingConfigEnabled,
    query,
)

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent


def banner(t: str) -> None:
    print("\n" + "=" * 72)
    print(" ", t)
    print("=" * 72)


async def probe_r2() -> dict[str, Any]:
    banner("R2 (fixed): thinking=ThinkingConfigEnabled(...)")
    out: dict[str, Any] = {}
    try:
        opts = ClaudeAgentOptions(
            thinking={"type": "enabled", "budget_tokens": 2000},  # TypedDict
            max_thinking_tokens=2000,
        )
        blocks: list[dict[str, Any]] = []
        async for m in query(
            prompt="Think step by step about 7*6, then state the final number only.",
            options=opts,
        ):
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if type(b).__name__ == "ThinkingBlock":
                        blocks.append(
                            {
                                "attrs": [a for a in dir(b) if not a.startswith("_")],
                                "thinking_preview": (getattr(b, "thinking", "") or "")[:160],
                                "signature_len": len(getattr(b, "signature", "") or ""),
                            }
                        )
            elif isinstance(m, ResultMessage):
                out["model"] = getattr(m, "model", None)
        out["thinking_blocks"] = blocks
        print("blocks found:", len(blocks))
        if blocks:
            print(blocks[0])
    except Exception as e:
        out["error"] = repr(e)
        traceback.print_exc()
    return out


async def probe_r5_callback() -> dict[str, Any]:
    """R5 redo: ensure can_use_tool actually triggers by running Bash in streaming input."""
    banner("R5 (redo): can_use_tool callback actually invoked?")
    out: dict[str, Any] = {"calls": []}

    async def guard(
        tool_name: str, tool_input: dict[str, Any], context: Any
    ) -> PermissionResultAllow | PermissionResultDeny:
        out["calls"].append(
            {
                "tool": tool_name,
                "input": tool_input,
                "ctx_attrs": [a for a in dir(context) if not a.startswith("_")],
            }
        )
        print(f"  >>> guard invoked: tool={tool_name} input_keys={list(tool_input.keys())}")
        if tool_name == "Bash" and ".env" in tool_input.get("command", ""):
            return PermissionResultDeny(message="probe: .env blocked", interrupt=False)
        return PermissionResultAllow(updated_input=tool_input)

    try:
        opts = ClaudeAgentOptions(
            cwd=str(PROJECT_ROOT),
            allowed_tools=["Bash"],
            can_use_tool=guard,
            setting_sources=None,  # no project skills so nothing else interferes
        )

        async def prompt_stream():
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "Run `echo spike-ok` via the Bash tool, then report the output.",
                },
                "parent_tool_use_id": None,
                "session_id": "probe-r5-redo",
            }

        tool_names: list[str] = []
        final_text: list[str] = []
        async for m in query(prompt=prompt_stream(), options=opts):
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    bt = type(b).__name__
                    if bt == "ToolUseBlock":
                        tool_names.append(b.name)
                    elif bt == "TextBlock":
                        final_text.append(b.text)
            elif isinstance(m, ResultMessage):
                out["stop_reason"] = getattr(m, "stop_reason", None)
                out["num_turns"] = getattr(m, "num_turns", None)
        out["tool_use_blocks"] = tool_names
        out["final_text"] = " ".join(final_text)[:300]
    except Exception as e:
        out["error"] = repr(e)
        traceback.print_exc()
    return out


async def probe_r5_deny() -> dict[str, Any]:
    """R5 deny path — make sure returning Deny actually prevents execution."""
    banner("R5 (deny path): does PermissionResultDeny stop the tool?")
    out: dict[str, Any] = {"calls": []}

    async def guard(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        out["calls"].append({"tool": tool_name})
        print(f"  >>> guard called: {tool_name}")
        return PermissionResultDeny(message="probe: deny-all", interrupt=False)

    try:
        opts = ClaudeAgentOptions(
            cwd=str(PROJECT_ROOT),
            allowed_tools=["Bash"],
            can_use_tool=guard,
        )

        async def stream():
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "Run `echo should-not-run` via Bash. If blocked, just say 'BLOCKED'.",
                },
                "parent_tool_use_id": None,
                "session_id": "probe-r5-deny",
            }

        txt: list[str] = []
        async for m in query(prompt=stream(), options=opts):
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if type(b).__name__ == "TextBlock":
                        txt.append(b.text)
        out["text"] = " ".join(txt)[:400]
    except Exception as e:
        out["error"] = repr(e)
        traceback.print_exc()
    return out


async def main() -> int:
    rep: dict[str, Any] = {}
    rep["R2"] = await probe_r2()
    rep["R5_redo"] = await probe_r5_callback()
    rep["R5_deny"] = await probe_r5_deny()
    (HERE / "sdk_probe2_report.json").write_text(json.dumps(rep, default=str, indent=2))
    banner("DONE (probe2)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
