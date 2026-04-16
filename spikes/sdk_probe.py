"""SDK spike for phase 2 — empirically answers R1–R5 against claude-agent-sdk.

Run:
    uv run --with claude-agent-sdk python spikes/sdk_probe.py

Auth: relies on `claude` CLI already logged in (OAuth). No ANTHROPIC_API_KEY.
Output: prints observations for R1–R5 to stdout; returns non-zero on auth failure.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import traceback
from pathlib import Path
from typing import Any

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    ThinkingConfigEnabled,
    UserMessage,
    query,
)

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


async def probe_r4_basic_query() -> dict[str, Any]:
    """R4: what messages come out of `query()`, what's inside AssistantMessage.content."""
    banner("R4: basic query() → message stream")
    out: dict[str, Any] = {"messages": []}
    try:
        async for message in query(prompt="Say hello in 3 words."):
            mtype = type(message).__name__
            info: dict[str, Any] = {"type": mtype}
            if isinstance(message, AssistantMessage):
                info["content_types"] = [type(b).__name__ for b in message.content]
                info["first_text"] = next(
                    (b.text for b in message.content if hasattr(b, "text")), None
                )
                info["model"] = getattr(message, "model", None)
            elif isinstance(message, ResultMessage):
                info.update(
                    subtype=getattr(message, "subtype", None),
                    duration_ms=getattr(message, "duration_ms", None),
                    num_turns=getattr(message, "num_turns", None),
                    total_cost_usd=getattr(message, "total_cost_usd", None),
                    usage=getattr(message, "usage", None),
                    session_id=getattr(message, "session_id", None),
                    stop_reason=getattr(message, "stop_reason", None),
                )
            elif isinstance(message, UserMessage):
                info["content"] = getattr(message, "content", None)
            elif isinstance(message, SystemMessage):
                info["subtype"] = getattr(message, "subtype", None)
            out["messages"].append(info)
            print(json.dumps(info, default=str)[:400])
    except Exception as e:
        out["error"] = repr(e)
        traceback.print_exc()
    return out


async def probe_r1_multi_turn_via_resume() -> dict[str, Any]:
    """R1a: resume=session_id path — one-shot query() that continues a previous session."""
    banner("R1a: multi-turn via resume=session_id")
    out: dict[str, Any] = {}
    try:
        session_id = None
        async for m in query(prompt="Remember the number 42."):
            if isinstance(m, ResultMessage):
                session_id = m.session_id
        out["first_session"] = session_id
        print(f"first session_id={session_id}")

        if session_id:
            opts = ClaudeAgentOptions(resume=session_id)
            echoed: list[str] = []
            async for m in query(prompt="What number did I ask you to remember?", options=opts):
                if isinstance(m, AssistantMessage):
                    for b in m.content:
                        if hasattr(b, "text"):
                            echoed.append(b.text)
            out["follow_up_text"] = " ".join(echoed)
            print("follow-up:", out["follow_up_text"][:300])
    except Exception as e:
        out["error"] = repr(e)
        traceback.print_exc()
    return out


async def probe_r1_prompt_iterable() -> dict[str, Any]:
    """R1b: query(prompt=async-iterable-of-dicts) — injecting history as SDK stream input."""
    banner("R1b: query(prompt=AsyncIterable[dict]) — streaming input mode")
    out: dict[str, Any] = {}

    async def messages():
        # Per SDK docs: each element is an SDKUserMessage envelope.
        yield {
            "type": "user",
            "message": {"role": "user", "content": "Call me Vita."},
            "parent_tool_use_id": None,
            "session_id": "probe-iter-1",
        }
        yield {
            "type": "user",
            "message": {"role": "user", "content": "What name did I give?"},
            "parent_tool_use_id": None,
            "session_id": "probe-iter-1",
        }

    collected: list[str] = []
    try:
        async for m in query(prompt=messages()):
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if hasattr(b, "text"):
                        collected.append(b.text)
            elif isinstance(m, ResultMessage):
                out["stop_reason"] = getattr(m, "stop_reason", None)
        out["text"] = " ".join(collected)
        print("result:", out["text"][:400])
    except Exception as e:
        out["error"] = repr(e)
        traceback.print_exc()
    return out


