"""Phase 6 spike S-6-0 — native SDK subagent empirical probe.

Answers Q1..Q13 from plan/phase6/detailed-plan.md §2. A SINGLE consolidated
probe — all subtests are gated by global `SKIP_MODE` flags so a failing SDK
path does not blow the entire run.

Design:
  * real `claude_agent_sdk.query(...)` calls against the user's OAuth session
    (claude CLI 2.x). No API key.
  * `general` AgentDefinition is the primary probe target; `worker` is used
    for parallel-count tests (Q8).
  * Hook factories write observations into a module-level OBS dict; we
    `json.dump` the final dict to `phase6_s0_report.json` alongside this
    script.
  * Every subtest is a top-level async coroutine so `asyncio.run` sees one
    orchestrator coroutine that calls them sequentially with per-test
    `asyncio.timeout(...)` guards.

Run:  uv run python spikes/phase6_s0_native_subagent.py
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    HookContext,
    HookMatcher,
    ResultMessage,
    SubagentStartHookInput,
    SubagentStopHookInput,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    query,
)

HERE = Path(__file__).resolve().parent
REPORT = HERE / "phase6_s0_report.json"
CWD = HERE.parent  # project root

# --------------------------------------------------------------------------
# Global observation sink. One per subtest.
# --------------------------------------------------------------------------

RESULTS: dict[str, Any] = {
    "sdk_version": None,
    "q1_background": None,
    "q2_task_tool": None,
    "q3_task_messages_in_iter": None,
    "q4_recursion": None,
    "q5_transcript_flush": None,
    "q6_cross_bridge_hook": None,
    "q7_cancel": None,
    "q8_concurrency": None,
    "q9_prompt_semantic": None,
    "q10_model_inherit": None,
    "q11_skills_field": None,
    "q12_session_id": None,
    # Q13 is an architecture decision, not empirical.
}


def _now() -> float:
    return time.monotonic()


def _ts() -> str:
    return time.strftime("%H:%M:%S", time.localtime())


# --------------------------------------------------------------------------
# Hook factory used by several subtests.
# --------------------------------------------------------------------------


def make_spy_hooks(bucket: dict[str, Any], *, tag: str) -> dict[str, list[HookMatcher]]:
    """Register SubagentStart/Stop hooks that write into `bucket`."""

    async def on_start(
        input_data: Any,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> dict[str, Any]:
        raw = cast(dict[str, Any], input_data)
        bucket.setdefault("start_events", []).append(
            {
                "tag": tag,
                "at": _now(),
                "hook_event_name": raw.get("hook_event_name"),
                "agent_id": raw.get("agent_id"),
                "agent_type": raw.get("agent_type"),
                "session_id": raw.get("session_id"),
                "cwd": raw.get("cwd"),
                "transcript_path": raw.get("transcript_path"),
                "keys": sorted(raw.keys()),
            }
        )
        return {}

    async def on_stop(
        input_data: Any,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> dict[str, Any]:
        raw = cast(dict[str, Any], input_data)
        agent_transcript = raw.get("agent_transcript_path")
        size = None
        assistant_block_count = 0
        last_text_preview = None
        if agent_transcript:
            try:
                p = Path(agent_transcript)
                size = p.stat().st_size if p.exists() else None
                if p.exists():
                    last_text_preview = ""
                    for line in p.read_text(encoding="utf-8").splitlines():
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        msg = obj.get("message", {}) if isinstance(obj, dict) else {}
                        if msg.get("role") == "assistant":
                            assistant_block_count += 1
                            content = msg.get("content", [])
                            if isinstance(content, list):
                                for blk in content:
                                    if (
                                        isinstance(blk, dict)
                                        and blk.get("type") == "text"
                                    ):
                                        last_text_preview = (blk.get("text") or "")[
                                            :160
                                        ]
            except Exception as e:
                bucket.setdefault("stop_read_errors", []).append(repr(e))
        bucket.setdefault("stop_events", []).append(
            {
                "tag": tag,
                "at": _now(),
                "hook_event_name": raw.get("hook_event_name"),
                "agent_id": raw.get("agent_id"),
                "agent_type": raw.get("agent_type"),
                "agent_transcript_path": agent_transcript,
                "transcript_size_bytes": size,
                "assistant_blocks_in_transcript": assistant_block_count,
                "last_text_preview": last_text_preview,
                "last_assistant_message_from_hook": raw.get(
                    "last_assistant_message"
                ),
                "session_id": raw.get("session_id"),
                "keys": sorted(raw.keys()),
            }
        )
        return {}

    return {
        "SubagentStart": [HookMatcher(hooks=[on_start])],
        "SubagentStop": [HookMatcher(hooks=[on_stop])],
    }


# --------------------------------------------------------------------------
# AgentDefinition builder (mirrors what plan §3 specs).
# --------------------------------------------------------------------------


def build_agents(
    *,
    model: str | None = None,
    max_turns: int = 6,
    tools: list[str] | None = None,
    skills: list[str] | None = None,
    prompt_extra: str = "",
    background: bool = False,
) -> dict[str, AgentDefinition]:
    base_prompt = (
        "You are a background subagent. "
        "Reply with the FINAL text as your last assistant message. "
        "Be concise."
    )
    if prompt_extra:
        base_prompt += "\n" + prompt_extra
    common_kwargs: dict[str, Any] = {
        "description": "Generic background task",
        "prompt": base_prompt,
        "tools": tools,
        "maxTurns": max_turns,
        "model": model,
    }
    if skills is not None:
        common_kwargs["skills"] = skills
    # Wave-2 B-W2-1: explicitly pass background= so Q1 can measure both modes.
    try:
        _probe = AgentDefinition(
            description="x", prompt="x", model=model, background=background
        )
        _probe_has_bg = getattr(_probe, "background", None) == background
    except TypeError:
        _probe_has_bg = False
    if _probe_has_bg:
        common_kwargs["background"] = background
    return {
        "general": AgentDefinition(**common_kwargs),
        "worker": AgentDefinition(
            description="Worker agent",
            prompt=base_prompt + " You are a WORKER.",
            tools=tools,
            maxTurns=max_turns,
            model=model,
            **({"background": background} if _probe_has_bg else {}),
        ),
    }


# --------------------------------------------------------------------------
# Orchestrator + tiny wall-clock helpers.
# --------------------------------------------------------------------------


async def _run_query(
    *,
    user_prompt: str,
    options: ClaudeAgentOptions,
    per_test_timeout_s: float = 120.0,
    record_into: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run `query(...)`; return timeline of messages as list + meta."""
    timeline: list[dict[str, Any]] = []
    start = _now()
    first_assistant_at: float | None = None
    first_task_started_at: float | None = None
    first_task_notification_at: float | None = None
    result_at: float | None = None
    result_message: dict[str, Any] | None = None
    assistant_text_chunks: list[str] = []
    try:
        async with asyncio.timeout(per_test_timeout_s):
            async for msg in query(prompt=user_prompt, options=options):
                kind = type(msg).__name__
                rel = _now() - start
                entry: dict[str, Any] = {"kind": kind, "t": round(rel, 3)}
                if isinstance(msg, TaskStartedMessage):
                    first_task_started_at = first_task_started_at or rel
                    entry["task_id"] = msg.task_id
                    entry["task_description"] = msg.description[:120]
                elif isinstance(msg, TaskProgressMessage):
                    entry["task_id"] = msg.task_id
                    entry["last_tool"] = msg.last_tool_name
                elif isinstance(msg, TaskNotificationMessage):
                    first_task_notification_at = first_task_notification_at or rel
                    entry["task_id"] = msg.task_id
                    entry["status"] = getattr(msg, "status", None)
                    entry["summary_preview"] = (msg.summary or "")[:160]
                    entry["output_file"] = msg.output_file
                elif isinstance(msg, AssistantMessage):
                    first_assistant_at = first_assistant_at or rel
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            assistant_text_chunks.append(block.text)
                    entry["content_kinds"] = [
                        type(b).__name__ for b in msg.content
                    ]
                elif isinstance(msg, ResultMessage):
                    result_at = rel
                    result_message = {
                        "stop_reason": msg.stop_reason,
                        "num_turns": msg.num_turns,
                        "cost_usd": msg.total_cost_usd,
                        "duration_ms": msg.duration_ms,
                    }
                    entry.update(result_message)
                elif isinstance(msg, SystemMessage):
                    entry["subtype"] = getattr(msg, "subtype", None)
                timeline.append(entry)
    except asyncio.TimeoutError:
        timeline.append({"kind": "TIMEOUT", "t": _now() - start})
    out = {
        "wall_seconds": _now() - start,
        "first_assistant_at": first_assistant_at,
        "first_task_started_at": first_task_started_at,
        "first_task_notification_at": first_task_notification_at,
        "result_at": result_at,
        "result": result_message,
        "timeline": timeline,
        "assistant_text_joined": "".join(assistant_text_chunks),
    }
    if record_into is not None:
        record_into.update(out)
    return out


