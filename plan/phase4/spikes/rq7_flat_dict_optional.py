"""RQ7 live probe — does the @tool flat-dict input_schema enforce required
fields, or does it accept partial argument dicts?

Run: ./.venv/bin/python plan/phase4/spikes/rq7_flat_dict_optional.py

Question (phase 4, memory-tools design):

    @tool("memory_search", "...", {"query": str, "area": str, "limit": int})
    async def memory_search(args): ...

When the model calls memory_search(query="foo") WITHOUT supplying ``area``
or ``limit``, does the SDK/MCP layer:

    (a) reject the call with is_error=True before the handler fires
        (strict required-fields), OR
    (b) invoke the handler with args={"query": "foo"} (permissive — handler
        must .get() defaults itself)?

If (a) — phase-4 plan must switch all memory tools to explicit JSON-Schema
``required: [...]`` form.

If (b) — flat-dict is fine; matches phase-3 installer pattern.

Method
------
Register TWO probe @tool functions on a live create_sdk_mcp_server:

  - echo_required:  flat-dict {"query": str, "area": str, "limit": int}
  - echo_extra:     flat-dict {"name": str}  (probe extra-key behavior)

Drive the model through THREE invocation forms via a system prompt that
asks it to call echo_required three times (full / partial / extra), then
once on echo_extra. Capture init tools list, every ToolUseBlock, and
every ToolResultBlock. Log everything to ``rq7_flat_dict_optional.txt``
+ a JSON sidecar with the criterion verdict.

Bound: 30 minutes wall, $0.40 cap.
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
    ResultMessage,
    SystemMessage,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk import query as _raw_query

REPORT_JSON = Path(__file__).with_suffix(".json")
REPORT_TXT = Path(__file__).with_suffix(".txt")
BUDGET_USD = 0.40
WALL_BUDGET_SEC = 1800  # 30 minutes


# ---------------------------------------------------------------------------
# Probe @tool functions — flat-dict input_schema with multiple keys.
# ---------------------------------------------------------------------------
@tool(
    "echo_required",
    "Echo all received arguments verbatim. Schema declares query, area, "
    "limit — used by the probe to test how the SDK handles missing or "
    "extra arguments.",
    {"query": str, "area": str, "limit": int},
)
async def echo_required(args: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(args.keys()) if isinstance(args, dict) else None
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"ECHO_REQ: keys={keys} args={json.dumps(args, default=str)}"
                ),
            }
        ]
    }


@tool(
    "echo_extra",
    "Echo all received arguments. Schema declares only name — used to "
    "test whether unknown keys are forwarded or stripped by the layer.",
    {"name": str},
)
async def echo_extra(args: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(args.keys()) if isinstance(args, dict) else None
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"ECHO_EXTRA: keys={keys} args={json.dumps(args, default=str)}"
                ),
            }
        ]
    }


# ---------------------------------------------------------------------------
# Stream-input helper.
# ---------------------------------------------------------------------------
async def _single_prompt(text: str) -> AsyncIterable[dict[str, Any]]:
    yield {
        "type": "user",
        "message": {"role": "user", "content": text},
    }


# ---------------------------------------------------------------------------
# Driver — collects events from one query() invocation.
# ---------------------------------------------------------------------------
async def _drive(
    prompt: str, opts: ClaudeAgentOptions
) -> tuple[list[dict[str, Any]], list[str] | None, dict[str, Any] | None,
           float | None, str | None]:
    events: list[dict[str, Any]] = []
    tools_list: list[str] | None = None
    init_data: dict[str, Any] | None = None
    cost: float | None = None
    stop: str | None = None
    async for msg in _raw_query(prompt=_single_prompt(prompt), options=opts):
        if isinstance(msg, SystemMessage):
            events.append(
                {
                    "type": "system",
                    "subtype": msg.subtype,
                    "data_keys": list(getattr(msg, "data", {}).keys()),
                }
            )
            if msg.subtype == "init":
                tools_list = list(msg.data.get("tools", []))
                # Capture full init data for tool-schema inspection.
                init_data = dict(msg.data)
        elif isinstance(msg, AssistantMessage):
            for block in msg.content:
                bt = type(block).__name__
                rec: dict[str, Any] = {
                    "type": "assistant_block",
                    "block_type": bt,
                }
                if bt == "ToolUseBlock":
                    rec["name"] = getattr(block, "name", None)
                    rec["input"] = getattr(block, "input", None)
                    rec["id"] = getattr(block, "id", None)
                elif bt == "TextBlock":
                    rec["text"] = getattr(block, "text", "")
                events.append(rec)
        elif isinstance(msg, UserMessage):
            for block in msg.content:
                bt = type(block).__name__
                rec = {"type": "user_block", "block_type": bt}
                if bt == "ToolResultBlock":
                    content = getattr(block, "content", None)
                    try:
                        rec["content"] = _flatten_tool_result_content(content)
                    except Exception:
                        rec["content_repr"] = repr(content)[:500]
                    rec["is_error"] = getattr(block, "is_error", None)
                    rec["tool_use_id"] = getattr(block, "tool_use_id", None)
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
    return events, tools_list, init_data, cost, stop


def _flatten_tool_result_content(content: Any) -> Any:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                out.append({"type": b.get("type"), "text": b.get("text")})
            else:
                out.append(
                    {
                        "type": getattr(b, "type", None),
                        "text": getattr(b, "text", None),
                    }
                )
        return out
    return repr(content)[:500]


def _tool_use_records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": e.get("name"),
            "input": e.get("input"),
            "id": e.get("id"),
        }
        for e in events
        if e.get("type") == "assistant_block"
        and e.get("block_type") == "ToolUseBlock"
    ]


def _tool_result_records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "tool_use_id": e.get("tool_use_id"),
            "is_error": e.get("is_error"),
            "content": e.get("content"),
        }
        for e in events
        if e.get("type") == "user_block"
        and e.get("block_type") == "ToolResultBlock"
    ]


# ---------------------------------------------------------------------------
# Main probe.
# ---------------------------------------------------------------------------
async def run_probe(tmp_project: Path, report: dict[str, Any]) -> None:
    server = create_sdk_mcp_server(
        name="probe",
        version="0.1.0",
        tools=[echo_required, echo_extra],
    )

    opts = ClaudeAgentOptions(
        cwd=str(tmp_project),
        setting_sources=["project"],
        allowed_tools=[
            "mcp__probe__echo_required",
            "mcp__probe__echo_extra",
        ],
        mcp_servers={"probe": server},
        max_turns=8,  # multiple tool calls in one query
        system_prompt=(
            "You are a tool-invocation probe. Follow the user's "
            "instructions LITERALLY. Do not add commentary, do not "
            "second-guess argument shapes, do not omit arguments the "
            "user told you to include, do not add arguments the user "
            "did not specify. Call the requested tool with the "
            "EXACT arguments the user lists. After each call, briefly "
            "state the verbatim text returned by the tool, then proceed "
            "to the next requested call."
        ),
    )

    # ---- Form 1: all args present ----
    prompt_full = (
        "Call the mcp__probe__echo_required tool ONCE with these EXACT "
        "arguments and no others: query=\"foo\", area=\"inbox\", limit=5. "
        "After the tool returns, state the verbatim ECHO_REQ output."
    )
    t0 = time.monotonic()
    ev_full, tools_init, init_data, cost_full, stop_full = await _drive(
        prompt_full, opts
    )
    dt_full = time.monotonic() - t0

    # ---- Form 2: only required-by-intent (query) supplied ----
    prompt_partial = (
        "Call the mcp__probe__echo_required tool ONCE with ONLY this "
        "argument: query=\"bar\". Do NOT supply area. Do NOT supply "
        "limit. After the tool returns, state the verbatim ECHO_REQ "
        "output."
    )
    t0 = time.monotonic()
    ev_partial, _, _, cost_partial, stop_partial = await _drive(
        prompt_partial, opts
    )
    dt_partial = time.monotonic() - t0

    # ---- Form 3: extra unknown key on echo_required ----
    prompt_extra_on_required = (
        "Call the mcp__probe__echo_required tool ONCE with these EXACT "
        "arguments: query=\"baz\", area=\"all\", limit=3, foo=\"bar\". "
        "Yes, include the foo argument even though it is not in the "
        "schema — this is a probe to see what happens. After the tool "
        "returns, state the verbatim ECHO_REQ output."
    )
    t0 = time.monotonic()
    ev_extra, _, _, cost_extra, stop_extra = await _drive(
        prompt_extra_on_required, opts
    )
    dt_extra = time.monotonic() - t0

    # ---- Form 4: echo_extra with extra key (separate tool, smaller schema) ----
    prompt_extra_on_extra = (
        "Call the mcp__probe__echo_extra tool ONCE with these EXACT "
        "arguments: name=\"alice\", surplus=\"hello\". Yes, include the "
        "surplus argument even though it is not in the schema. After "
        "the tool returns, state the verbatim ECHO_EXTRA output."
    )
    t0 = time.monotonic()
    ev_extra2, _, _, cost_extra2, stop_extra2 = await _drive(
        prompt_extra_on_extra, opts
    )
    dt_extra2 = time.monotonic() - t0

    # ---- analyze ----
    forms = {
        "form1_full": {
            "prompt": prompt_full,
            "elapsed_sec": round(dt_full, 2),
            "stop_reason": stop_full,
            "cost_usd": cost_full,
            "tool_uses": _tool_use_records(ev_full),
            "tool_results": _tool_result_records(ev_full),
        },
        "form2_partial": {
            "prompt": prompt_partial,
            "elapsed_sec": round(dt_partial, 2),
            "stop_reason": stop_partial,
            "cost_usd": cost_partial,
            "tool_uses": _tool_use_records(ev_partial),
            "tool_results": _tool_result_records(ev_partial),
        },
        "form3_extra_on_required": {
            "prompt": prompt_extra_on_required,
            "elapsed_sec": round(dt_extra, 2),
            "stop_reason": stop_extra,
            "cost_usd": cost_extra,
            "tool_uses": _tool_use_records(ev_extra),
            "tool_results": _tool_use_records(ev_extra) and _tool_result_records(ev_extra),
        },
        "form4_extra_on_echo_extra": {
            "prompt": prompt_extra_on_extra,
            "elapsed_sec": round(dt_extra2, 2),
            "stop_reason": stop_extra2,
            "cost_usd": cost_extra2,
            "tool_uses": _tool_use_records(ev_extra2),
            "tool_results": _tool_result_records(ev_extra2),
        },
    }

    # init schema visibility — probe whether init exposes per-tool schemas
    init_schema_keys: list[str] | None = None
    init_probe_schema: Any = None
    if init_data is not None:
        init_schema_keys = sorted(init_data.keys())
        # Some SDK versions expose tool definitions under different keys.
        for k in (
            "tools",
            "tool_definitions",
            "tool_definitions_list",
            "mcp_tools",
        ):
            v = init_data.get(k)
            if isinstance(v, list) and v:
                # If list of dicts with name+input_schema, capture our probe entries.
                if all(isinstance(x, dict) for x in v):
                    init_probe_schema = [
                        x for x in v if "probe" in str(x.get("name", ""))
                    ]
                    if init_probe_schema:
                        break

    report["init_meta"] = {
        "tools_in_init_count": (
            len(tools_init) if tools_init is not None else None
        ),
        "probe_in_init": [
            t for t in (tools_init or [])
            if "mcp__probe__" in t
        ],
        "init_data_keys": init_schema_keys,
        "init_probe_schema": init_probe_schema,
    }
    report["forms"] = forms
    report["totals"] = {
        "cost_usd": round(
            sum(c for c in (
                cost_full, cost_partial, cost_extra, cost_extra2
            ) if c is not None),
            4,
        ),
        "elapsed_sec": round(
            dt_full + dt_partial + dt_extra + dt_extra2, 2
        ),
    }

    # ---- Verdict ----
    # Strict (a):  form2 (partial) -> tool_result is_error=True with a
    #              JSON-Schema validation message; OR no tool_use at all
    #              because SDK rejected before invocation.
    # Permissive (b): form2 -> handler invoked with args={"query": "bar"}
    #                  -> ECHO_REQ keys=['query'] surfaced in result.
    partial_results = forms["form2_partial"]["tool_results"]
    partial_uses = forms["form2_partial"]["tool_uses"]
    partial_any_error = any(
        bool(r.get("is_error")) for r in partial_results
    )
    partial_handler_fired = any(
        any(
            "ECHO_REQ" in (b.get("text") or "")
            for b in (r.get("content") or [])
            if isinstance(b, dict)
        )
        for r in partial_results
    )

    if partial_handler_fired and not partial_any_error:
        verdict = "permissive"
    elif partial_any_error:
        verdict = "strict"
    elif not partial_uses:
        verdict = "strict_or_model_refused"
    else:
        verdict = "inconclusive"

    # form3 / form4 extra-key analysis (informational, not the primary verdict)
    extra_results = forms["form3_extra_on_required"]["tool_results"] or []
    extra_handler_fired_with_foo = any(
        any(
            "ECHO_REQ" in (b.get("text") or "") and "foo" in (b.get("text") or "")
            for b in (r.get("content") or [])
            if isinstance(b, dict)
        )
        for r in extra_results
    )
    extra_any_error = any(
        bool(r.get("is_error")) for r in extra_results
    )

    extra2_results = forms["form4_extra_on_echo_extra"]["tool_results"]
    extra2_handler_fired_with_surplus = any(
        any(
            "ECHO_EXTRA" in (b.get("text") or "")
            and "surplus" in (b.get("text") or "")
            for b in (r.get("content") or [])
            if isinstance(b, dict)
        )
        for r in extra2_results
    )
    extra2_any_error = any(
        bool(r.get("is_error")) for r in extra2_results
    )

    report["verdict"] = {
        "missing_required_fields": verdict,
        "partial_handler_fired": partial_handler_fired,
        "partial_any_error": partial_any_error,
        "extra_key_on_required_handler_fired_with_foo": (
            extra_handler_fired_with_foo
        ),
        "extra_key_on_required_any_error": extra_any_error,
        "extra_key_on_echo_extra_handler_fired_with_surplus": (
            extra2_handler_fired_with_surplus
        ),
        "extra_key_on_echo_extra_any_error": extra2_any_error,
    }


# ---------------------------------------------------------------------------
# OAuth detection (mirror of phase-3 RQ1 approach).
# ---------------------------------------------------------------------------
def _oauth_present() -> tuple[bool, str]:
    # The local `claude` CLI persists OAuth in macOS Keychain or in
    # ~/.claude/.credentials.json. We can't read Keychain headlessly, so
    # the cheapest probe is: invoke `claude --version` (already done by
    # the SDK at query time). The phase-3 RQ1 spike worked on this same
    # host, so credentials are assumed present. We do a soft check.
    home = Path.home()
    cred = home / ".claude" / ".credentials.json"
    if cred.exists():
        return True, f"file: {cred}"
    # Keychain-based session — can't verify cheaply; rely on first query
    # to surface auth errors.
    return True, "no .credentials.json file (likely Keychain) — proceed and let SDK surface auth errors"


async def main() -> int:
    started = time.monotonic()
    report: dict[str, Any] = {
        "sdk_version": "0.1.63",
        "python_version": sys.version.split()[0],
        "cwd_at_launch": os.getcwd(),
        "budget_usd_cap": BUDGET_USD,
        "wall_budget_sec": WALL_BUDGET_SEC,
    }

    have_oauth, oauth_note = _oauth_present()
    report["oauth_check"] = {"have": have_oauth, "note": oauth_note}
    if not have_oauth:
        report["error"] = {"reason": "no OAuth — abort"}
        REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
        REPORT_TXT.write_text(
            "RQ7 ABORTED: no OAuth credentials present.\n", encoding="utf-8"
        )
        print("ABORT: no OAuth")
        return 2

    try:
        with tempfile.TemporaryDirectory(prefix="rq7-") as td:
            tmp_project = Path(td)
            (tmp_project / ".claude").mkdir()
            try:
                await asyncio.wait_for(
                    run_probe(tmp_project, report),
                    timeout=WALL_BUDGET_SEC,
                )
                report["error"] = None
            except asyncio.TimeoutError:
                report["error"] = {
                    "class": "TimeoutError",
                    "message": f"exceeded {WALL_BUDGET_SEC}s wall budget",
                }
            except Exception as exc:  # noqa: BLE001 - probe catches all
                report["error"] = {
                    "class": type(exc).__name__,
                    "message": str(exc),
                }
    finally:
        report["elapsed_sec_total"] = round(
            time.monotonic() - started, 2
        )
        REPORT_JSON.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        # Also write a human-readable .txt log.
        lines = [
            "RQ7 — @tool flat-dict optional-args probe",
            "=" * 60,
            f"sdk: {report['sdk_version']}  py: {report['python_version']}",
            f"elapsed_total: {report['elapsed_sec_total']}s",
            f"oauth: {report['oauth_check']}",
            "",
        ]
        if report.get("error"):
            lines.append(f"ERROR: {report['error']}")
        if "verdict" in report:
            lines.append("VERDICT")
            lines.append("-" * 60)
            for k, v in report["verdict"].items():
                lines.append(f"  {k}: {v}")
            lines.append("")
        if "init_meta" in report:
            lines.append("INIT META")
            lines.append("-" * 60)
            for k, v in report["init_meta"].items():
                lines.append(f"  {k}: {v}")
            lines.append("")
        if "forms" in report:
            for fname, fdata in report["forms"].items():
                lines.append(f"FORM: {fname}")
                lines.append("-" * 60)
                lines.append(f"  prompt: {fdata['prompt']}")
                lines.append(
                    f"  elapsed: {fdata['elapsed_sec']}s  "
                    f"stop: {fdata['stop_reason']}  cost: ${fdata['cost_usd']}"
                )
                lines.append("  tool_uses:")
                for tu in fdata.get("tool_uses") or []:
                    lines.append(f"    - {tu}")
                lines.append("  tool_results:")
                for tr in fdata.get("tool_results") or []:
                    lines.append(f"    - {tr}")
                lines.append("")
        if "totals" in report:
            lines.append(f"TOTAL COST: ${report['totals']['cost_usd']}")
            lines.append(f"TOTAL ELAPSED: {report['totals']['elapsed_sec']}s")
        REPORT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps(report.get("verdict", {}), indent=2))
    print(f"\nTotal cost: ${report.get('totals', {}).get('cost_usd', '?')}")
    print(f"JSON: {REPORT_JSON}")
    print(f"TXT:  {REPORT_TXT}")
    return 0 if report.get("error") is None else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