async def probe_r3_setting_sources_and_skills() -> dict[str, Any]:
    """R3: does setting_sources=['project'] + cwd pick up .claude/skills/*/SKILL.md?"""
    banner("R3: setting_sources=['project'] + skills discovery")
    out: dict[str, Any] = {}

    # Create a throwaway skill under .claude/skills/probe_ping (NOT touching real skills/).
    skills_root = PROJECT_ROOT / ".claude" / "skills"
    skill_dir = skills_root / "probe_ping"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\n"
        "name: probe_ping\n"
        "description: Probe skill. When the user asks 'run the probe_ping skill',"
        " reply with the single word PONG_PROBE and nothing else.\n"
        "---\n\n"
        "Reply with the single word PONG_PROBE.\n",
        encoding="utf-8",
    )
    print(f"wrote {skill_md}")

    try:
        opts = ClaudeAgentOptions(
            cwd=str(PROJECT_ROOT),
            setting_sources=["project"],
        )
        asked: list[str] = []
        sys_info: list[dict[str, Any]] = []
        async for m in query(prompt="Run the probe_ping skill.", options=opts):
            if isinstance(m, SystemMessage):
                sys_info.append(
                    {
                        "subtype": getattr(m, "subtype", None),
                        "keys": list(getattr(m, "data", {}).keys())
                        if isinstance(getattr(m, "data", None), dict)
                        else None,
                    }
                )
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if hasattr(b, "text"):
                        asked.append(b.text)
        out["text"] = " ".join(asked)
        out["system_messages"] = sys_info
        print("assistant text:", out["text"][:400])
        print("system msgs:", sys_info[:3])
    except Exception as e:
        out["error"] = repr(e)
        traceback.print_exc()
    finally:
        # Cleanup probe skill so it does not pollute subsequent tests / real setup.
        try:
            skill_md.unlink(missing_ok=True)
            skill_dir.rmdir()
        except OSError:
            pass
    return out


async def probe_r5_can_use_tool() -> dict[str, Any]:
    """R5: can_use_tool permission callback signature & behavior."""
    banner("R5: can_use_tool permission callback")
    out: dict[str, Any] = {"calls": []}

    async def guard(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        out["calls"].append({"tool": tool_name, "input_keys": list(tool_input.keys())})
        print(f"  can_use_tool called: tool={tool_name} input={str(tool_input)[:120]}")
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if ".env" in cmd or ".ssh" in cmd:
                return PermissionResultDeny(message="blocked by probe guard", interrupt=False)
        return PermissionResultAllow(updated_input=tool_input)

    try:
        opts = ClaudeAgentOptions(
            cwd=str(PROJECT_ROOT),
            setting_sources=["project"],
            allowed_tools=["Bash"],
            can_use_tool=guard,
            # can_use_tool requires streaming input — spec says must use async iterable
        )

        async def prompt_iter():
            yield {
                "type": "user",
                "message": {"role": "user", "content": "Run `echo hello-from-bash` via Bash."},
                "parent_tool_use_id": None,
                "session_id": "probe-r5",
            }

        async for m in query(prompt=prompt_iter(), options=opts):
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    bt = type(b).__name__
                    if bt == "ToolUseBlock":
                        out["calls"].append({"tool_use_block": {"name": b.name, "input": b.input}})
        out["guard_signature"] = str(inspect.signature(guard))
    except Exception as e:
        out["error"] = repr(e)
        traceback.print_exc()
    return out


async def probe_r2_thinking() -> dict[str, Any]:
    """R2: ThinkingBlock — need extra_args={'thinking': ...}? Model dependency?"""
    banner("R2: ThinkingBlock")
    out: dict[str, Any] = {}
    try:
        opts = ClaudeAgentOptions(
            extra_args={"thinking": json.dumps({"type": "enabled", "budget_tokens": 2000})},
        )
        tblocks: list[dict[str, Any]] = []
        async for m in query(
            prompt="Think briefly about 2+2, then answer with the number only.", options=opts
        ):
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if type(b).__name__ == "ThinkingBlock":
                        tblocks.append(
                            {
                                "thinking_preview": (getattr(b, "thinking", "") or "")[:120],
                                "signature_len": len(getattr(b, "signature", "") or ""),
                                "attrs": [a for a in dir(b) if not a.startswith("_")],
                            }
                        )
            elif isinstance(m, ResultMessage):
                out["model"] = getattr(m, "model", None)
        out["thinking_blocks"] = tblocks
        print(f"thinking blocks: {len(tblocks)}")
        if tblocks:
            print(tblocks[0])
    except Exception as e:
        out["error"] = repr(e)
        traceback.print_exc()
    return out


async def probe_options_fields() -> dict[str, Any]:
    """Meta: inspect ClaudeAgentOptions fields and can_use_tool / hooks types."""
    banner("META: ClaudeAgentOptions fields")
    from dataclasses import fields

    info = {
        "fields": [(f.name, str(f.type)) for f in fields(ClaudeAgentOptions)],
    }
    for line in info["fields"]:
        print(line)
    return info


async def main() -> int:
    report: dict[str, Any] = {"sdk_version": claude_agent_sdk.__version__}
    report["meta"] = await probe_options_fields()
    report["R4"] = await probe_r4_basic_query()
    report["R1a_resume"] = await probe_r1_multi_turn_via_resume()
    report["R1b_prompt_iter"] = await probe_r1_prompt_iterable()
    report["R3_skills"] = await probe_r3_setting_sources_and_skills()
    report["R5_can_use_tool"] = await probe_r5_can_use_tool()
    report["R2_thinking"] = await probe_r2_thinking()
    out_path = HERE / "sdk_probe_report.json"
    out_path.write_text(json.dumps(report, default=str, indent=2), encoding="utf-8")
    banner(f"DONE — report written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