# --------------------------------------------------------------------------
# Q1 + Q2 + Q3: one combined probe.
# --------------------------------------------------------------------------


async def test_q1_q2_q3() -> None:
    """Spawn a `general` subagent; check background + Task-tool + message kinds.

    Wave-2 re-run: `background=True` is now passed explicitly (previous v1
    spike omitted it — see B-W2-1). We keep this probe's verdict on the
    new background=True code path; `test_q1_background_compare` below
    contrasts against `background=False` in a separate run.
    """
    print(f"[{_ts()}] Q1/Q2/Q3: spawn background subagent (background=True)")
    obs: dict[str, Any] = {}
    hooks = make_spy_hooks(obs, tag="q1")
    agents = build_agents(
        model="inherit",
        max_turns=4,
        tools=["Read"],  # narrow; subagent must answer mostly from knowledge
        background=True,  # wave-2: explicit.
    )
    opts = ClaudeAgentOptions(
        cwd=str(CWD),
        setting_sources=None,  # isolate from project settings
        max_turns=5,
        allowed_tools=["Task", "Read"],  # explicit Task so main model can delegate
        hooks=hooks,
        agents=agents,
        system_prompt=(
            "You coordinate background tasks. When the user asks you to "
            "delegate, use the `general` agent via the Task tool and report "
            "the returned task_id to the user briefly, then STOP. Do NOT "
            "wait for the task result."
        ),
    )
    prompt = (
        "Please use the `general` agent (Task tool) to write a concise "
        "3-sentence paragraph about cats. After you launch it, just reply "
        "with the word 'launched' and stop."
    )
    run_data: dict[str, Any] = {}
    await _run_query(
        user_prompt=prompt,
        options=opts,
        per_test_timeout_s=180.0,
        record_into=run_data,
    )

    # Q1: did main ResultMessage arrive BEFORE the first TaskNotificationMessage?
    q1: dict[str, Any] = {}
    result_at = run_data.get("result_at")
    first_notif = run_data.get("first_task_notification_at")
    q1["main_result_at"] = result_at
    q1["first_task_notification_at"] = first_notif
    if result_at is not None and first_notif is not None:
        q1["main_finished_before_subagent"] = result_at < first_notif
    elif result_at is not None and first_notif is None:
        q1["main_finished_before_subagent"] = True
        q1["note"] = "main finished; no TaskNotification within iter → subagent still running or no native spawn"
    else:
        q1["main_finished_before_subagent"] = None

    # Q2: did the model emit a ToolUseBlock with name='Task'?
    task_tool_uses = 0
    for entry in run_data["timeline"]:
        if entry["kind"] == "AssistantMessage":
            kinds = entry.get("content_kinds", [])
            for kind in kinds:
                if kind == "ToolUseBlock":
                    task_tool_uses += 1
    # More reliable signal: presence of TaskStartedMessage in iter.
    q2 = {
        "task_started_messages_seen": sum(
            1 for e in run_data["timeline"] if e["kind"] == "TaskStartedMessage"
        ),
        "assistant_tool_use_blocks": task_tool_uses,
        "hint": (
            "If task_started_messages_seen > 0 → model auto-discovered Task tool "
            "from agents registry."
        ),
    }

    # Q3: which kinds of Task*Message appeared in main iter?
    kinds_seen = {e["kind"] for e in run_data["timeline"]}
    q3 = {
        "distinct_message_kinds": sorted(kinds_seen),
        "task_started_count": sum(
            1 for e in run_data["timeline"] if e["kind"] == "TaskStartedMessage"
        ),
        "task_progress_count": sum(
            1 for e in run_data["timeline"] if e["kind"] == "TaskProgressMessage"
        ),
        "task_notification_count": sum(
            1 for e in run_data["timeline"] if e["kind"] == "TaskNotificationMessage"
        ),
    }

    # If we saw TaskStarted but no TaskNotification before Result -- confirms Q1.
    RESULTS["q1_background"] = q1
    RESULTS["q2_task_tool"] = q2
    RESULTS["q3_task_messages_in_iter"] = q3
    RESULTS["q1_q2_q3_raw"] = {
        "run": run_data,
        "hook_observations": obs,
    }
    RESULTS["q5_transcript_flush"] = _analyse_q5(obs)


