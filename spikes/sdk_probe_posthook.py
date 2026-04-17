"""Probe: PostToolUse hook — verify SDK invokes hook after Write and `tool_input`
carries `file_path` for the path-based sentinel logic in phase 3.

Run:

    uv run python spikes/sdk_probe_posthook.py

Emits `spikes/sdk_probe_posthook_report.json`. OAuth via `claude` CLI; no
ANTHROPIC_API_KEY. Safe side-effect: writes one file under a temporary
scratch directory and deletes it in `finally`.

Verification targets:
  (a) `hooks={"PreToolUse":[…], "PostToolUse":[…]}` co-exist in ClaudeAgentOptions.
  (b) PostToolUse fires after Write succeeds, with `input_data["tool_name"]=="Write"`
      and `input_data["tool_input"]["file_path"]` set to the absolute path
      the model wrote to.
  (c) Returning `{}` from PostToolUse is a no-op (we do not need any
      `hookSpecificOutput` payload — the hook only has side effects).

If this script refuses to run (no `claude` CLI, OAuth expired), the static
signature checks above (inspect.signature(HookMatcher),
`PostToolUseHookInput` TypedDict schema) are already enough to commit the
implementation plan; leave this file as a stub and note so in
`spike-findings.md` §S3.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    query,
)

HERE = Path(__file__).resolve().parent


async def main() -> dict[str, Any]:
    scratch = Path(tempfile.mkdtemp(prefix="0xone-post-probe-"))
    report: dict[str, Any] = {
        "scratch": str(scratch),
        "pre_calls": [],
        "post_calls": [],
        "error": None,
        "completed": False,
    }

    async def pre_hook(
        input_data: Any, tool_use_id: str | None, ctx: Any
    ) -> dict[str, Any]:
        report["pre_calls"].append(
            {
                "tool_name": input_data.get("tool_name"),
                "file_path": (input_data.get("tool_input") or {}).get("file_path"),
                "tool_use_id": tool_use_id,
            }
        )
        return {}  # allow

    async def post_hook(
        input_data: Any, tool_use_id: str | None, ctx: Any
    ) -> dict[str, Any]:
        report["post_calls"].append(
            {
                "tool_name": input_data.get("tool_name"),
                "file_path": (input_data.get("tool_input") or {}).get("file_path"),
                "tool_use_id": tool_use_id,
                "has_tool_response": "tool_response" in input_data,
            }
        )
        return {}  # PostToolUse no-op

    opts = ClaudeAgentOptions(
        cwd=str(scratch),
        setting_sources=None,
        allowed_tools=["Write", "Bash"],
        hooks={
            "PreToolUse": [HookMatcher(matcher="Write", hooks=[pre_hook])],
            "PostToolUse": [HookMatcher(matcher="Write", hooks=[post_hook])],
        },
    )

    target = scratch / "hello.txt"

    async def stream():
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": (
                    f"Use the Write tool to create the file {target} "
                    "with the exact content `probe-ok`. After the Write "
                    "succeeds, reply with only `DONE` and nothing else."
                ),
            },
            "parent_tool_use_id": None,
            "session_id": "probe-post-1",
        }

    try:
        async for m in query(prompt=stream(), options=opts):
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if type(b).__name__ == "TextBlock":
                        report.setdefault("text", []).append(b.text[:200])
            if isinstance(m, ResultMessage):
                report["stop_reason"] = m.stop_reason
                report["completed"] = True
                break
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    return report


if __name__ == "__main__":
    report = asyncio.run(main())
    out = HERE / "sdk_probe_posthook_report.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    sys.exit(0 if report.get("completed") else 1)
