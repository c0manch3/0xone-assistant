"""RQ1 live hermetic probe — @tool-decorator + hooks + setting_sources coexistence.

Run: ./.venv/bin/python plan/phase3/spikes/rq1_tool_decorator_coexist.py

Verifies six acceptance criteria before coder implements phase-3 dogfood
@tool installer:

1. SystemMessage(init).data["tools"] contains both
   "mcp__installer__skill_preview" AND "mcp__memory__memory_search".
2. Model invocation "use skill_preview tool with url=https://example.com/x"
   -> ToolUseBlock(name="mcp__installer__skill_preview") -> marker
   "PREVIEW-OK: https://example.com/x".
3. Model invocation "search memory for foo via memory_search tool" ->
   ToolUseBlock(name="mcp__memory__memory_search") -> marker
   "MEMORY-OK: foo".
4. Default HookMatcher(matcher="Bash") and HookMatcher(matcher="Write") do
   NOT fire when mcp__ tools are invoked (hooks scoped narrowly by
   tool_name).
5. Explicit HookMatcher(matcher="mcp__installer__.*") DOES fire on
   mcp__installer__skill_preview (confirms regex matcher works; if this
   fails, installer SDK can fall back to exact tool-name matchers).
6. setting_sources=["project"] + mcp_servers={...} coexist without error.
   Edge case: .claude/settings.local.json with {"mcpServers": {...}} —
   probe captures SDK merge/override behavior.

Budget: <= $0.20 (3 paid query() invocations; each is a single turn with
ephemeral 1h cache after the first, so subsequent turns are cheap).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk import query as _raw_query

# --- probe config ---
BUDGET_USD = 0.20
REPORT_PATH = Path(__file__).with_suffix(".json")


# --- tool defs ---
@tool("skill_preview", "Test installer preview tool", {"url": str})
async def skill_preview(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": f"PREVIEW-OK: {args['url']}"}
        ]
    }


@tool("memory_search", "Placeholder memory search tool", {"query": str})
async def memory_search(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": f"MEMORY-OK: {args['query']}"}
        ]
    }


# --- stream input helper for streaming mode ---
async def _single_prompt(text: str) -> AsyncIterable[dict[str, Any]]:
    yield {
        "type": "user",
        "message": {"role": "user", "content": text},
    }


# --- hook recorders ---
def make_recorder(
    label: str, record: list[dict[str, Any]]
) -> Any:
    async def hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        ctx: Any,
    ) -> dict[str, Any]:
        record.append(
            {
                "hook_label": label,
                "tool_name": input_data.get("tool_name"),
                "hook_event_name": input_data.get("hook_event_name"),
            }
        )
        return {}  # passive audit; no decision

    return hook


async def run_probe(
    tmp_project: Path, report: dict[str, Any]
) -> None:
    installer_server = create_sdk_mcp_server(
        name="installer",
        version="0.1.0",
        tools=[skill_preview],
    )
    memory_server = create_sdk_mcp_server(
        name="memory",
        version="0.1.0",
        tools=[memory_search],
    )

    bash_fired: list[dict[str, Any]] = []
    write_fired: list[dict[str, Any]] = []
    mcp_regex_fired: list[dict[str, Any]] = []
    mcp_exact_fired: list[dict[str, Any]] = []

    hooks: dict[str, list[HookMatcher]] = {
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[make_recorder("bash", bash_fired)]),
            HookMatcher(matcher="Write", hooks=[make_recorder("write", write_fired)]),
            HookMatcher(
                matcher="mcp__installer__.*",
                hooks=[make_recorder("mcp_regex", mcp_regex_fired)],
            ),
            HookMatcher(
                matcher="mcp__memory__memory_search",
                hooks=[make_recorder("mcp_exact", mcp_exact_fired)],
            ),
        ],
    }

    opts = ClaudeAgentOptions(
        cwd=str(tmp_project),
        setting_sources=["project"],
        allowed_tools=[
            "mcp__installer__skill_preview",
            "mcp__memory__memory_search",
        ],
        mcp_servers={
            "installer": installer_server,
            "memory": memory_server,
        },
        hooks=hooks,
        max_turns=3,
        system_prompt=(
            "You are a tool-invocation probe. When asked to use a tool, invoke "
            "it once with the given arguments and respond with the tool's "
            "output text verbatim. Do not add commentary. Do not use Bash, "
            "Write, or any other tool."
        ),
    )

    # ---- Criterion 1 + 2: invoke skill_preview ----
    prompt1 = (
        "Use the skill_preview tool with url='https://example.com/x'. "
        "Then respond with only the tool's output text."
    )
    t0 = time.monotonic()
    events_1, tools_list, cost_1, stop_1 = await _drive(prompt1, opts)
    dt1 = time.monotonic() - t0

    # ---- Criterion 3: invoke memory_search ----
    prompt2 = (
        "Use the memory_search tool with query='foo'. "
        "Then respond with only the tool's output text."
    )
    t0 = time.monotonic()
    events_2, _, cost_2, stop_2 = await _drive(prompt2, opts)
    dt2 = time.monotonic() - t0

    # ---- Criterion 6 edge: .claude/settings.local.json with mcpServers ----
    settings_dir = tmp_project / ".claude"
    settings_dir.mkdir(exist_ok=True)
    (settings_dir / "settings.local.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "external_stub": {
                        "command": "/bin/echo",
                        "args": ["stub"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    prompt3 = "Use skill_preview with url='https://example.com/edge'."
    t0 = time.monotonic()
    events_3, tools_list_3, cost_3, stop_3 = await _drive(prompt3, opts)
    dt3 = time.monotonic() - t0

    # ---- analyze results ----
    tool_use_names_1 = _tool_use_names(events_1)
    tool_use_names_2 = _tool_use_names(events_2)
    tool_use_names_3 = _tool_use_names(events_3)
    preview_marker = _extract_marker(events_1, "PREVIEW-OK:")
    memory_marker = _extract_marker(events_2, "MEMORY-OK:")

    report["probe"] = {
        "tmp_project": str(tmp_project),
        "query_1_prompt": prompt1,
        "query_1_elapsed_sec": round(dt1, 2),
        "query_1_stop": stop_1,
        "query_1_cost_usd": cost_1,
        "query_1_tools_in_init": tools_list,
        "query_1_tool_use_names": tool_use_names_1,
        "query_1_preview_marker_seen": preview_marker,
        "query_2_prompt": prompt2,
        "query_2_elapsed_sec": round(dt2, 2),
        "query_2_stop": stop_2,
        "query_2_cost_usd": cost_2,
        "query_2_tool_use_names": tool_use_names_2,
        "query_2_memory_marker_seen": memory_marker,
        "query_3_prompt": prompt3,
        "query_3_elapsed_sec": round(dt3, 2),
        "query_3_stop": stop_3,
        "query_3_cost_usd": cost_3,
        "query_3_tools_in_init": tools_list_3,
        "query_3_tool_use_names": tool_use_names_3,
        "hook_fires": {
            "bash_fired": bash_fired,
            "write_fired": write_fired,
            "mcp_regex_fired": mcp_regex_fired,
            "mcp_exact_fired": mcp_exact_fired,
        },
        "total_cost_usd": round(
            sum(c for c in (cost_1, cost_2, cost_3) if c is not None), 4
        ),
    }

    # ---- criterion evaluation ----
    c1 = (
        tools_list is not None
        and "mcp__installer__skill_preview" in tools_list
        and "mcp__memory__memory_search" in tools_list
    )
    c2 = (
        "mcp__installer__skill_preview" in tool_use_names_1
        and preview_marker is not None
        and "https://example.com/x" in preview_marker
    )
    c3 = (
        "mcp__memory__memory_search" in tool_use_names_2
        and memory_marker is not None
        and memory_marker.endswith("foo")
    )
    # Hooks Bash/Write should not fire at all (no Bash/Write invoked).
    c4 = len(bash_fired) == 0 and len(write_fired) == 0
    # mcp_regex should fire at least once on skill_preview invocation.
    c5_regex = any(
        f.get("tool_name") == "mcp__installer__skill_preview"
        for f in mcp_regex_fired
    )
    c5_exact = any(
        f.get("tool_name") == "mcp__memory__memory_search"
        for f in mcp_exact_fired
    )
    c5 = c5_regex and c5_exact
    # Criterion 6: query 3 did not crash (settings.local.json with mcpServers
    # tolerated alongside programmatic mcp_servers=).
    c6 = stop_3 is not None  # non-None stop reason = no crash

    report["criteria"] = {
        "C1_tools_in_init_both_present": c1,
        "C2_skill_preview_invoked_and_marker_seen": c2,
        "C3_memory_search_invoked_and_marker_seen": c3,
        "C4_bash_write_hooks_did_not_fire": c4,
        "C5_regex_and_exact_mcp_matchers_fired": c5,
        "C5_regex_detail": c5_regex,
        "C5_exact_detail": c5_exact,
        "C6_setting_sources_plus_mcp_servers_coexist": c6,
    }
    report["all_pass"] = all(
        [c1, c2, c3, c4, c5, c6]
    )


async def _drive(
    prompt: str, opts: ClaudeAgentOptions
) -> tuple[list[dict[str, Any]], list[str] | None, float | None, str | None]:
    events: list[dict[str, Any]] = []
    tools_list: list[str] | None = None
    cost: float | None = None
    stop: str | None = None
    async for msg in _raw_query(prompt=_single_prompt(prompt), options=opts):
        if isinstance(msg, SystemMessage):
            events.append({"type": "system", "subtype": msg.subtype, "data_keys": list(getattr(msg, "data", {}).keys())})
            if msg.subtype == "init":
                tools_list = list(msg.data.get("tools", []))
        elif isinstance(msg, AssistantMessage):
            for block in msg.content:
                bt = type(block).__name__
                rec: dict[str, Any] = {"type": "assistant_block", "block_type": bt}
                if bt == "ToolUseBlock":
                    rec["name"] = getattr(block, "name", None)
                    rec["input"] = getattr(block, "input", None)
                elif bt == "TextBlock":
                    rec["text"] = getattr(block, "text", "")
                events.append(rec)
        elif isinstance(msg, UserMessage):
            # ToolResultBlock comes in UserMessage per SDK shape.
            for block in msg.content:
                bt = type(block).__name__
                rec = {"type": "user_block", "block_type": bt}
                if bt == "ToolResultBlock":
                    # Flatten content to serializable form
                    content = getattr(block, "content", None)
                    try:
                        rec["content"] = _flatten_tool_result_content(content)
                    except Exception:
                        rec["content_repr"] = repr(content)[:500]
                    rec["is_error"] = getattr(block, "is_error", None)
                events.append(rec)
        elif isinstance(msg, ResultMessage):
            events.append(
                {
                    "type": "result",
                    "stop_reason": getattr(msg, "stop_reason", None),
                    "total_cost_usd": getattr(msg, "total_cost_usd", None),
                    "num_turns": getattr(msg, "num_turns", None),
                    "session_id": getattr(msg, "session_id", None),
                }
            )
            cost = getattr(msg, "total_cost_usd", None)
            stop = getattr(msg, "stop_reason", None)
        else:
            events.append({"type": "other", "class": type(msg).__name__})
    return events, tools_list, cost, stop


def _flatten_tool_result_content(content: Any) -> Any:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return [
            {
                "type": getattr(b, "type", None) or (b.get("type") if isinstance(b, dict) else None),
                "text": getattr(b, "text", None) or (b.get("text") if isinstance(b, dict) else None),
            }
            for b in content
        ]
    return repr(content)[:500]


def _tool_use_names(events: list[dict[str, Any]]) -> list[str]:
    return [
        e["name"]
        for e in events
        if e.get("type") == "assistant_block"
        and e.get("block_type") == "ToolUseBlock"
        and e.get("name")
    ]


def _extract_marker(events: list[dict[str, Any]], prefix: str) -> str | None:
    # Markers appear inside ToolResultBlock content (UserMessage) as the
    # literal text returned by the @tool handler. Also check assistant
    # TextBlock as fallback (model may echo).
    for e in events:
        if e.get("type") == "user_block" and e.get("block_type") == "ToolResultBlock":
            c = e.get("content")
            if isinstance(c, list):
                for b in c:
                    t = b.get("text") if isinstance(b, dict) else None
                    if isinstance(t, str) and prefix in t:
                        return t.strip()
            elif isinstance(c, str) and prefix in c:
                return c.strip()
        if e.get("type") == "assistant_block" and e.get("block_type") == "TextBlock":
            t = e.get("text", "")
            if isinstance(t, str) and prefix in t:
                return t.strip()
    return None


async def main() -> int:
    report: dict[str, Any] = {
        "sdk_version": "0.1.63",
        "python_version": sys.version.split()[0],
        "cwd_at_launch": os.getcwd(),
        "budget_usd_cap": BUDGET_USD,
    }
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="rq1-") as td:
        tmp_project = Path(td)
        # Minimal project scaffold so `setting_sources=["project"]` has
        # something to read. Empty .claude dir is fine.
        (tmp_project / ".claude").mkdir()
        try:
            await run_probe(tmp_project, report)
            report["error"] = None
        except Exception as exc:  # noqa: BLE001 - probe catches all
            report["error"] = {
                "class": type(exc).__name__,
                "message": str(exc),
            }
            raise
        finally:
            report["elapsed_sec_total"] = round(time.monotonic() - started, 2)
            REPORT_PATH.write_text(
                json.dumps(report, indent=2, default=str), encoding="utf-8"
            )

    print(json.dumps(report["criteria"], indent=2))
    print(f"\nTotal cost: ${report['probe']['total_cost_usd']:.4f}")
    print(f"All pass: {report['all_pass']}")
    print(f"Report: {REPORT_PATH}")
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