async def test_q1_background_compare() -> None:
    """Wave-2 B-W2-1 follow-up: compare background=True vs background=False
    on the same prompt shape. Records:
      * result_at (main ResultMessage)
      * first_task_notification_at (child completes)
      * main_finished_before_subagent bool

    Uses a slightly larger child task (writes a 200-word paragraph) to
    widen the gap and reduce timing noise.
    """
    print(f"[{_ts()}] Q1-BG: compare background=True vs background=False")
    compare: dict[str, Any] = {}

    async def _one_run(*, bg: bool) -> dict[str, Any]:
        obs: dict[str, Any] = {}
        hooks = make_spy_hooks(obs, tag=f"q1bg_{bg}")
        agents = build_agents(
            model="inherit",
            max_turns=4,
            tools=["Read"],
            background=bg,
        )
        opts = ClaudeAgentOptions(
            cwd=str(CWD),
            setting_sources=None,
            max_turns=5,
            allowed_tools=["Task", "Read"],
            hooks=hooks,
            agents=agents,
            system_prompt=(
                "You coordinate background tasks. When the user asks you to "
                "delegate, use the `general` agent via the Task tool. AS SOON "
                "as the Task tool returns a task_id, reply with exactly "
                "'dispatched' and STOP. Do NOT wait, summarise, or comment."
            ),
        )
        prompt = (
            "Use the `general` agent (Task tool) to write a detailed 200-word "
            "paragraph about the history of bridges. After you invoke Task, "
            "reply 'dispatched' and stop."
        )
        run: dict[str, Any] = {}
        await _run_query(
            user_prompt=prompt,
            options=opts,
            per_test_timeout_s=300.0,
            record_into=run,
        )
        return {
            "background_flag": bg,
            "result_at": run.get("result_at"),
            "first_task_notification_at": run.get("first_task_notification_at"),
            "first_task_started_at": run.get("first_task_started_at"),
            "main_finished_before_subagent": (
                run["result_at"] < run["first_task_notification_at"]
                if run.get("result_at") is not None
                and run.get("first_task_notification_at") is not None
                else None
            ),
            "wall_seconds": run.get("wall_seconds"),
            "stop_events_count": len(obs.get("stop_events", [])),
            "start_events_count": len(obs.get("start_events", [])),
            "timeline_kinds": [e["kind"] for e in run["timeline"]],
            "main_cost_usd": (run.get("result") or {}).get("cost_usd"),
        }

    try:
        compare["bg_true"] = await _one_run(bg=True)
    except Exception as e:
        compare["bg_true"] = {"error": repr(e)}
    try:
        compare["bg_false"] = await _one_run(bg=False)
    except Exception as e:
        compare["bg_false"] = {"error": repr(e)}

    # Verdict synthesis.
    bt = compare.get("bg_true", {})
    bf = compare.get("bg_false", {})
    bg_true_non_blocking = bt.get("main_finished_before_subagent") is True
    bg_false_non_blocking = bf.get("main_finished_before_subagent") is True
    if bg_true_non_blocking and not bg_false_non_blocking:
        verdict = "PASS_BACKGROUND_FREES_MAIN"
    elif bg_true_non_blocking and bg_false_non_blocking:
        verdict = "PASS_BUT_BG_NO_EFFECT"
    elif not bg_true_non_blocking and not bg_false_non_blocking:
        verdict = "FAIL_BG_BLOCKS_MAIN_TURN"  # same as v1 finding — flag has no effect
    else:
        verdict = "AMBIGUOUS"
    compare["verdict"] = verdict
    RESULTS["q1_background_compare"] = compare


def _analyse_q5(obs: dict[str, Any]) -> dict[str, Any]:
    """Q5: SubagentStop hook fired AFTER transcript flushed + readable?"""
    stop_events = obs.get("stop_events", [])
    start_events = obs.get("start_events", [])
    if not stop_events:
        return {
            "verdict": "UNKNOWN",
            "note": "no SubagentStop hook fired",
            "start_events_seen": len(start_events),
        }
    sizes = [e["transcript_size_bytes"] for e in stop_events]
    blocks = [e["assistant_blocks_in_transcript"] for e in stop_events]
    previews = [e["last_text_preview"] for e in stop_events]
    all_nonempty = all(s and s > 0 for s in sizes if s is not None)
    any_assistant = any(b and b > 0 for b in blocks)
    verdict = (
        "PASS"
        if stop_events and all_nonempty and any_assistant
        else "PARTIAL"
    )
    return {
        "verdict": verdict,
        "stop_events_count": len(stop_events),
        "start_events_count": len(start_events),
        "transcript_sizes": sizes,
        "assistant_block_counts": blocks,
        "last_text_previews": previews,
        "read_errors": obs.get("stop_read_errors", []),
    }


# --------------------------------------------------------------------------
# Q4: recursion (subagent spawns sub-subagent).
# --------------------------------------------------------------------------


async def test_q4_recursion() -> None:
    print(f"[{_ts()}] Q4: subagent-from-subagent recursion attempt")
    obs: dict[str, Any] = {}
    hooks = make_spy_hooks(obs, tag="q4")
    agents = {
        "general": AgentDefinition(
            description="Background agent capable of delegating further",
            prompt=(
                "You are a background subagent. If asked, you MAY spawn a child "
                "via the Task tool. Report the child's task_id and stop."
            ),
            tools=["Task", "Read"],
            model="inherit",
            maxTurns=4,
        ),
    }
    opts = ClaudeAgentOptions(
        cwd=str(CWD),
        setting_sources=None,
        max_turns=5,
        allowed_tools=["Task", "Read"],
        hooks=hooks,
        agents=agents,
    )
    run_data: dict[str, Any] = {}
    try:
        await _run_query(
            user_prompt=(
                "Use the `general` agent to perform the following: "
                "inside it, delegate once more to `general` asking the child "
                "to write the single word 'grandchild' and return that. "
                "Report the task_ids back to me briefly."
            ),
            options=opts,
            per_test_timeout_s=180.0,
            record_into=run_data,
        )
    except Exception as e:
        RESULTS["q4_recursion"] = {"verdict": "ERROR", "error": repr(e)}
        return
    # agent_ids across start events — distinct count > 1 means recursion occurred.
    start_ids = [e["agent_id"] for e in obs.get("start_events", [])]
    distinct = sorted(set(start_ids))
    RESULTS["q4_recursion"] = {
        "distinct_agent_ids_in_start_events": distinct,
        "recursion_observed": len(distinct) > 1,
        "start_events_count": len(start_ids),
        "stop_events_count": len(obs.get("stop_events", [])),
        "start_event_session_ids": [e["session_id"] for e in obs.get("start_events", [])],
        "timeline_wall_s": run_data.get("wall_seconds"),
    }
    RESULTS["q4_recursion_raw"] = {"obs": obs}


# --------------------------------------------------------------------------
# Q6: cross-bridge / multi-options scenario.
# --------------------------------------------------------------------------


async def test_q6_cross_bridge() -> None:
    print(f"[{_ts()}] Q6: SubagentStop fires from separate options instances")
    shared_obs: dict[str, Any] = {}
    hooks_for_both = make_spy_hooks(shared_obs, tag="q6_shared")

    def make_opts(tag: str) -> ClaudeAgentOptions:
        agents = build_agents(
            model="inherit",
            max_turns=3,
            tools=["Read"],
            prompt_extra=f"(instance-{tag})",
        )
        return ClaudeAgentOptions(
            cwd=str(CWD),
            setting_sources=None,
            max_turns=4,
            allowed_tools=["Task", "Read"],
            hooks=hooks_for_both,
            agents=agents,
            system_prompt=(
                "Launch the `general` agent on the user's request and reply "
                "with just 'launched'. Do NOT wait for it."
            ),
        )

    async def one_instance(tag: str) -> dict[str, Any]:
        r: dict[str, Any] = {}
        try:
            await _run_query(
                user_prompt=(
                    f"[{tag}] Use the `general` agent to write ONE sentence "
                    "about trees. Then reply 'launched'."
                ),
                options=make_opts(tag),
                per_test_timeout_s=180.0,
                record_into=r,
            )
        except Exception as e:
            r = {"error": repr(e)}
        return r

    runs = await asyncio.gather(one_instance("A"), one_instance("B"))
    start_ids = sorted({e["agent_id"] for e in shared_obs.get("start_events", [])})
    stop_ids = sorted({e["agent_id"] for e in shared_obs.get("stop_events", [])})
    RESULTS["q6_cross_bridge_hook"] = {
        "start_events_count": len(shared_obs.get("start_events", [])),
        "stop_events_count": len(shared_obs.get("stop_events", [])),
        "distinct_start_agent_ids": start_ids,
        "distinct_stop_agent_ids": stop_ids,
        "verdict": (
            "PASS"
            if len(start_ids) >= 2 and len(stop_ids) >= 2
            else "PARTIAL_OR_FAIL"
        ),
        "note": (
            "Both options instances share the SAME hook callback object; if "
            "distinct start/stop agent_ids ≥ 2 then the shared factory "
            "pattern will work — the plan's Daemon can build one set of "
            "hook callbacks and pass them into every ClaudeBridge."
        ),
    }
    RESULTS["q6_cross_bridge_raw"] = {"shared_obs": shared_obs, "runs": runs}


# --------------------------------------------------------------------------
# Q7: cancel-propagation.
# --------------------------------------------------------------------------


async def test_q7_cancel() -> None:
    print(f"[{_ts()}] Q7: cancel main task → child behaviour")
    obs: dict[str, Any] = {}
    hooks = make_spy_hooks(obs, tag="q7")
    agents = {
        "general": AgentDefinition(
            description="Long-running child",
            prompt=(
                "You are a LONG subagent. Write 1000 words about the "
                "history of mathematics. Do NOT stop early. Use only your "
                "knowledge — no tools needed."
            ),
            tools=["Read"],
            model="inherit",
            maxTurns=8,
        ),
    }
    opts = ClaudeAgentOptions(
        cwd=str(CWD),
        setting_sources=None,
        max_turns=4,
        allowed_tools=["Task", "Read"],
        hooks=hooks,
        agents=agents,
        system_prompt=(
            "Delegate the user's writing request to the `general` agent and "
            "STOP. Reply with 'dispatched' once. Do not wait."
        ),
    )

    start = _now()
    cancel_age: float | None = None
    error_at_main: str | None = None

    async def driver() -> None:
        async for _msg in query(
            prompt=(
                "Use the `general` agent to write 1000 words on the history "
                "of mathematics. Reply with 'dispatched' and stop."
            ),
            options=opts,
        ):
            # keep iterating — we want cancel to land while subagent still running
            pass

    main_task = asyncio.create_task(driver(), name="q7_main")
    # wait until we see SubagentStart OR 10s, then cancel.
    waited = 0.0
    while waited < 15.0 and not obs.get("start_events"):
        await asyncio.sleep(0.5)
        waited += 0.5
    cancel_age = _now() - start
    main_task.cancel()
    try:
        await asyncio.wait_for(main_task, timeout=30.0)
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError as e:
        error_at_main = f"timeout awaiting cancelled main: {e}"
    except Exception as e:
        error_at_main = repr(e)

    # After cancel, check if SubagentStop fires within a grace window.
    grace_waited = 0.0
    while grace_waited < 20.0:
        if obs.get("stop_events"):
            break
        await asyncio.sleep(1.0)
        grace_waited += 1.0

    RESULTS["q7_cancel"] = {
        "cancel_at_s": cancel_age,
        "grace_waited_s": grace_waited,
        "start_events": len(obs.get("start_events", [])),
        "stop_events": len(obs.get("stop_events", [])),
        "main_task_cancel_err": error_at_main,
        "stop_events_detail": obs.get("stop_events", []),
        "verdict_note": (
            "If start_events>0 and stop_events==0: subagent orphaned on main "
            "cancel (fallback = flag-poll). If both == expected count: SDK "
            "propagates cancel."
        ),
    }


# --------------------------------------------------------------------------
# Q8: concurrency cap — launch N subagents in one main turn.
# --------------------------------------------------------------------------


async def test_q8_concurrency(n_parallel: int = 4) -> None:
    print(f"[{_ts()}] Q8: ask main to launch {n_parallel} subagents")
    obs: dict[str, Any] = {}
    hooks = make_spy_hooks(obs, tag="q8")
    agents = {
        "general": AgentDefinition(
            description="Background agent",
            prompt=(
                "You are a subagent. Write ONE sentence about the topic "
                "provided and stop."
            ),
            tools=["Read"],
            model="inherit",
            maxTurns=3,
        ),
    }
    opts = ClaudeAgentOptions(
        cwd=str(CWD),
        setting_sources=None,
        max_turns=5,
        allowed_tools=["Task", "Read"],
        hooks=hooks,
        agents=agents,
        system_prompt=(
            f"On the user's request, launch EXACTLY {n_parallel} `general` agents "
            "in parallel — one per topic given. Do not wait. Reply 'dispatched' once."
        ),
    )
    topics = [f"topic_{i}" for i in range(n_parallel)]
    run_data: dict[str, Any] = {}
    try:
        await _run_query(
            user_prompt=(
                f"Launch {n_parallel} `general` subagents in parallel. "
                f"Each writes one sentence about its topic. Topics: {topics}."
            ),
            options=opts,
            per_test_timeout_s=300.0,
            record_into=run_data,
        )
    except Exception as e:
        RESULTS["q8_concurrency"] = {"verdict": "ERROR", "error": repr(e)}
        return

    start_events = obs.get("start_events", [])
    stop_events = obs.get("stop_events", [])
    # Heuristic: ordered (start_at, stop_at) per agent_id. Max overlap count ≈ cap.
    by_id: dict[str, dict[str, Any]] = {}
    for e in start_events:
        by_id.setdefault(e["agent_id"], {})["start_at"] = e["at"]
    for e in stop_events:
        by_id.setdefault(e["agent_id"], {})["stop_at"] = e["at"]
    intervals = [
        (v["start_at"], v.get("stop_at", v["start_at"] + 0.01))
        for v in by_id.values()
        if "start_at" in v
    ]
    max_overlap = 0
    if intervals:
        # Sweep-line peak concurrency.
        events = []
        for s, e in intervals:
            events.append((s, +1))
            events.append((e, -1))
        events.sort()
        cur = 0
        for _t, delta in events:
            cur += delta
            max_overlap = max(max_overlap, cur)

    RESULTS["q8_concurrency"] = {
        "requested_parallel": n_parallel,
        "distinct_start_agent_ids": len(by_id),
        "start_events_count": len(start_events),
        "stop_events_count": len(stop_events),
        "peak_overlap_observed": max_overlap,
        "wall_seconds": run_data.get("wall_seconds"),
        "note": (
            "peak_overlap_observed is a LOWER BOUND on effective concurrency — "
            "if ≥ requested_parallel, SDK has no visible cap at this N."
        ),
    }


# --------------------------------------------------------------------------
# Q9-Q12: cheap probes extracted from q1 run + fresh probe.
# --------------------------------------------------------------------------


async def test_q9_prompt_semantic() -> None:
    """Q9: does `AgentDefinition.prompt` replace or append to the base system prompt?"""
    print(f"[{_ts()}] Q9: AgentDefinition.prompt semantic")
    obs: dict[str, Any] = {}
    hooks = make_spy_hooks(obs, tag="q9")
    agents = {
        "general": AgentDefinition(
            description="Probe agent that follows a custom style",
            prompt=(
                "You are a haiku-only agent. Marker MARKER_Q9_XYZ999. "
                "Every reply MUST be exactly one 5-7-5 English haiku. "
                "Include the word 'marker' in the final line verbatim. "
                "Stop after one haiku."
            ),
            tools=["Read"],
            model="inherit",
            maxTurns=2,
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
            "Delegate writing tasks to the `general` agent via the Task tool. "
            "Always use it for any creative writing request."
        ),
    )
    run_data: dict[str, Any] = {}
    try:
        await _run_query(
            user_prompt=(
                "Please use the `general` agent to write a haiku about summer. "
                "Report back the haiku the agent produced."
            ),
            options=opts,
            per_test_timeout_s=180.0,
            record_into=run_data,
        )
    except Exception as e:
        RESULTS["q9_prompt_semantic"] = {"verdict": "ERROR", "error": repr(e)}
        return
    previews = [e.get("last_text_preview") for e in obs.get("stop_events", [])]
    # Also examine full transcript text to catch marker even if not in preview.
    full_texts: list[str] = []
    for e in obs.get("stop_events", []):
        p = e.get("agent_transcript_path")
        if p and Path(p).exists():
            try:
                full_texts.append(Path(p).read_text(encoding="utf-8")[:4000])
            except Exception:
                pass
    from_hook_msgs = [
        e.get("last_assistant_message_from_hook") for e in obs.get("stop_events", [])
    ]
    RESULTS["q9_prompt_semantic"] = {
        "stop_events_count": len(obs.get("stop_events", [])),
        "start_events_count": len(obs.get("start_events", [])),
        "subagent_last_messages": from_hook_msgs,
        "main_assistant_text_preview": run_data.get("assistant_text_joined", "")[
            :400
        ],
        "full_transcript_preview": full_texts[0] if full_texts else None,
        "note": (
            "Check if subagent reply follows the haiku constraint. If yes → "
            "subagent saw the prompt verbatim ('full system prompt' semantic)."
        ),
    }


async def test_q10_model_inherit() -> None:
    """Q10: does model='inherit' construct and actually run?"""
    print(f"[{_ts()}] Q10: model='inherit' validity")
    try:
        ad = AgentDefinition(
            description="x",
            prompt="x",
            model="inherit",
        )
        RESULTS["q10_model_inherit"] = {
            "constructor_accepts": True,
            "agent_definition_model_repr": ad.model,
            "note": (
                "Construction accepted the value; Q1 run uses model='inherit' "
                "— if Q1 ran to ResultMessage, 'inherit' is runtime-valid."
            ),
        }
    except Exception as e:
        RESULTS["q10_model_inherit"] = {"constructor_accepts": False, "err": repr(e)}


async def test_q11_skills_field() -> None:
    """Q11: `skills` field — does passing a known manifest slug narrow subagent view?"""
    print(f"[{_ts()}] Q11: skills list narrowing")
    obs: dict[str, Any] = {}
    hooks = make_spy_hooks(obs, tag="q11")
    # Pick a slug that exists in skills/ — directory name IS the slug.
    existing = sorted([p.parent.name for p in (CWD / "skills").glob("*/SKILL.md")])
    candidate = "memory" if "memory" in existing else (
        existing[0] if existing else "memory"
    )
    agents = {
        "general": AgentDefinition(
            description="Skill-narrowed probe",
            prompt="Reply with 'ok' and stop.",
            tools=["Read"],
            model="inherit",
            skills=[candidate],
            maxTurns=2,
        ),
    }
    opts = ClaudeAgentOptions(
        cwd=str(CWD),
        setting_sources=None,
        max_turns=3,
        allowed_tools=["Task", "Read"],
        hooks=hooks,
        agents=agents,
    )
    try:
        run_data: dict[str, Any] = {}
        await _run_query(
            user_prompt=(
                "Use the `general` agent to reply with the word 'ok' and stop."
            ),
            options=opts,
            per_test_timeout_s=120.0,
            record_into=run_data,
        )
        RESULTS["q11_skills_field"] = {
            "configured_slug": candidate,
            "available_slugs": existing,
            "stop_events": len(obs.get("stop_events", [])),
            "note": (
                "Empirical narrowing not introspectable from outside. "
                "If this succeeds without errors, the field is accepted at "
                "runtime; coverage of actual narrowing is a phase-7 concern."
            ),
        }
    except Exception as e:
        RESULTS["q11_skills_field"] = {"verdict": "ERROR", "error": repr(e)}


async def test_q12_session_id_stability() -> None:
    """Q12: is `session_id` in SubagentStart stable within one main turn?"""
    print(f"[{_ts()}] Q12: session_id usefulness")
    # We already captured start events in Q1 obs (q1_raw). Reuse those if present.
    raw = RESULTS.get("q1_q2_q3_raw")
    note: dict[str, Any] = {}
    if raw:
        obs = raw["hook_observations"]
        sess = [e["session_id"] for e in obs.get("start_events", [])]
        note = {
            "start_session_ids": sess,
            "all_equal": len(set(sess)) == 1 if sess else None,
            "hint": (
                "session_id on SubagentStart — whose session is it? If it "
                "equals the MAIN session, it's the parent pointer (works for "
                "ledger parent linking). If unique per subagent, it's the "
                "subagent's own session."
            ),
        }
    RESULTS["q12_session_id"] = note


# --------------------------------------------------------------------------
# Main.
# --------------------------------------------------------------------------


async def main() -> None:
    import claude_agent_sdk as sdk
    RESULTS["sdk_version"] = sdk.__version__
    RESULTS["started_at"] = _ts()

    tests = [
        ("Q10 (construct-only)", test_q10_model_inherit),
        ("Q1/Q2/Q3 + Q5 combined", test_q1_q2_q3),
        ("Q1-BG compare", test_q1_background_compare),
        ("Q9 prompt semantic", test_q9_prompt_semantic),
        ("Q11 skills field", test_q11_skills_field),
        ("Q12 session id", test_q12_session_id_stability),
        ("Q4 recursion", test_q4_recursion),
        ("Q6 cross-bridge", test_q6_cross_bridge),
        ("Q7 cancel", test_q7_cancel),
        ("Q8 concurrency", test_q8_concurrency),
    ]
    # Env switch: run only a subset (e.g. SPIKE_TESTS=q1,q9)
    only = os.environ.get("SPIKE_TESTS", "").strip()
    if only:
        want = {s.strip().lower() for s in only.split(",")}
        tests = [t for t in tests if any(w in t[0].lower() for w in want)]

    for name, fn in tests:
        print(f"\n===== BEGIN {name} =====")
        t0 = _now()
        try:
            await fn()
        except Exception as e:
            RESULTS.setdefault("errors", []).append(
                {"test": name, "error": repr(e), "traceback": traceback.format_exc()}
            )
            print(f"[{_ts()}] {name} ERROR: {e!r}")
        finally:
            print(f"[{_ts()}] {name} done in {_now() - t0:.2f}s")

    RESULTS["finished_at"] = _ts()
    REPORT.write_text(json.dumps(RESULTS, indent=2, default=str), encoding="utf-8")
    print(f"\n[{_ts()}] WROTE {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
